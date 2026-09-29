"""Review RE (wave E fixer): the UI fixes, in Chromium AND WebKit.

* The Canvas section follows the clip's stored background (Undo left it on
  "Blur" with its strength row while the clip had none).
* A refused canvas picture shows the server's reason, not "400 Bad Request".
* The Effects panel's Flip H is the Inspector's Mirror (one model): a clip
  mirrored by the legacy effect shows the Flip button pressed, and a press
  unmirrors it for real.
* The Voice effects section says a picture-only clip has no sound.
* The Animation section's looping previews stop when the viewer switches to
  Reduce motion WHILE it is open.
* The Inspector has a jump list, and "Background" / "Voice changer" name the
  CapCut sections.
* An overlay (PiP) is drawn colour-exact in WebKit: a 64/128/192 grey-band
  overlay reads back the export's decode (it read 64/142/211 through
  WebKit's colour-managed drawImage).
* The preview shows an "≈" chip on an APPROX frame (Spin In, a voice effect)
  and none on an EXACT one.
* A loudness gain the server has not measured yet is no "≈ Loudness" chip
  (K2, 0.8.0 QA: every fresh project showed one): it is in the preview's
  telemetry, the render that measures it is asked for at once, and a voice
  effect's chip names only the voice effect meanwhile.

Harness: VAE_A11Y_BASE_URL = a Vite dev server proxying /api to a backend
(test_frontend_a11y's fixture). Screenshots go to VAE_RE_SHOTS.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_frontend_a11y import base_url  # noqa: E402,F401  (fixture)
from test_speed_ui_e2e import _client, _edl, _open, engine, pw  # noqa: E402,F401

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")
SHOTS = Path(os.environ.get("VAE_RE_SHOTS", "/tmp"))


def _ff(dst: Path, *args: str) -> Path:
    if not dst.exists():
        subprocess.run(["ffmpeg", "-y", "-v", "error", *args, str(dst)], check=True, capture_output=True)
    return dst


@pytest.fixture(scope="module")
def media(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("re_ui_media")
    main = _ff(d / "main.mp4", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=4",
               "-f", "lavfi", "-i", "sine=f=330:sample_rate=48000:duration=4",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest")
    silent = _ff(d / "silent.mp4", "-f", "lavfi", "-i", "color=c=0x808080:s=320x320:r=30:d=4",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an")
    # 64 / 128 / 192 luma bands, untagged — what WebKit's drawImage brightened
    bands = _ff(d / "bands.mp4", "-f", "lavfi", "-i", "color=c=black:s=384x128:r=30:d=4",
                "-vf", "geq=lum='if(lt(X\\,128)\\,64\\,if(lt(X\\,256)\\,128\\,192))':cb=128:cr=128,format=yuv420p",
                "-c:v", "libx264", "-crf", "2", "-pix_fmt", "yuv420p")
    return {"main": main, "silent": silent, "bands": bands}


def _dispatch(c, sid: str, tool: str, args: dict) -> dict:
    r = c.post(f"/api/sessions/{sid}/dispatch", json={"tool": tool, "args": args})
    assert r.status_code == 200, r.text
    return r.json()


def _track(edl: dict, tid: str) -> list[dict]:
    return next(t for t in edl["tracks"] if t["id"] == tid)["clips"]


def _upload(c, sid: str, path: Path, add: bool) -> str:
    with path.open("rb") as fh:
        r = c.post(f"/api/sessions/{sid}/upload", files={"file": (path.name, fh, "video/mp4")},
                   data={"add_to_timeline": "true" if add else "false", "transcribe": "false"})
    assert r.status_code in (200, 202), r.text
    return r.json()["src"]


def _project(base_url, media, name: str, portrait: bool = True) -> tuple[str, str, dict]:  # noqa: F811
    with _client(base_url) as c:
        sid = c.post("/api/sessions", json={"name": name}).json()["id"]
        srcs = {"main": _upload(c, sid, media["main"], True)}
        for k in ("silent", "bands"):
            srcs[k] = _upload(c, sid, media[k], False)
        deadline = time.time() + 120
        while not _track(c.get(f"/api/sessions/{sid}/edl").json(), "v1"):
            assert time.time() < deadline, "the import never reached the timeline"
            time.sleep(0.3)
        if portrait:
            _dispatch(c, sid, "set_canvas", {"w": 720, "h": 1280})
        v1 = _track(c.get(f"/api/sessions/{sid}/edl").json(), "v1")[0]["id"]
    return sid, v1, srcs


def _select(page, clip_id: str) -> None:
    page.evaluate("id => window.__vaeTest.useStore.getState().setSelection(id)", clip_id)
    page.locator(f".props[data-clip-id='{clip_id}']").wait_for(timeout=10000)


def _undo(page) -> None:
    page.evaluate("() => window.__vaeTest.useStore.getState().dispatch('undo', {})")


def _clip(base_url, sid, cid) -> dict:
    return next(c for t in _edl(base_url, sid)["tracks"] for c in t["clips"] if c.get("id") == cid)


def _wait(fn, what: str, timeout: float = 15.0):
    deadline = time.time() + timeout
    while True:
        v = fn()
        if v:
            return v
        assert time.time() < deadline, f"timed out: {what}"
        time.sleep(0.2)


# ---------------------------------------------------------------- Canvas section

def test_the_canvas_section_follows_undo_and_shows_the_servers_reason(engine, base_url, media, tmp_path):  # noqa: F811
    name = engine.engine_name
    sid, v1, _ = _project(base_url, media, f"re-canvas-{name}")
    page = _open(engine, base_url, sid, 1400, 900)
    try:
        _select(page, v1)
        group = page.get_by_role("radiogroup", name="Canvas background")
        group.get_by_role("radio", name="Blur").click()
        _wait(lambda: (_clip(base_url, sid, v1).get("canvas_bg") or {}).get("type") == "blur", "blur set")
        page.get_by_role("radiogroup", name="Blur strength").get_by_role("radio", name="Strong").click()
        _wait(lambda: (_clip(base_url, sid, v1).get("canvas_bg") or {}).get("blur") == 3, "strong")
        _undo(page)
        _undo(page)
        _wait(lambda: _clip(base_url, sid, v1).get("canvas_bg") is None, "undone")
        page.wait_for_timeout(500)
        # review RE: it stayed on Blur (and the strength row) after the undo
        assert group.get_by_role("radio", name="None").get_attribute("aria-checked") == "true"
        assert page.get_by_role("radiogroup", name="Blur strength").count() == 0
        page.screenshot(path=str(SHOTS / f"re_canvas_undo_{name}.png"))
        # a refused picture: the server's reason, never "400 Bad Request"
        fake = tmp_path / "fake.png"
        fake.write_bytes(b"not a picture at all")
        with page.expect_file_chooser() as fc:
            group.get_by_role("radio", name="Image").click()
        fc.value.set_files(str(fake))
        alert = page.locator(".canvas-section [role=alert]")
        alert.wait_for(timeout=10000)
        txt = alert.inner_text()
        assert "could not be read as a picture" in txt and "400" not in txt, txt
    finally:
        page.context.close()


# ---------------------------------------------------------------- one mirror model

def test_the_effects_panel_flip_is_the_inspector_mirror(engine, base_url, media):  # noqa: F811
    name = engine.engine_name
    sid, v1, _ = _project(base_url, media, f"re-flip-{name}", portrait=False)
    with _client(base_url) as c:        # a project mirrored by the LEGACY effect
        _dispatch(c, sid, "add_effect", {"clip_id": v1, "type": "hflip", "params": {}})
    page = _open(engine, base_url, sid, 1400, 900)
    try:
        _select(page, v1)
        btn = page.get_by_role("button", name="Flip horizontal")
        assert btn.get_attribute("aria-pressed") == "true", "a clip mirrored by the effect reads as mirrored"
        btn.click()
        c = _wait(lambda: (lambda x: x if not any(e["type"] == "hflip" for e in x.get("effects") or []) else None)(
            _clip(base_url, sid, v1)), "the legacy effect folded away")
        assert not (c.get("transform") or {}).get("flip_h"), c.get("transform")
        _wait(lambda: btn.get_attribute("aria-pressed") == "false", "the button follows")
    finally:
        page.context.close()


# ---------------------------------------------------------------- Voice effects on a silent clip

def test_the_voice_section_says_a_picture_only_clip_has_no_sound(engine, base_url, media):  # noqa: F811
    name = engine.engine_name
    sid, _v1, srcs = _project(base_url, media, f"re-silent-{name}")
    with _client(base_url) as c:
        cid = _dispatch(c, sid, "add_clip", {"track": "v2", "src": srcs["silent"], "in": 0, "out": 2,
                                             "start": 0})["result"]["clip_id"]
        # and the op itself refuses it
        r = c.post(f"/api/sessions/{sid}/dispatch", json={"tool": "set_voice_effect",
                                                          "args": {"clip_id": cid, "effect": "robot"}})
        assert r.status_code == 400 and "no sound" in r.text, r.text
    page = _open(engine, base_url, sid, 1400, 900)
    try:
        _select(page, cid)
        note = page.locator("[data-voice='silent']")
        note.wait_for(timeout=10000)
        assert "no sound" in note.inner_text()
        assert page.get_by_role("radiogroup", name="Voice effect").count() == 0
    finally:
        page.context.close()


# ---------------------------------------------------------------- reduced motion, live

def test_animation_previews_stop_when_reduce_motion_turns_on(engine, base_url, media):  # noqa: F811
    name = engine.engine_name
    sid, v1, _ = _project(base_url, media, f"re-motion-{name}")
    page = _open(engine, base_url, sid, 1400, 900)
    try:
        _select(page, v1)
        page.locator("[data-anim-preview]").first.wait_for(timeout=10000)
        running = "() => document.getAnimations().filter(a => a.playState === 'running').length"
        assert page.evaluate(running) > 0
        page.emulate_media(reduced_motion="reduce")
        _wait(lambda: page.evaluate(running) == 0, "the loops stop")
        assert page.locator("[data-anim-preview]").count() == 0
        page.emulate_media(reduced_motion="no-preference")
        _wait(lambda: page.evaluate(running) > 0, "and start again")
    finally:
        page.context.close()


# ---------------------------------------------------------------- the jump list

def test_the_inspector_has_a_jump_list_and_capcut_names(engine, base_url, media):  # noqa: F811
    name = engine.engine_name
    sid, v1, _ = _project(base_url, media, f"re-index-{name}")
    page = _open(engine, base_url, sid, 1440, 900)
    try:
        _select(page, v1)
        nav = page.get_by_role("navigation", name="Jump to an Inspector section")
        nav.wait_for()
        assert page.locator("[data-section='Canvas'] .section-aka").inner_text().strip() == "· Background"
        assert page.locator("[data-section='Voice effects'] .section-aka").inner_text().strip() == "· Voice changer"
        # keyboard only: Tab to the Canvas chip, Enter: the section is in view and focused
        chip = nav.get_by_role("button", name="Jump to Canvas")
        chip.focus()
        page.keyboard.press("Enter")
        page.wait_for_timeout(700)
        assert page.evaluate("() => document.activeElement?.dataset?.section") == "Canvas"
        box = page.locator("[data-section='Canvas']").bounding_box()
        body = page.locator(".right-body").bounding_box()
        assert box and body and body["y"] - 2 <= box["y"] <= body["y"] + body["height"] / 2, (box, body)
        page.screenshot(path=str(SHOTS / f"re_index_{name}.png"))
    finally:
        page.context.close()


# ---------------------------------------------------------------- PiP colour in WebKit

_BANDS = """() => {
  const cv = document.querySelector('canvas[data-layer="stickers"]')
  const layers = [...document.querySelectorAll('[data-pip-blend-layer]')].filter((c) => c.style.display !== 'none')
  const src = layers.length ? layers[0] : cv
  const g = src.getContext('2d')
  const { width: w, height: h } = src
  const d = g.getImageData(0, 0, w, h).data
  // the drawn overlay: opaque pixels; its middle row, left to right
  let y0 = h, y1 = -1
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x += 4) if (d[(y * w + x) * 4 + 3] > 250) { y0 = Math.min(y0, y); y1 = Math.max(y1, y) }
  if (y1 < 0) return null
  const y = (y0 + y1) >> 1
  const row = []
  for (let x = 0; x < w; x++) { const i = (y * w + x) * 4; if (d[i + 3] > 250) row.push(d[i + 1]) }
  return row
}"""


def _preview_mode(page, mode: str) -> bool:
    page.evaluate("""(m) => fetch('/api/settings/preview', {method: 'PUT',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify({engine: m})})""", mode)
    page.reload()
    page.locator(".timeline-canvas-wrap canvas").first.wait_for()
    page.wait_for_timeout(1800)
    return page.locator('[data-preview-engine="client"]').count() > 0


def _export_bands(base_url, sid) -> list[int]:
    """The export's decode of the three bands (green channel, BT.709 decode)."""
    with _client(base_url) as c:
        r = c.post(f"/api/sessions/{sid}/export", json={"height": 720})     # the short side: 720x1280
        assert r.status_code == 200, r.text
        path = r.json()["path"]
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", "1.0", "-i", path, "-frames:v", "1", "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    a = np.frombuffer(raw, np.uint8).reshape(1280, 720, 3)
    row = a[640, :, 1].astype(int)
    return sorted({int(np.median(row[(row > lo) & (row <= hi)])) for lo, hi in ((20, 95), (95, 165), (165, 250))})


