"""An audio-only source plays in Instant preview, in Chromium AND WebKit
(INSTANT_PREVIEW_SPEC §3.6, §7; final sweep 3, HIGH).

The finding: a music bed uploaded as an audio file (no picture) played SILENT
in Instant preview while the server preview had it at full level, and every
frame still read EXACT with no ≈ chip. The client waited for the bed's proxy
to report frames > 0, which an audio-only proxy never does, so the bed never
got a proxy key and the sound engine took it for silence.

Measured here on the LIVE sink (the loudness test's ScriptProcessor tap, but
keeping the samples): the 660 Hz bed and the 440 Hz v1 tone, each against the
server preview file of the same timeline. Spec bar: 0.5 dB.

Once the bed played, it played up to +6.7 dB for its first second or two: its
fade-in curve, written after the context had passed the block's start, was
moved by Chromium onto the next block's first event, which threw -- after the
block's source had started but before the clip recorded it, so every refill
scheduled that block again (mixGraph.setLiveCurve, mixGraphLateCurve.test.ts).
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
SR = 48000
V1_HZ, BED_HZ = 440.0, 660.0
TAP = r"""
(() => {
  window.__cap = { L: [], on: false }
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
            if (!window.__cap.on) return
            window.__cap.L.push(Array.from(e.inputBuffer.getChannelData(0)))
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


def _video(p: Path) -> Path:
    subprocess.run(["ffmpeg", "-y", "-v", "error",
                    "-f", "lavfi", "-i", f"testsrc2=s=320x180:d={DUR}:r=30",
                    "-f", "lavfi", "-i", f"sine=frequency={V1_HZ:g}:duration={DUR}:sample_rate={SR}",
                    "-map", "0:v", "-map", "1:a", "-shortest", "-pix_fmt", "yuv420p",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-b:a", "192k", str(p)],
                   check=True, capture_output=True)
    return p


def _bed(p: Path) -> Path:
    subprocess.run(["ffmpeg", "-y", "-v", "error",
                    "-f", "lavfi", "-i", f"sine=frequency={BED_HZ:g}:duration={DUR}:sample_rate={SR}",
                    "-c:a", "aac", "-b:a", "192k", str(p)], check=True, capture_output=True)
    return p


def _band_db(x: np.ndarray, hz: float) -> float:
    """Mean power (dB) of a ±8 Hz band, Hann-windowed 8192-point frames."""
    n = 8192
    w = np.hanning(n)
    frames = [x[i:i + n] * w for i in range(0, len(x) - n, n // 2)]
    assert frames, "no sound captured"
    spec = np.mean([np.abs(np.fft.rfft(f)) ** 2 for f in frames], axis=0)
    f = np.fft.rfftfreq(n, 1 / SR)
    band = (f >= hz - 8) & (f <= hz + 8)
    return float(10 * math.log10(spec[band].sum() + 1e-30))


def _pcm(path: str, t0: float, t1: float) -> np.ndarray:
    pcm = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", str(t0), "-t", str(t1 - t0), "-i", path,
                          "-map", "0:a:0", "-af", "pan=mono|c0=c0", "-ar", str(SR), "-f", "f32le", "pipe:1"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(pcm, dtype=np.float32).astype(np.float64)


@pytest.fixture(scope="module")
def session(base_url, tmp_path_factory):  # noqa: F811
    d = tmp_path_factory.mktemp("bed")
    vid, bed = _video(d / "v8.mp4"), _bed(d / "bed.m4a")
    with httpx.Client(base_url=base_url, timeout=300) as c:
        sid = c.post("/api/sessions", json={"name": "instant audio-only bed"}).json()["id"]
        with vid.open("rb") as fh:
            r = c.post(f"/api/sessions/{sid}/upload", files={"file": ("v8.mp4", fh, "video/mp4")},
                       data={"add_to_timeline": "true", "transcribe": "false"})
        assert r.status_code in (200, 202), r.text
        deadline = time.time() + 120
        while time.time() < deadline:
            edl = c.get(f"/api/sessions/{sid}/edl").json()
            if any(t["id"] == "v1" and t["clips"] for t in edl["tracks"]):
                break
            time.sleep(0.3)
        with bed.open("rb") as fh:
            r = c.post(f"/api/sessions/{sid}/audio_upload", files={"file": ("bed.m4a", fh, "audio/mp4")},
                       data={"add_to_music": "true", "duck": "false", "volume_db": "0"})
        assert r.status_code == 200, r.text
        edl = c.get(f"/api/sessions/{sid}/edl").json()
        music = next(t for t in edl["tracks"] if t["id"] == "music")
        assert music["clips"] and not music.get("duck"), music
    return sid


def _set_engine(base_url, mode: str) -> None:  # noqa: F811
    r = httpx.put(f"{base_url}/api/settings/preview", json={"engine": mode}, timeout=10)
    assert r.status_code == 200, r.text


def test_an_audio_only_bed_plays_in_instant_preview(engine, base_url, session):  # noqa: F811
    sid = session
    _set_engine(base_url, "client")
    try:
        ctx = engine.new_context(viewport={"width": 1440, "height": 900})
        ctx.add_init_script(f"try {{ localStorage.setItem('vai.sessionId', {json.dumps(sid)}) }} catch (e) {{}}")
        ctx.add_init_script(TAP)
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base_url + "/?vae-test")
        page.locator(".timeline-canvas-wrap canvas").first.wait_for(timeout=60000)
        if page.locator('[data-preview-engine="client"]').count() == 0:
            page.wait_for_timeout(3000)
        if page.locator('[data-preview-engine="client"]').count() == 0:
            assert engine.engine_name != "chromium", "headless Chromium runs the client preview"
            pytest.skip(f"{engine.engine_name}: the client preview does not run here")
        # the bed's sound is known (no 'audio:pending') and the gain measured
        page.wait_for_function("""() => { const v = window.__vaeTest.useStore.getState().clientView
            return !!v && v.live && !v.wait && v.loudness === 'measured'
              && !v.reasons.includes('audio:pending') }""", timeout=90000)
        transport = page.locator(".pl-play")
        page.evaluate("window.__cap.on = true")
        transport.click()
        page.wait_for_timeout(4500)
        transport.click()
        chunks = page.evaluate("window.__cap.L")
        ctx.close()
        # a refused gain curve used to leave its block untracked and scheduled
        # again on top: the bed's fade-in played up to +6.7 dB (mixGraph.ts)
        assert not errors, errors
    finally:
        _set_engine(base_url, "server")
    live = np.concatenate([np.asarray(c, dtype=np.float64) for c in chunks]) if chunks else np.zeros(0)
    nz = np.flatnonzero(np.abs(live) > 1e-4)
    assert nz.size > SR * 3, f"{engine.engine_name}: it did not really play ({nz.size} samples)"
    live = live[nz[0] + SR // 4: nz[-1] - SR // 4]           # steady middle, no start/stop edges

    preview = httpx.post(f"{base_url}/api/sessions/{sid}/preview", timeout=600).json()["path"]
    server = _pcm(preview, 0.5, 4.5)
    gaps = {name: _band_db(live, hz) - _band_db(server, hz) for name, hz in (("bed", BED_HZ), ("v1", V1_HZ))}
    for name, gap in gaps.items():
        assert abs(gap) <= 0.5, f"{engine.engine_name}: {name} {gap:+.2f} dB against the server preview ({gaps})"
