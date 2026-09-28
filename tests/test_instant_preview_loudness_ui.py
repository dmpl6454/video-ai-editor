"""The Instant preview plays at the project's loudness, in Chromium AND WebKit
(INSTANT_PREVIEW_SPEC §3.6, §7; Final QA r3).

The finding: on a project with the default −16 LUFS target, Instant preview
(beta) played the raw mix — about 11 dB under the server preview and the
export — and classed every frame EXACT, with no ≈ chip. The client now plays
the server preview's master gain (GET /preview_loudness) and marks the frames
APPROX ("Loudness") until that gain was measured for this sound.

Measured here on the LIVE sink: a ScriptProcessor tap on everything the app
connects to its AudioContext's destination, while the app plays; against the
server preview file of the same timeline, decoded. Spec bar: 0.5 dB.

Harness: test_frontend_a11y's server (VAE_A11Y_BASE_URL for a Vite dev
server proxying /api to a backend), test_wave_d_rail_ui's engines.
"""
from __future__ import annotations

import json
import math
import shutil
import subprocess
import time
from pathlib import Path

import httpx
import numpy as np
import pytest

from test_frontend_a11y import base_url  # noqa: F401  (fixture)
from test_wave_d_rail_ui import engine, pw  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")

DUR = 8.0
TAP = r"""
(() => {
  window.__lv = { sum: 0, n: 0, on: false }
  const orig = AudioNode.prototype.connect
  const taps = new WeakMap()
  AudioNode.prototype.connect = function (dest, ...rest) {
    const r = orig.call(this, dest, ...rest)
    try {
      const ctx = this.context
      if (dest === ctx.destination && !this.__tap) {
        let sp = taps.get(ctx)
        if (!sp) {
          sp = ctx.createScriptProcessor(4096, 2, 2); sp.__tap = true
          sp.onaudioprocess = (e) => {
            e.outputBuffer.getChannelData(0).fill(0); e.outputBuffer.getChannelData(1).fill(0)
            if (!window.__lv.on) return
            const L = e.inputBuffer.getChannelData(0)
            let s = 0, p = 0
            for (let i = 0; i < L.length; i++) { s += L[i] * L[i]; p = Math.max(p, Math.abs(L[i])) }
            if (p > 1e-4) { window.__lv.sum += s; window.__lv.n += L.length }
          }
          orig.call(sp, ctx.destination); taps.set(ctx, sp)
        }
        orig.call(this, sp)
      }
    } catch (e) { /* not an AudioNode we can tap */ }
    return r
  }
})()
"""


def _noise(p: Path) -> Path:
    # steady pink noise at about −30 dBFS RMS: a stable level for a 4 s window
    subprocess.run(["ffmpeg", "-y", "-v", "error",
                    "-f", "lavfi", "-i", f"color=c=gray:s=320x180:d={DUR}:r=30",
                    "-f", "lavfi", "-i", f"anoisesrc=color=pink:amplitude=0.1:seed=7:d={DUR}:r=48000",
                    "-filter_complex", "[1:a]aformat=channel_layouts=stereo[a]", "-map", "0:v", "-map", "[a]",
                    "-shortest", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", "-b:a", "256k", str(p)], check=True, capture_output=True)
    return p


def _rms_file(path: str, t0: float, t1: float) -> float:
    pcm = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", str(t0), "-t", str(t1 - t0), "-i", path,
                          "-map", "0:a:0", "-af", "pan=mono|c0=c0", "-ar", "48000", "-f", "f32le", "pipe:1"],
                         capture_output=True, check=True).stdout
    x = np.frombuffer(pcm, dtype=np.float32).astype(np.float64)
    return float(np.sqrt(np.mean(x * x)))


@pytest.fixture(scope="module")
def session(base_url, tmp_path_factory):  # noqa: F811
    src = _noise(tmp_path_factory.mktemp("loud") / "noise.mp4")
    with httpx.Client(base_url=base_url, timeout=300) as c:
        sid = c.post("/api/sessions", json={"name": "instant loudness"}).json()["id"]
        with src.open("rb") as fh:
            r = c.post(f"/api/sessions/{sid}/upload", files={"file": ("noise.mp4", fh, "video/mp4")},
                       data={"add_to_timeline": "true", "transcribe": "false"})
        assert r.status_code in (200, 202), r.text
        deadline = time.time() + 120
        while time.time() < deadline:
            edl = c.get(f"/api/sessions/{sid}/edl").json()
            if any(t["id"] == "v1" and t["clips"] for t in edl["tracks"]):
                break
            time.sleep(0.3)
        assert edl["canvas"]["loudness_lufs"] == -16.0          # the default target
    return sid


def _set_engine(base_url, mode: str) -> None:  # noqa: F811
    r = httpx.put(f"{base_url}/api/settings/preview", json={"engine": mode}, timeout=10)
    assert r.status_code == 200, r.text


def test_instant_preview_plays_at_the_server_previews_loudness(engine, base_url, session):  # noqa: F811
    sid = session
    _set_engine(base_url, "client")
    try:
        ctx = engine.new_context(viewport={"width": 1440, "height": 900})
        ctx.add_init_script(f"try {{ localStorage.setItem('vai.sessionId', {json.dumps(sid)}) }} catch (e) {{}}")
        ctx.add_init_script(TAP)
        page = ctx.new_page()
        page.goto(base_url + "/?vae-test")
        page.locator(".timeline-canvas-wrap canvas").first.wait_for(timeout=60000)
        if page.locator('[data-preview-engine="client"]').count() == 0:
            page.wait_for_timeout(3000)
        if page.locator('[data-preview-engine="client"]').count() == 0:
            assert engine.engine_name != "chromium", "headless Chromium runs the client preview"
            pytest.skip(f"{engine.engine_name}: the client preview does not run here")
        # The idle server render measures this sound; its gain becomes
        # current and the ≈ "Loudness" goes (the engine classed the whole
        # timeline EXACT before, at the raw level).
        page.wait_for_function("""() => { const v = window.__vaeTest.useStore.getState().clientView
            return !!v && v.live && !v.wait && !v.reasons.includes('audio:loudness') }""", timeout=90000)
        chip = page.locator('[data-fidelity="approx"]')
        assert chip.count() == 0 or "Loudness" not in (chip.first.get_attribute("aria-label") or "")

        transport = page.locator(".timeline-toolbar button[aria-keyshortcuts=Space]")
        page.evaluate("window.__lv.on = true")
        transport.click()
        page.wait_for_timeout(4500)
        transport.click()
        lv = page.evaluate("window.__lv")
        ctx.close()
    finally:
        _set_engine(base_url, "server")
    live = math.sqrt(lv["sum"] / max(lv["n"], 1))
    assert lv["n"] > 48000 * 3, lv                            # it really played (3 s+ of sound)

    preview = httpx.post(f"{base_url}/api/sessions/{sid}/preview", timeout=600).json()["path"]
    server = _rms_file(preview, 0.5, 4.5)
    gap = 20 * math.log10(live / server)
    assert abs(gap) <= 0.5, f"{engine.engine_name}: Instant preview {gap:+.2f} dB against the server preview"