def test_an_overlay_is_drawn_colour_exact_in_both_preview_modes(engine, base_url, media):  # noqa: F811
    """review RE: WebKit's drawImage(<video>) colour-manages an untagged
    video through a ~1.96 gamma: the grey bands read 64/142/211 where the
    export decodes 56/130/205. PiP frames go through a WebGL copy there."""
    name = engine.engine_name
    sid, _v1, srcs = _project(base_url, media, f"re-bands-{name}")
    with _client(base_url) as c:
        cid = _dispatch(c, sid, "add_clip", {"track": "v2", "src": srcs["bands"], "in": 0, "out": 3.5,
                                             "start": 0})["result"]["clip_id"]
        _dispatch(c, sid, "set_clip_transform", {"clip_id": cid, "x": 360, "y": 640, "scale": 2.0})
        _dispatch(c, sid, "set_canvas_background", {"all": True, "type": "color", "color": "#000000"})
    want = _export_bands(base_url, sid)
    assert len(want) == 3 and abs(want[1] - 130) <= 3, want
    page = _open(engine, base_url, sid, 1400, 900)
    try:
        for mode in ("client", "server"):
            got_mode = _preview_mode(page, mode)
            if mode == "client" and not got_mode:
                continue
            page.evaluate("t => window.__vaeTest.useStore.getState().setPlayhead(t)", 1.0)
            page.wait_for_timeout(1500)
            row = _wait(lambda: page.evaluate(_BANDS), "the overlay is drawn")
            r = np.array(row)
            got = sorted({int(np.median(r[(r > lo) & (r <= hi)])) for lo, hi in ((20, 95), (95, 165), (165, 250))
                          if ((r > lo) & (r <= hi)).sum() > 10})
            page.screenshot(path=str(SHOTS / f"re_bands_{name}_{mode}.png"))
            assert len(got) == 3 and max(abs(a - b) for a, b in zip(got, want)) <= 3, (name, mode, got, want)
    finally:
        page.evaluate("""() => fetch('/api/settings/preview', {method: 'PUT',
            headers: {'Content-Type': 'application/json'}, body: JSON.stringify({engine: 'server'})})""")
        page.context.close()


