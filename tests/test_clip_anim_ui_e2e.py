"""The Inspector's Animation section and the client draw of clip animations,
through the UI, in Chromium AND WebKit (wave E, F1).

* A main-track clip: the In tab's Zoom In tile is clicked; then, KEYBOARD
  only, the tabs are walked to Out, the preset grid is entered, Fade Out is
  chosen with Space and the Out duration slider stepped with the arrows —
  every choice lands in the EDL as ONE set_animation op; the Combo tab's
  Rock replaces In and Out (CapCut).
* Each tile loops a preview of its preset (Web Animations) — and shows a
  still glyph instead when the viewer asks for reduced motion.
* The client draws what the export bakes: a sticker's Slide Left In moves
  its picture in the preview canvas by the plan's own travel at that frame,
  and an overlay (PIP) clip's Zoom In draws it at the plan's own scale.

Harness: VAE_A11Y_BASE_URL = a Vite dev server proxying /api to a backend
(test_frontend_a11y's fixture). Screenshots go to VAE_ANIM_SHOTS.
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from fractions import Fraction
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import frame_map_golden_lib as G  # noqa: E402
from test_frontend_a11y import base_url  # noqa: E402,F401  (fixture)
from test_speed_ui_e2e import _client, _edl, _open, _wait_edl, engine, pw  # noqa: E402,F401

from video_ai_editor.edl import clip_animations as A  # noqa: E402

try:
    from playwright.sync_api import expect
except ImportError:  # the fixture skips
    expect = None

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")
SHOTS = Path(os.environ.get("VAE_ANIM_SHOTS", "/tmp"))


@pytest.fixture(scope="module")
def bars(tmp_path_factory) -> Path:
    return G.make_bar_source(tmp_path_factory.mktemp("anim_bars") / "bars10.mp4",
                             G.SourceSpec(key="anim", sid=7, rate=Fraction(30), seconds=10.0))


@pytest.fixture(scope="module")
def red_png(tmp_path_factory) -> Path:
    from PIL import Image
    p = tmp_path_factory.mktemp("anim_png") / "red.png"
    Image.new("RGBA", (120, 120), (255, 0, 0, 255)).save(p)
    return p


def _clip(edl: dict, cid: str) -> dict:
    return next(c for t in edl["tracks"] for c in t["clips"] if c.get("id") == cid)


def _project(base_url, bars: Path, png: Path, name: str) -> tuple[str, str, str, str]:  # noqa: F811
    """v1: the bar source (0-10 s); a red sticker 1-4 s; an overlay clip of
    the bars on v2 at 5-8 s. Returns (sid, v1 id, sticker id, overlay id)."""
    with _client(base_url) as c:
        sid = c.post("/api/sessions", json={"name": name}).json()["id"]
        with bars.open("rb") as fh:
            r = c.post(f"/api/sessions/{sid}/upload", files={"file": (bars.name, fh, "video/mp4")},
                       data={"add_to_timeline": "true", "transcribe": "false"})
        assert r.status_code in (200, 202), r.text
        src = r.json()["src"]
        deadline = time.time() + 120
        while True:
            edl = c.get(f"/api/sessions/{sid}/edl").json()
            v1 = next(t for t in edl["tracks"] if t["id"] == "v1")["clips"]
            if v1:
                break
            assert time.time() < deadline, "the import never reached the timeline"
            time.sleep(0.3)
        with png.open("rb") as fh:
            r = c.post(f"/api/sessions/{sid}/sticker_upload", files={"file": (png.name, fh, "image/png")},
                       data={"add_at_playhead": "true", "playhead": "1.0"})
        assert r.status_code == 200, r.text
        sticker = r.json()["sticker_id"]
        r = c.post(f"/api/sessions/{sid}/dispatch", json={"tool": "add_clip", "args": {
            "track": "v2", "src": src, "in": 1.0, "out": 4.0, "start": 5.0}})
        assert r.status_code == 200, r.text
        overlay = r.json()["result"]["clip_id"]
    return sid, v1[0]["id"], sticker, overlay


def _select(page, cid: str) -> None:
    page.evaluate("id => window.__vaeTest.useStore.getState().setSelection(id)", cid)
    page.locator(f'.in-clip[data-clip-id="{cid}"]').wait_for()
    # The clip inspector (2026-10-02 shell) keeps the Animation section on its own tab.
    page.locator(".in-tabs").get_by_role("tab", name="Animation", exact=True).click()
    page.locator(f'.props[data-clip-id="{cid}"]').wait_for()


def _playhead(page, t: float) -> None:
    page.evaluate("t => window.__vaeTest.useStore.getState().setPlayhead(t)", t)
    # the layer's clock is the preview element's presented time: wait for it
    deadline = time.time() + 15
    while time.time() < deadline:
        got = page.evaluate("""() => { const v = document.querySelector('video');
          return v ? v.currentTime : window.__vaeTest.useStore.getState().playhead }""")
        if abs(got - t) < 0.02:
            break
        page.wait_for_timeout(100)
    page.wait_for_timeout(400)


_CANVAS_STATS = """([mode]) => {
  const cv = document.querySelector('canvas[data-layer="stickers"]')
  const ctx = cv.getContext('2d')
  const { width: w, height: h } = cv
  const d = ctx.getImageData(0, 0, w, h).data
  let n = 0, sx = 0, sy = 0, x0 = w, x1 = -1, y0 = h, y1 = -1
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    const i = (y * w + x) * 4
    const hit = mode === 'red' ? (d[i] > 180 && d[i + 1] < 90 && d[i + 2] < 90 && d[i + 3] > 180) : d[i + 3] > 200
    if (!hit) continue
    n++; sx += x; sy += y
    if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y
  }
  const dpr = w / Math.max(1, cv.clientWidth)
  return { n, cx: n ? sx / n / dpr : null, cy: n ? sy / n / dpr : null, w: (x1 - x0 + 1) / dpr,
           h: (y1 - y0 + 1) / dpr, cssW: cv.clientWidth, cssH: cv.clientHeight }
}"""


def test_animation_section_by_mouse_and_keyboard(engine, base_url, bars, red_png):  # noqa: F811
    sid, v1, _st, _ov = _project(base_url, bars, red_png, f"anim-ui-{engine.engine_name}")
    page = _open(engine, base_url, sid, 1440, 900)
    try:
        _select(page, v1)
        tabs = page.get_by_role("tablist", name="Animation")
        tabs.scroll_into_view_if_needed()
        expect_tab = page.get_by_role("tab", name="In", exact=True)
        assert expect_tab.get_attribute("aria-selected") == "true"
        page.get_by_role("radiogroup", name="In animation").get_by_role("radio", name="Zoom In", exact=True).click()
        _wait_edl(base_url, sid, lambda e: _clip(e, v1).get("anim_in") == "zoom_in", "Zoom In")
        section = page.locator(".anim-section")
        section.screenshot(path=str(SHOTS / f"anim-section-in-{engine.engine_name}.png"))

        # keyboard only from here: the tabs, the grid, the slider
        page.get_by_role("tab", name="In (Zoom In)").focus()
        page.keyboard.press("ArrowRight")
        out_tab = page.get_by_role("tab", name="Out", exact=True)
        assert out_tab.get_attribute("aria-selected") == "true"
        assert page.evaluate("document.activeElement.dataset.animTab") == "out"
        page.keyboard.press("Tab")                       # into the grid: None is the focus stop
        assert page.evaluate("document.activeElement.dataset.anim") == "none"
        page.keyboard.press("ArrowRight")                # Fade Out
        assert page.evaluate("document.activeElement.dataset.anim") == "fade_out"
        page.keyboard.press("Space")
        edl = _wait_edl(base_url, sid, lambda e: _clip(e, v1).get("anim_out") == "fade_out", "Fade Out")
        assert _clip(edl, v1).get("anim_in") == "zoom_in"
        slider = page.get_by_role("slider", name="Out duration")
        slider.focus()
        for _ in range(3):
            page.keyboard.press("ArrowRight")
        _wait_edl(base_url, sid, lambda e: abs((_clip(e, v1).get("anim_out_dur") or 0) - 0.8) < 1e-6,
                  "Out duration 0.8 s")
        section.screenshot(path=str(SHOTS / f"anim-section-out-{engine.engine_name}.png"))

        page.get_by_role("tab", name="Combo").click()
        page.get_by_role("radiogroup", name="Combo animation").get_by_role("radio", name="Rock", exact=True).click()
        edl = _wait_edl(base_url, sid, lambda e: _clip(e, v1).get("anim_combo") == "rock", "Rock")
        assert _clip(edl, v1).get("anim_in") is None and _clip(edl, v1).get("anim_out") is None
        expect(page.get_by_role("radio", name="Rock", exact=True)).to_have_attribute("aria-checked", "true")

        # one op per choice: Zoom In, Fade Out, the duration, Rock
        with _client(base_url) as c:
            ops = c.get(f"/api/sessions/{sid}/ops").json()
        tools = [o.get("tool") for o in (ops.get("ops") if isinstance(ops, dict) else ops)]
        assert tools.count("set_animation") == 4, tools

        # each tile loops a preview of its preset
        running = page.evaluate("""() => [...document.querySelectorAll('[data-anim-preview]')]
          .map((el) => el.getAnimations().filter((a) => a.playState === 'running').length)""")
        assert len(running) == len(A.COMBO_PRESETS) and all(n >= 1 for n in running), running
        section.screenshot(path=str(SHOTS / f"anim-section-combo-{engine.engine_name}.png"))
    finally:
        page.context.close()


def test_reduced_motion_shows_still_glyphs(engine, base_url, bars, red_png):  # noqa: F811
    sid, v1, _st, _ov = _project(base_url, bars, red_png, f"anim-rm-{engine.engine_name}")
    ctx = engine.new_context(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
    ctx.add_init_script(f"try {{ localStorage.setItem('vai.sessionId', {sid!r}); "
                        "localStorage.setItem('vai.rightTab', 'inspect') } catch (e) {}")
    page = ctx.new_page()
    try:
        page.goto(base_url + "/?vae-test")
        page.locator(".timeline-canvas-wrap canvas").first.wait_for()
        page.wait_for_timeout(800)
        _select(page, v1)
        grid = page.get_by_role("radiogroup", name="In animation")
        grid.wait_for()
        assert page.locator("[data-anim-preview]").count() == 0
        assert grid.locator(".anim-tile svg").count() == len(A.IN_PRESETS) + 1
        page.locator(".anim-section").screenshot(path=str(SHOTS / f"anim-section-reduced-{engine.engine_name}.png"))
    finally:
        ctx.close()


def test_client_draws_the_sticker_and_overlay_animation(engine, base_url, bars, red_png):  # noqa: F811
    sid, _v1, sticker, overlay = _project(base_url, bars, red_png, f"anim-draw-{engine.engine_name}")
    with _client(base_url) as c:
        for cid, args in ((sticker, {"in": "slide_left", "in_duration": 0.5}),
                          (overlay, {"in": "zoom_in", "in_duration": 1.0})):
            r = c.post(f"/api/sessions/{sid}/dispatch", json={"tool": "set_animation", "args": {"clip_id": cid, **args}})
            assert r.status_code == 200, r.text
    page = _open(engine, base_url, sid, 1440, 900)
    try:
        # the sticker (1-4 s): at rest, then 0.4 s into its 0.5 s Slide Left
        _playhead(page, 2.5)
        rest = page.evaluate(_CANVAS_STATS, ["red"])
        assert rest["n"] > 50, rest
        _playhead(page, 1.4)
        moving = page.evaluate(_CANVAS_STATS, ["red"])
        pl = A.plan("slide_left", None, None, 0.5, None, 3.0)
        want_dx = pl.value("x", 0.4) * rest["cssW"]
        assert want_dx > 10, want_dx
        assert abs((moving["cx"] - rest["cx"]) - want_dx) <= 2.0, (moving, rest, want_dx)
        assert abs(moving["cy"] - rest["cy"]) <= 1.0, (moving, rest)
        page.screenshot(path=str(SHOTS / f"anim-sticker-slide-{engine.engine_name}.png"))

        # the overlay (5-8 s): its drawn width at rest vs 0.5 s into a 1 s Zoom In
        _playhead(page, 7.0)
        rest = page.evaluate(_CANVAS_STATS, ["alpha"])
        assert rest["n"] > 200, rest
        _playhead(page, 5.5)
        zoom = page.evaluate(_CANVAS_STATS, ["alpha"])
        want = A.plan("zoom_in", None, None, 1.0, None, 3.0).value("scale", 0.5)
        got = zoom["w"] / rest["w"]
        assert abs(got - want) <= 0.03, (got, want, zoom, rest)
        assert abs(zoom["h"] / rest["h"] - want) <= 0.03, (zoom, rest)
        page.screenshot(path=str(SHOTS / f"anim-overlay-zoom-{engine.engine_name}.png"))
    finally:
        page.context.close()
