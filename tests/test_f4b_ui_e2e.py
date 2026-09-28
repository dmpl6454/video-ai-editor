"""Wave E, lane F4b: the UI leftovers, in Chromium AND WebKit.

  * Mirror in the Inspector: "Flip horizontal" / "Flip vertical" toggle the
    clip's Transform.flip_h / flip_v (one `flip_clip` op, one undo step),
    keyboard-operable, state in aria-pressed.
  * A timeline EDGE drag on a speed-CURVE (Hero) clip lands on the frame
    under the pointer: the kept frames are exactly the frames the clip showed
    there before (the program map of the EDL before and after — the frame
    model every export golden is decoded against), for the right edge (tail
    trim) and the left edge (head trim).
  * Item 27: the timeline zoom −/+ steps stay reachable in a browser window
    below 1100 px (they used to drop out below a 522 px pane).
  * Item 26: Help says how Tab reaches every control in Safari (⌥Tab).

Harness: `test_frontend_a11y`'s server fixture (VAE_A11Y_BASE_URL = a Vite
dev server proxying /api to a backend). Screenshots to VAE_F4B_SHOTS.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_frontend_a11y import base_url  # noqa: E402,F401  (fixture)
from test_speed_ui_e2e import (  # noqa: E402,F401  (fixtures + helpers)
    LABEL_W, TimelineGeometry, _client, _edl, _expected, _footprint, _open, _project, _v1, _wait_edl, bars,
    engine, expect, pw,
)

from video_ai_editor.edl import timebase as tb  # noqa: E402
from video_ai_editor.edl.schema import Transform  # noqa: E402

SHOTS = Path(os.environ.get("VAE_F4B_SHOTS", "/tmp"))
FPS = 30


def _shot(page, name: str) -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / f"{name}.png"))


def _close(page) -> None:
    page.context.close()


def _dispatch(base_url, sid, tool, args) -> dict:  # noqa: F811
    with _client(base_url) as c:
        r = c.post(f"/api/sessions/{sid}/dispatch", json={"tool": tool, "args": args})
        assert r.status_code == 200, r.text
        return r.json()


# --------------------------------------------------------------------------- flip / mirror

@pytest.mark.skipif("flip_h" not in Transform.model_fields, reason="needs Transform.flip_h (lane F4a)")
def test_the_inspector_mirror_buttons_flip_the_clip_one_undo_step_each(engine, base_url, bars):  # noqa: F811
    name = engine.engine_name
    sid = _project(base_url, bars, f"flip ui {name}")
    target = _v1(_edl(base_url, sid))[1]["id"]
    page = _open(engine, base_url, sid, 1280, 800)
    try:
        TimelineGeometry(page).select(base_url, sid, target)
        h = page.get_by_role("button", name="Flip horizontal")
        v = page.get_by_role("button", name="Flip vertical")
        expect(h).to_have_attribute("aria-pressed", "false")
        expect(v).to_have_attribute("aria-pressed", "false")
        # a click …
        h.click()
        edl = _wait_edl(base_url, sid, lambda e: next(c for c in _v1(e) if c["id"] == target)
                        .get("transform", {}).get("flip_h") is True, "flip_h on")
        expect(h).to_have_attribute("aria-pressed", "true")
        others = [c.get("transform", {}) for c in _v1(edl) if c["id"] != target]
        assert not any(t.get("flip_h") or t.get("flip_v") for t in others), others
        # … and the keyboard: focus the vertical toggle, press Space
        v.focus()
        page.keyboard.press("Space")
        _wait_edl(base_url, sid, lambda e: next(c for c in _v1(e) if c["id"] == target)
                  .get("transform", {}).get("flip_v") is True, "flip_v on")
        expect(v).to_have_attribute("aria-pressed", "true")
        _shot(page, f"f4b-flip-inspector-{name}")
        # one press = one undo step: ⌘Z takes the vertical flip back only
        page.keyboard.press("Meta+z")
        edl = _wait_edl(base_url, sid, lambda e: not next(c for c in _v1(e) if c["id"] == target)
                        .get("transform", {}).get("flip_v"), "undo of flip_v")
        assert next(c for c in _v1(edl) if c["id"] == target)["transform"].get("flip_h") is True
    finally:
        _close(page)


# --------------------------------------------------------------------------- curve edge drag

def _hero_project(base_url, bars, name) -> tuple[str, dict]:  # noqa: F811
    sid = _project(base_url, bars, name)
    hero_id = _v1(_edl(base_url, sid))[2]["id"]
    _dispatch(base_url, sid, "set_speed", {"clip_id": hero_id, "preset": "hero"})
    edl = _edl(base_url, sid)
    return sid, next(c for c in _v1(edl) if c["id"] == hero_id)


#: Grab an edge this many px INSIDE the clip: at the seam itself the
#: neighbour's zone wins (lib/timelineHit: left of a seam trims the outgoing
#: clip's tail).
EDGE_INSET_PX = 3


def _drag_edge(page, geo: TimelineGeometry, base_url, sid, t_edge: float, inward: int, t_to: float) -> None:  # noqa: F811
    """Grab the clip edge at `t_edge` (`inward` = +1 for a head, -1 for a
    tail) and drag it to time `t_to` (after "Zoom to fit", snapping off, on
    the Main video row found by `select`)."""
    edl = _edl(base_url, sid)
    box = page.locator(".timeline-canvas-wrap canvas").first.bounding_box()
    zoom = max(40.0, box["width"] - LABEL_W - 24) / edl["duration"]
    y = geo.last_point[1]
    x0 = box["x"] + LABEL_W + t_edge * zoom + inward * EDGE_INSET_PX
    x1 = box["x"] + LABEL_W + t_to * zoom + inward * EDGE_INSET_PX
    page.mouse.move(x0, y)
    page.mouse.down()
    page.mouse.move((x0 + x1) / 2, y, steps=6)
    page.mouse.move(x1, y, steps=6)
    page.mouse.up()


def _snapping_off(page) -> None:
    snap = page.get_by_role("button", name="Snapping")
    if snap.get_attribute("aria-pressed") == "true":
        snap.click()


@pytest.mark.parametrize("side", ["right", "left"])
def test_an_edge_drag_on_a_hero_clip_lands_on_the_frame_under_the_pointer(engine, base_url, bars, side):  # noqa: F811
    name = engine.engine_name
    sid, hero = _hero_project(base_url, bars, f"curve edge {side} {name}")
    before = _edl(base_url, sid)
    exp_before = _expected(before)["top"]
    start, dur = hero["start"], _footprint(hero)
    f_start = tb.frame_of(start, FPS)
    page = _open(engine, base_url, sid, 1440, 900)
    try:
        geo = TimelineGeometry(page)
        geo.select(base_url, sid, hero["id"])
        _snapping_off(page)
        if side == "right":
            # the tail, 60 % into the clip: the ramp's slow middle, where the
            # mean speed and the curve disagree by frames
            pointer = start + 0.6 * dur
            _drag_edge(page, geo, base_url, sid, start + dur, -1, pointer)
        else:
            pointer = start + 0.35 * dur
            _drag_edge(page, geo, base_url, sid, start, +1, pointer)
        after = _wait_edl(base_url, sid, lambda e: abs(_footprint(next(c for c in _v1(e) if c["id"] == hero["id"]))
                                                       - dur) > 0.05, f"the {side} edge trim")
        _shot(page, f"f4b-curve-edge-{side}-{name}")
    finally:
        _close(page)
    clip = next(c for c in _v1(after) if c["id"] == hero["id"])
    exp_after = _expected(after)["top"]
    n_after = tb.frame_of(_footprint(clip), FPS)
    k_pointer = tb.frame_of(pointer, FPS)
    if side == "right":
        # the new end is the frame under the pointer (±1: the pointer's pixel)
        assert abs((f_start + n_after) - k_pointer) <= 1, (f_start, n_after, k_pointer)
        # and every kept frame is the one the clip showed there before
        assert exp_after[f_start:f_start + n_after] == exp_before[f_start:f_start + n_after]
    else:
        assert tb.frame_of(clip["start"], FPS) == f_start          # the Main video stays packed
        # the first kept frame is the frame that was under the pointer …
        k = exp_before.index(exp_after[f_start], f_start)
        assert abs(k - k_pointer) <= 1, (k, k_pointer)
        # … and the rest of the clip plays exactly as before from there
        assert exp_after[f_start:f_start + n_after] == exp_before[k:k + n_after]


# --------------------------------------------------------------------------- items 26 and 27

@pytest.mark.parametrize("width", [900, 1024, 1099])
def test_the_zoom_steps_stay_reachable_below_1100px(engine, base_url, bars, width):  # noqa: F811
    name = engine.engine_name
    sid = _project(base_url, bars, f"zoom steps {width} {name}")
    page = _open(engine, base_url, sid, width, 760)
    try:
        bar = page.locator(".timeline-toolbar").bounding_box()
        for label in ("Zoom out", "Zoom in", "Zoom to fit"):
            btn = page.get_by_role("button", name=label)
            expect(btn).to_be_visible()
            bb = btn.bounding_box()
            assert bar["x"] - 0.5 <= bb["x"] and bb["x"] + bb["width"] <= bar["x"] + bar["width"] + 0.5, (label, bb, bar)
            assert bb["width"] >= 24 and bb["height"] >= 24, (label, bb)   # WCAG 2.5.8 target size
        assert page.evaluate("() => { const t = document.querySelector('.timeline-toolbar'); "
                             "return t.scrollWidth <= t.clientWidth + 1 }")
        zoom = "() => window.__vaeTest.useStore.getState().timelineZoom"
        z0 = page.evaluate(zoom)
        page.get_by_role("button", name="Zoom in").click()
        page.wait_for_function(f"() => ({zoom})() > {z0} * 1.2")
        # keyboard: Tab-reachable and Enter-operable
        page.get_by_role("button", name="Zoom out").focus()
        page.keyboard.press("Enter")
        page.wait_for_function(f"() => Math.abs(({zoom})() - {z0}) < {z0} * 0.02")
        _shot(page, f"f4b-zoom-steps-{width}-{name}")
    finally:
        _close(page)


def test_help_says_how_tab_reaches_every_control_in_safari(engine, base_url, bars):  # noqa: F811
    name = engine.engine_name
    sid = _project(base_url, bars, f"help {name}")
    page = _open(engine, base_url, sid, 1280, 800)
    try:
        page.locator("body").press("Shift+Slash")
        dialog = page.get_by_role("dialog", name="Keyboard shortcuts")
        expect(dialog).to_be_visible()
        tip = dialog.locator("[data-help-tip=safari-tab]")
        expect(tip).to_contain_text("Safari")
        expect(tip).to_contain_text("Tab")
        assert "⌥" in tip.inner_text() or "Alt" in tip.inner_text()
        tip.scroll_into_view_if_needed()
        _shot(page, f"f4b-help-safari-tab-{name}")
    finally:
        _close(page)


# --------------------------------------------------------------------------- the Prompt bar, by name

def test_the_prompt_bar_edits_by_name(engine, base_url, bars):  # noqa: F811
    """Typed into the key-free Prompt bar: a transition removed by its seam,
    a clip trimmed to a length, a title deleted, a clip mirrored — each lands
    on the EDL as asked and nothing else moves."""
    name = engine.engine_name
    sid = _project(base_url, bars, f"prompt by name {name}")
    _dispatch(base_url, sid, "add_transition", {"at": 2.0, "type": "dissolve"})
    _dispatch(base_url, sid, "add_transition", {"at": 4.0, "type": "fade"})
    _dispatch(base_url, sid, "add_text", {"text": "Day One", "start": 0.0, "end": 2.0})
    ids = [c["id"] for c in _v1(_edl(base_url, sid))]
    page = _open(engine, base_url, sid, 1440, 900)
    box = page.get_by_role("textbox", name="What should happen to this video?")

    def say(text: str) -> None:
        box.fill(text)
        box.press("Enter")

    def texts(e):
        return [c for t in e["tracks"] if t["type"] == "text" for c in t["clips"]]

    try:
        say("remove the transition between clip 2 and 3")
        edl = _wait_edl(base_url, sid, lambda e: len(next(t for t in e["tracks"] if t["id"] == "v1")["transitions"]) == 1,
                        "one transition left", timeout=40)
        assert [(t["at"], t["type"]) for t in next(t for t in edl["tracks"] if t["id"] == "v1")["transitions"]] \
            == [(2.0, "dissolve")]
        page.wait_for_timeout(600)
        say("trim the second clip to 1 second")
        edl = _wait_edl(base_url, sid, lambda e: abs(_footprint(next(c for c in _v1(e) if c["id"] == ids[1])) - 1.0)
                        < 0.02, "clip 2 at 1 s", timeout=40)
        assert [round(_footprint(c), 3) for c in _v1(edl)][:3] == [2.0, 1.0, 2.0]
        page.wait_for_timeout(600)
        say("delete the title")
        _wait_edl(base_url, sid, lambda e: not texts(e), "the title gone", timeout=40)
        if "flip_h" in Transform.model_fields:
            page.wait_for_timeout(600)
            say("flip the second clip")
            edl = _wait_edl(base_url, sid, lambda e: next(c for c in _v1(e) if c["id"] == ids[1])
                            .get("transform", {}).get("flip_h") is True, "clip 2 mirrored", timeout=40)
            assert not any(c.get("transform", {}).get("flip_h") for c in _v1(edl) if c["id"] != ids[1])
        page.wait_for_timeout(800)
        _shot(page, f"f4b-prompt-by-name-{name}")
    finally:
        _close(page)