# ---------------------------------------------------------------- the ≈ chip

def test_an_approx_frame_shows_the_chip_and_an_exact_one_does_not(engine, base_url, media):  # noqa: F811
    name = engine.engine_name
    sid, v1, _ = _project(base_url, media, f"re-chip-{name}", portrait=False)
    page = _open(engine, base_url, sid, 1400, 900)
    try:
        if not _preview_mode(page, "client"):
            pytest.skip(f"no client preview in Playwright {name}")
        chip = page.locator("[data-fidelity='approx']")

        def at(t: float):
            page.evaluate("t => window.__vaeTest.useStore.getState().setPlayhead(t)", t)
            page.wait_for_timeout(1200)

        with _client(base_url) as c:
            _dispatch(c, sid, "set_animation", {"clip_id": v1, "in": "slide_left"})
        at(0.1)
        assert chip.count() == 0, "an EXACT preset shows no chip"
        with _client(base_url) as c:
            _dispatch(c, sid, "set_animation", {"clip_id": v1, "in": "spin"})
        at(0.1)
        chip.wait_for(timeout=10000)
        assert "Spin In animation" in (chip.get_attribute("aria-label") or "")
        page.screenshot(path=str(SHOTS / f"re_chip_spin_{name}.png"))
        with _client(base_url) as c:
            _dispatch(c, sid, "set_animation", {"clip_id": v1, "in": "none"})
            _dispatch(c, sid, "set_voice_effect", {"clip_id": v1, "effect": "deep"})
        at(1.0)
        chip.wait_for(timeout=10000)
        assert "Voice effect: Deep" in (chip.get_attribute("aria-label") or "")
        with _client(base_url) as c:
            _dispatch(c, sid, "set_voice_effect", {"clip_id": v1, "effect": "echo"})   # EXACT
        at(1.0)
        _wait(lambda: chip.count() == 0, "the chip goes with the APPROX effect")
    finally:
        page.evaluate("""() => fetch('/api/settings/preview', {method: 'PUT',
            headers: {'Content-Type': 'application/json'}, body: JSON.stringify({engine: 'server'})})""")
        page.context.close()


