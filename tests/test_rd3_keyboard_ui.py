"""Wave D3 review (RD3) editor fixes, through the real UI in Chromium AND
WebKit, with screenshots:

* Space on a KEYBOARD-focused checkbox toggles it and does not play (it
  started playback: Play backwards, Keep pitch, Mute… were unreachable);
  after a mouse click Space still plays;
* a plain button no longer swallows Shift+→ (the playhead did not move from
  the toolbar or the import drop zone);
* C (CapCut / Final Cut) selects the clip at the playhead, ↓ / ↑ the next /
  previous clip, bringing the playhead to it — keyboard-only speed change on
  clip 1 then works;
* ⌥T puts focus in the new text's field with its words selected; typing
  replaces them, Escape returns to the timeline;
* ⌥7 puts focus on the Captions tab's panel, one Tab from its first control;
* a Hero clip's Inspector Duration trims through the curve (00:00:01:00 is
  one second, not 00:00:01:07), and an edge drag keeps the frame under it;
* History never shows a split piece's id.

Harness: `test_frontend_a11y`'s server fixture (VAE_A11Y_BASE_URL = a Vite
dev server proxying /api to a backend). Screenshots to VAE_RD3_SHOTS.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_frontend_a11y import base_url  # noqa: E402,F401  (fixture)
from test_speed_ui_e2e import (  # noqa: E402,F401  (fixtures + helpers)
    FIT_MARGIN, LABEL_W, TimelineGeometry, _client, _edl, _inspector_tab, _open, _project, _timecode, _v1, _wait_edl,
    bars, engine, expect, pw,
)

from video_ai_editor.edl.schema import Clip  # noqa: E402

SHOTS = Path(os.environ.get("VAE_RD3_SHOTS", "/tmp"))

STORE = "(await (window.__vaeTest ?? import('/src/store.ts'))).useStore.getState()"


def _state(page) -> dict:
    return page.evaluate("async () => { const s = " + STORE + "; return { playing: s.isPlaying, ph: s.playhead,"
                         " sel: s.selection, multi: s.multiSelection } }")


def _keyboard_focus(page, selector: str) -> None:
    """Focus `selector` as a keyboard user does: back out of it with ⇧Tab and
    in again with Tab (⌥Tab in WebKit, Safari's tab to every control), so it
    matches :focus-visible in both engines."""
    tab = "Tab" if page.context.browser.browser_type.name == "chromium" else "Alt+Tab"
    page.locator(selector).first.focus()
    page.keyboard.press("Shift+" + tab)
    page.keyboard.press(tab)
    assert page.evaluate("(sel) => document.activeElement === document.querySelector(sel)"
                         " && document.activeElement.matches(':focus-visible')", selector), selector


def _shot(page, name: str) -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / f"rd3_{name}.png"))


def _timeline(page):
    return page.locator('canvas[aria-label="Timeline"]').first


def test_space_on_a_keyboard_focused_checkbox_toggles_it_and_does_not_play(engine, base_url, bars):  # noqa: F811
    name = engine.engine_name
    sid = _project(base_url, bars, f"rd3 space {name}")
    page = _open(engine, base_url, sid, 1280, 800)
    TimelineGeometry(page).select(base_url, sid, _v1(_edl(base_url, sid))[1]["id"])
    _inspector_tab(page, "Speed")
    box = '.props input[aria-label="Play backwards"]'
    before = page.locator(box).is_checked()
    _keyboard_focus(page, box)
    page.keyboard.press("Space")
    page.wait_for_timeout(400)
    assert page.locator(box).is_checked() is (not before)
    assert _state(page)["playing"] is False
    _shot(page, f"space_checkbox_{name}")
    # the keyboard user's Space on a focused BUTTON presses it too; a mouse
    # user's Space after clicking one still plays (Chromium keeps focus there)
    page.locator(".pl .ed-panel-head").first.click()  # focus leaves the box first
    was = page.locator(box).is_checked()
    page.locator(box).click()
    cid = _v1(_edl(base_url, sid))[1]["id"]
    _wait_edl(base_url, sid, lambda e: bool(next(c for c in _v1(e) if c["id"] == cid).get("reverse")) is (not was),
              "the click's own toggle")
    expect(page.locator(box)).to_be_checked(checked=not was)
    after_click = not was
    page.keyboard.press("Space")
    page.wait_for_timeout(400)
    assert page.locator(box).is_checked() is after_click
    assert _state(page)["playing"] is True
    page.keyboard.press("Space")
    page.context.close()


def test_shift_arrow_moves_the_playhead_with_a_button_focused(engine, base_url, bars):  # noqa: F811
    name = engine.engine_name
    sid = _project(base_url, bars, f"rd3 arrows {name}")
    page = _open(engine, base_url, sid, 1280, 800)
    for sel in ('button[aria-label^="Split"]', ".ab-toolbar .ui-btn-primary"):
        page.evaluate("async () => { const s = (await (window.__vaeTest ?? import('/src/store.ts'))).useStore.getState(); s.setPlaying(false);"
                      " s.setPlayhead(0) }")
        _keyboard_focus(page, sel)
        page.keyboard.press("Shift+ArrowRight")
        page.keyboard.press("Shift+ArrowRight")
        page.wait_for_timeout(300)
        assert _state(page)["ph"] == pytest.approx(2.0, abs=1e-6), sel
    page.context.close()


def test_c_and_the_arrows_select_a_particular_clip(engine, base_url, bars):  # noqa: F811
    name = engine.engine_name
    sid = _project(base_url, bars, f"rd3 select {name}")
    clips = _v1(_edl(base_url, sid))
    page = _open(engine, base_url, sid, 1280, 800)
    _timeline(page).focus()
    page.keyboard.press("Escape")
    page.keyboard.press("Home")
    page.keyboard.press("KeyC")
    page.wait_for_timeout(300)
    assert _state(page)["sel"] == clips[0]["id"]
    page.keyboard.press("ArrowDown")
    page.keyboard.press("ArrowDown")
    page.wait_for_timeout(300)
    st = _state(page)
    assert st["sel"] == clips[2]["id"] and st["ph"] == pytest.approx(clips[2]["start"], abs=1e-6)
    page.keyboard.press("ArrowUp")
    page.wait_for_timeout(300)
    assert _state(page)["sel"] == clips[1]["id"]
    expect(page.locator(".props[data-clip-id]")).to_have_attribute("data-clip-id", clips[1]["id"])
    announced = page.evaluate("() => document.querySelector('[data-announcer]')?.textContent ?? ''")
    assert announced.startswith("Selected "), announced
    # the keyboard-only speed change on the chosen clip
    page.keyboard.press("ArrowUp")
    page.wait_for_timeout(300)
    assert _state(page)["sel"] == clips[0]["id"]
    _inspector_tab(page, "Speed")
    speed = page.locator('.props input[type="range"][aria-label*="peed"]').first
    _keyboard_focus(page, '.props input[type="range"][aria-label*="peed"]')
    speed.press("ArrowRight")
    after = _wait_edl(base_url, sid, lambda e: _v1(e)[0].get("speed") not in (None, 1, 1.0), "the speed change")
    assert [c.get("speed") in (None, 1, 1.0) for c in _v1(after)[1:]] == [True] * (len(clips) - 1)
    _shot(page, f"select_clip_{name}")
    page.context.close()


def test_alt_t_puts_the_typing_into_the_new_text(engine, base_url, bars):  # noqa: F811
    name = engine.engine_name
    sid = _project(base_url, bars, f"rd3 text {name}")
    page = _open(engine, base_url, sid, 1280, 800)
    _timeline(page).focus()
    page.keyboard.press("Home")
    page.keyboard.press("Shift+ArrowRight")
    ph = _state(page)["ph"]
    page.keyboard.press("Alt+KeyT")
    page.wait_for_function("() => document.activeElement?.getAttribute('aria-label') === 'Text'", timeout=8000)
    sel = page.evaluate("() => { const a = document.activeElement; return [a.selectionStart, a.selectionEnd, a.value.length] }")
    assert sel[0] == 0 and sel[1] == sel[2] > 0, sel
    page.keyboard.type("Hello keyboard")
    page.keyboard.press("Escape")
    edl = _wait_edl(base_url, sid, lambda e: any(c.get("text") == "Hello keyboard" for t in e["tracks"]
                                                 for c in t["clips"]), "the typed text")
    assert edl
    assert page.evaluate("() => document.activeElement?.getAttribute('aria-label')") == "Timeline"
    assert _state(page)["ph"] == pytest.approx(ph, abs=1e-6)      # L / K never shuttled
    _shot(page, f"alt_t_{name}")
    page.context.close()


def test_alt_7_puts_focus_on_the_captions_panel(engine, base_url, bars):  # noqa: F811
    name = engine.engine_name
    sid = _project(base_url, bars, f"rd3 panel {name}")
    page = _open(engine, base_url, sid, 1280, 800)
    _timeline(page).focus()
    page.keyboard.press("Alt+Digit7")
    page.wait_for_function("() => document.activeElement?.id === 'asset-panel-captions'", timeout=5000)
    # its first control is one Tab away (WebKit tabs to buttons with ⌥, like
    # Safari; the packaged app turns full Tab on: test_wk_tab_focus.py)
    page.keyboard.press("Tab" if name == "chromium" else "Alt+Tab")
    assert page.evaluate("() => !!document.activeElement?.closest('[data-auto-captions]')"), \
        page.evaluate("() => document.activeElement?.outerHTML.slice(0, 120)")
    _shot(page, f"alt7_{name}")
    page.context.close()


def test_a_hero_clips_duration_and_edge_drag_trim_through_the_curve(engine, base_url, bars):  # noqa: F811
    name = engine.engine_name
    sid = _project(base_url, bars, f"rd3 curve {name}")
    last, prev = _v1(_edl(base_url, sid))[-1], _v1(_edl(base_url, sid))[-2]   # 12-15 s and 10-12 s
    with _client(base_url) as c:
        for cl in (last, prev):
            assert c.post(f"/api/sessions/{sid}/dispatch", json={"tool": "set_speed",
                          "args": {"clip_id": cl["id"], "preset": "hero"}}).status_code == 200
    last = next(c for c in _v1(_edl(base_url, sid)) if c["id"] == last["id"])
    page = _open(engine, base_url, sid, 1280, 800)
    geo = TimelineGeometry(page)
    geo.select(base_url, sid, last["id"])
    _inspector_tab(page, "Video")
    field = page.locator('.props input[aria-label="Duration"]').first
    field.click()
    field.fill("00:00:01:00")
    field.press("Enter")
    edl = _wait_edl(base_url, sid, lambda e: _v1(e)[-1]["out"] != last["out"], "the Duration edit")
    piece = Clip.model_validate(_v1(edl)[-1])
    assert piece.effective_duration == pytest.approx(1.0, abs=1e-6)
    page.wait_for_timeout(400)
    expect(field).to_have_value("00:00:01:00")
    _shot(page, f"curve_duration_{name}")
    # an edge drag on the Hero clip before it
    edl = _edl(base_url, sid)
    prev = next(c for c in _v1(edl) if c["id"] == prev["id"])
    whole = Clip.model_validate(prev)
    page.get_by_role("button", name="Zoom to fit").click()
    page.wait_for_timeout(300)
    box = page.locator(".timeline-canvas-wrap canvas").first.bounding_box()
    zoom = max(40.0, box["width"] - LABEL_W - FIT_MARGIN) / edl["duration"]
    y = geo.last_point[1]
    x_edge = box["x"] + LABEL_W + (prev["start"] + whole.effective_duration) * zoom
    dx = round(0.8 * zoom)                     # 0.8 s inward
    page.mouse.move(x_edge - 2, y)
    page.mouse.down()
    page.mouse.move(x_edge - 2 - dx / 2, y, steps=4)
    page.mouse.move(x_edge - 2 - dx, y, steps=4)
    page.mouse.up()
    edl2 = _wait_edl(base_url, sid, lambda e: next(c for c in _v1(e) if c["id"] == prev["id"])["out"] != prev["out"],
                     "the edge drag")
    after = Clip.model_validate(next(c for c in _v1(edl2) if c["id"] == prev["id"]))
    want = whole.effective_duration - dx / zoom
    # the piece of the curve under the edge: within a frame of where it was dropped
    assert abs(after.effective_duration - want) <= 1.5 / 30, (after.effective_duration, want)
    assert after.out == pytest.approx(after.in_ + whole.source_offset_at(after.effective_duration), abs=1e-6)
    page.context.close()


def test_history_never_shows_a_split_pieces_id(engine, base_url, bars):  # noqa: F811
    name = engine.engine_name
    sid = _project(base_url, bars, f"rd3 history {name}")
    piece = _v1(_edl(base_url, sid))[1]["id"]
    assert re.search(r"_[0-9a-f]{4,}$", piece), piece      # a split descendant
    with _client(base_url) as c:
        for tool, args in (("set_speed", {"clip_id": piece, "factor": 2.0}),
                           ("trim_clip", {"clip_id": piece, "out": 3.5}),
                           ("freeze_frame", {"time": 0.5, "duration": 1.0})):
            assert c.post(f"/api/sessions/{sid}/dispatch", json={"tool": tool, "args": args}).status_code == 200
    page = _open(engine, base_url, sid, 1280, 800)
    page.locator(".in-history summary").click()      # History is a disclosure under Details
    page.locator(".ops-log .op").first.wait_for()
    rows = page.locator(".ops-log .op").all_text_contents()
    assert rows and not [r for r in rows if re.search(r"\b[a-z]{1,3}_[0-9a-f]{6,}", r) or "()" in r or "— →" in r], rows
    _shot(page, f"history_{name}")
    page.context.close()