# K2 (0.8.0 QA): a not-yet-measured loudness gain is not a chip.
_RENDER_RE = re.compile(r".*/api/sessions/[^/]+/preview(\?.*)?$")
_RENDER_TIMES = r"""
(() => {
  window.__renders = []
  const orig = window.fetch
  window.fetch = function (input, init) {
    const url = typeof input === 'string' ? input : input.url
    if (/\/api\/sessions\/[^/]+\/preview(\?|$)/.test(url) && (init?.method ?? 'GET') === 'POST') {
      window.__renders.push(performance.now())
    }
    return orig.apply(this, arguments)
  }
})()
"""
_LOUDNESS = "() => window.__vaeTest.useStore.getState().clientView?.loudness ?? null"


def test_a_loudness_gain_not_measured_yet_shows_no_chip_and_is_measured_at_once(engine, base_url, media):  # noqa: F811
    name = engine.engine_name
    sid, v1, _ = _project(base_url, media, f"re-loud-{name}", portrait=False)
    page = _open(engine, base_url, sid, 1400, 900)
    page.context.add_init_script(_RENDER_TIMES)
    held = []
    hold = {"on": True}
    try:
        if not _preview_mode(page, "client"):
            pytest.skip(f"no client preview in Playwright {name}")
        chip = page.locator("[data-fidelity='approx']")
        # the server render that measures the gain is held: it stays unmeasured
        page.route(_RENDER_RE, lambda route: held.append(route) if hold["on"] else route.continue_())

        def edit(tool: str, args: dict) -> float:
            """Dispatch through the app's store; the page clock at the edit."""
            return page.evaluate("""async ([tool, args]) => {
                const s = window.__vaeTest.useStore.getState(); const t = performance.now()
                await s.dispatch(tool, args); return t }""", [tool, args])

        def at(t: float):
            page.evaluate("t => window.__vaeTest.useStore.getState().setPlayhead(t)", t)
            page.wait_for_timeout(1200)

        t_edit = edit("set_animation", {"clip_id": v1, "in": "slide_left"})       # EXACT; a new sound key
        at(0.1)
        page.wait_for_function(f"() => ({_LOUDNESS})() === 'pending'", timeout=10000)
        assert chip.count() == 0, ("a loudness gain not measured yet shows no chip",
                                   chip.first.get_attribute("aria-label"),
                                   page.evaluate("() => window.__vaeTest.useStore.getState().clientView"))
        # …and the render that measures it is asked for at once, not at the
        # 1.5 s idle cadence
        page.wait_for_function("t => window.__renders.some((r) => r >= t)", arg=t_edit, timeout=10000)
        first = page.evaluate("t => Math.min(...window.__renders.filter((r) => r >= t))", t_edit)
        assert first - t_edit < 1200, f"the render that measures the gain waited {first - t_edit:.0f} ms"

        edit("set_voice_effect", {"clip_id": v1, "effect": "deep"})
        at(1.0)
        chip.wait_for(timeout=10000)
        assert page.evaluate(_LOUDNESS) == "pending"
        label = chip.get_attribute("aria-label") or ""
        # each reason keeps its own words; none of them is the unmeasured gain
        # (a voice effect's sound is unbounded for the limiter bound, gate RX,
        # so "Limiter on loud sound" may stand beside it)
        assert "Voice effect: Deep" in label and "Loudness" not in label, label

        hold["on"] = False
        while held:
            held.pop(0).continue_()
        page.wait_for_function(f"() => ({_LOUDNESS})() === 'measured'", timeout=60000)
        at(1.0)
        assert "Loudness" not in (chip.get_attribute("aria-label") or ""), "the measured gain plays: no loudness chip"
        page.screenshot(path=str(SHOTS / f"re_loud_{name}.png"))
    finally:
        hold["on"] = False
        for r in held:
            try:
                r.continue_()
            except Exception:  # noqa: BLE001 — the page may be gone
                pass
        page.evaluate("""() => fetch('/api/settings/preview', {method: 'PUT',
            headers: {'Content-Type': 'application/json'}, body: JSON.stringify({engine: 'server'})})""")
        page.context.close()
