"""Wave D, left tool rail, phase R1 — the editor shell measured in a real
browser, in Chromium AND WebKit (docs/design/LEFT_RAIL_SPEC.md §8.3).

R1 replaced the three-tab LeftPane with a vertical tool rail (Media, Audio,
Stickers, Effects, Transitions, AI) driving one tool panel, moved the right
panel into RightPanel with a 36 px collapsed rail, and rebuilt the .app grid on
per-state column variables. What each case pins:

1.  Names: every rail tab matches exactly one element by its exact label, and
    the tooltip is never part of a name (no `title`, an aria-hidden portal).
2.  Grid: the §1.2 column widths at 1024/1280/1440/1920, with the tool panel
    collapsed, and with both side panels collapsed — no empty column (H3).
6.  Focus rescue (H2): focus inside a region that hides moves to the control
    that owns it; focus anywhere else is left alone. WebKit parks focus on
    <body> or a hidden element where Chromium differs, so the rescue is by
    element state. Panel switches here use a focus-neutral `el.click()` —
    exactly what a keyboard chord (R4) will do.
7.  Tooltip (H4): 400 ms delay, right of the rail, Esc hides it, hoverable,
    and it names a chord only once the live keymap binds one (R4 binds them).
4.  Activity (R2, §2.8): the top bar's activity chip keeps a voiceover's Stop
    and the captions' Cancel reachable with another panel showing AND the tool
    panel collapsed; its polite live region exists before anything runs and
    speaks state changes only; the rail's Audio / Captions dots follow the run.
10. Tab order: Media tab → Hide the tool panel → the dropzone.

Plus the rail's roving tabindex (↑/↓/Home/End, Space toggles) and reference
screenshots at the four spec sizes (VAE_RAIL_SHOTS, default /tmp).

Harness (server, sessions) is test_frontend_a11y's: set VAE_A11Y_BASE_URL to
run against a server that is already up (a Vite dev server proxying /api),
otherwise it serves frontend/dist. Engines missing from the Playwright install
skip. The packaged app's engine is WKWebView; Playwright WebKit is the closest
automated stand-in (see tests/wk/ for the in-app WebKit harness).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path

import pytest

from test_frontend_a11y import base_url, sessions  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")
SHOTS = Path(os.environ.get("VAE_RAIL_SHOTS", "/tmp"))

RAIL = ["Media", "Audio", "Text", "Stickers", "Effects", "Transitions", "Captions", "AI"]
TIPS = {
    "Media": "Footage, photos and the project bin",
    "Stickers": "Emoji and stickers",
}
# §1.2: viewport → (rail, tool panel, centre, right) first-run widths.
GRID = {
    (1024, 768): (48, 220, 484, 260),
    (1280, 800): (64, 240, 684, 280),
    (1440, 900): (64, 280, 804, 280),
    (1920, 1080): (64, 320, 1204, 320),
}


@pytest.fixture(scope="module")
def pw():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed")
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="module", params=["chromium", "webkit"])
def engine(request, pw):
    try:
        b = getattr(pw, request.param).launch()
    except Exception as e:  # noqa: BLE001 — a missing engine is a skip, not a failure
        pytest.skip(f"no Playwright {request.param}: {e}")
    b.engine_name = request.param
    yield b
    b.close()


def _open(browser, base_url, sid, width=1440, height=900, extra: dict | None = None):  # noqa: F811
    ctx = browser.new_context(viewport={"width": width, "height": height})
    items = {"vai.sessionId": sid, "vai.rightTab": "inspect", **(extra or {})}
    ctx.add_init_script("try { " + " ".join(
        f"localStorage.setItem({json.dumps(k)}, {json.dumps(v)});" for k, v in items.items()) + " } catch (e) {}")
    page = ctx.new_page()
    page.goto(base_url + "/")
    page.get_by_role("tab", name="Media", exact=True).wait_for()
    page.locator(".timeline-canvas-wrap canvas").first.wait_for()
    page.wait_for_timeout(1200)
    return page


def _tab(page, label):
    return page.get_by_role("tab", name=label, exact=True)


def _cols(page) -> list[int]:
    raw = page.evaluate("getComputedStyle(document.querySelector('.app')).gridTemplateColumns")
    return [round(float(x.removesuffix("px"))) for x in raw.split()]


def _active(page) -> dict:
    return page.evaluate("""() => { const a = document.activeElement
      return { id: a.id, tag: a.tagName, label: a.getAttribute('aria-label'), text: (a.textContent || '').trim().slice(0, 40) } }""")


def _js_click(locator):
    """Activate without moving focus — what a keyboard chord does (R4)."""
    locator.evaluate("el => el.click()")


# ---------------------------------------------------------------- case 1 ----

@pytest.mark.parametrize("size", [(1440, 900), (1024, 768)])
def test_each_rail_tab_has_exactly_its_label_as_name(engine, base_url, sessions, size):  # noqa: F811
    page = _open(engine, base_url, sessions["full"], *size)
    assert page.locator("nav.rail [role=tab]").count() == len(RAIL)
    for label in RAIL:
        assert _tab(page, label).count() == 1, label
        assert _tab(page, label).get_attribute("title") is None, label
    # Show a tooltip: it is aria-hidden and never joins a name.
    _tab(page, "Stickers").hover()
    page.wait_for_timeout(500)
    assert page.locator(".rail-tip").is_visible()
    assert page.locator(".rail-tip").get_attribute("aria-hidden") == "true"
    for label in RAIL:
        assert _tab(page, label).count() == 1, label
    assert page.get_by_role("tab", name=re.compile("—|Emoji and stickers")).count() == 0
    if size[0] < 1280:
        # Compact rail: the label is visually hidden but still the name.
        box = page.locator("#rail-tab-media .rail-label").bounding_box()
        assert box["width"] <= 1 and box["height"] <= 1, box
    page.context.close()


def test_rail_is_one_tab_stop_with_arrow_keys(engine, base_url, sessions):  # noqa: F811
    page = _open(engine, base_url, sessions["full"])
    tabbable = page.evaluate("[...document.querySelectorAll('nav.rail [role=tab]')].map(t => t.tabIndex)")
    assert sorted(tabbable) == [-1] * (len(RAIL) - 1) + [0]
    _tab(page, "Media").focus()
    for key, want in (("ArrowDown", "Audio"), ("End", "AI"), ("ArrowDown", "Media"), ("ArrowUp", "AI"), ("Home", "Media")):
        page.keyboard.press(key)
        assert _tab(page, want).get_attribute("aria-selected") == "true", (key, want)
        assert _active(page)["id"] == f"rail-tab-{want.lower()}", (key, _active(page))
        assert page.locator(f"#tool-panel-{want.lower()}").is_visible()
    # Enter on the active tab collapses the panel and opens it again (Space
    # too from R4: test_wave_d_rail_keys_ui).
    page.keyboard.press("Enter")
    assert _tab(page, "Media").get_attribute("aria-expanded") == "false"
    assert not page.locator("#tool-panel").is_visible()
    page.keyboard.press("Enter")
    assert _tab(page, "Media").get_attribute("aria-expanded") == "true"
    # An arrow from a collapsed panel opens the panel it lands on.
    page.keyboard.press("Enter")
    page.keyboard.press("ArrowDown")
    assert page.locator("#tool-panel-audio").is_visible()
    page.context.close()


def _playing(page) -> bool:
    return page.locator(".timeline-toolbar button[aria-keyshortcuts=Space]").get_attribute("aria-label") == "Pause"


def _snap(page) -> str:
    return page.get_by_role("button", name="Snapping", exact=True).get_attribute("aria-pressed")


def test_a_clicked_rail_tab_leaves_every_global_shortcut_working(engine, base_url, sessions):  # noqa: F811
    """Review RD1 (HIGH): after a MOUSE click on a rail tab, Space must play
    (not collapse the panel) and N must toggle snapping — as a click on the
    old LeftPane tabs did. The click must not park focus on the tab."""
    page = _open(engine, base_url, sessions["full"], 1280, 800)
    _tab(page, "Effects").click()
    page.wait_for_timeout(150)
    assert _tab(page, "Effects").get_attribute("aria-selected") == "true"
    assert _active(page)["id"] != "rail-tab-effects", _active(page)
    assert not _playing(page)
    page.keyboard.press("Space")
    page.wait_for_timeout(250)
    assert _playing(page), "Space after a rail click did not play"
    assert page.locator("#tool-panel").is_visible(), "Space collapsed the tool panel"
    page.keyboard.press("Space")
    page.wait_for_timeout(150)
    before = _snap(page)
    page.keyboard.press("n")
    page.wait_for_timeout(150)
    assert _snap(page) != before, "N after a rail click did not toggle snapping"
    page.keyboard.press("n")
    page.context.close()


def test_keyboard_focus_on_the_rail_keeps_undo_and_playback(engine, base_url, sessions):  # noqa: F811
    """Review RD1: a keyboard user arrowing through the rail keeps the global
    shortcuts (N snaps; ⌘Z, J/K/L in test_wave_d_rail_keys_ui) — the rail is
    not an ignore scope. From R4, Space on the focused tab is the tab's own
    key (§2.4: it collapses / re-opens the panel) and does not play."""
    page = _open(engine, base_url, sessions["full"], 1280, 800)
    _tab(page, "Media").focus()
    page.keyboard.press("ArrowDown")
    assert _active(page)["id"] == "rail-tab-audio"
    page.keyboard.press("Space")
    page.wait_for_timeout(250)
    assert not _playing(page), "Space on a focused rail tab played"
    assert not page.locator("#tool-panel").is_visible(), "Space on a focused rail tab did not collapse"
    page.keyboard.press("Space")
    page.wait_for_timeout(150)
    assert page.locator("#tool-panel").is_visible()
    before = _snap(page)
    page.keyboard.press("n")
    page.wait_for_timeout(150)
    assert _snap(page) != before
    page.keyboard.press("n")
    page.context.close()


def test_idle_ai_description_is_not_in_the_reading_order(engine, base_url, sessions):  # noqa: F811
    """Review RD1: the sr-only 'An AI tool is running' must not be read inside
    the Tools nav when nothing runs (it is only an aria-describedby target)."""
    page = _open(engine, base_url, sessions["full"], 1280, 800)
    snap = page.get_by_role("navigation", name="Tools").aria_snapshot()
    assert "An AI tool is running" not in snap, snap
    assert _tab(page, "AI").get_attribute("aria-describedby") is None
    page.context.close()


@pytest.mark.parametrize("size", [(1000, 768), (1024, 768)])
def test_timeline_toolbar_is_never_clipped(engine, base_url, sessions, size):  # noqa: F811
    """Review RD1: the R1 centre column is narrower (484 at 1024), so the
    zoom steps must drop before 'Zoom to fit' is pushed out of the toolbar."""
    page = _open(engine, base_url, sessions["full"], *size)
    bar = page.locator(".timeline-toolbar")
    sw, cw = bar.evaluate("el => [el.scrollWidth, el.clientWidth]")
    assert sw <= cw, (sw, cw)
    fit = page.get_by_role("button", name="Zoom to fit").bounding_box()
    box = bar.bounding_box()
    assert fit["x"] + fit["width"] <= box["x"] + box["width"] + 0.5, (fit, box)
    page.context.close()


# ---------------------------------------------------------------- case 2 ----

@pytest.mark.parametrize("size", list(GRID))
def test_grid_columns_per_breakpoint_and_no_hole_when_collapsed(engine, base_url, sessions, size):  # noqa: F811
    w, _ = size
    rail, left, centre, right = GRID[size]
    page = _open(engine, base_url, sessions["full"], *size)
    assert _cols(page) == [rail, left, 6, centre, 6, right]
    page.get_by_role("button", name="Hide the tool panel").click()
    page.wait_for_timeout(250)
    assert _cols(page) == [rail, 0, 0, w - rail - 6 - right, 6, right]
    assert not page.locator("#tool-panel").is_visible()
    page.get_by_role("button", name="Hide the Inspector and Chat panel").click()
    page.wait_for_timeout(250)
    assert _cols(page) == [rail, 0, 0, w - rail - 36, 0, 36]
    # No hole: the centre starts right at the rail's edge and fills to the
    # 36 px right rail.
    c = page.locator("main.center").bounding_box()
    assert round(c["x"]) == rail and round(c["width"]) == w - rail - 36, c
    assert round(page.locator("#right-panel").bounding_box()["width"]) == 36
    page.screenshot(path=str(SHOTS / f"rail-r1-{engine.engine_name}-{w}-collapsed.png"))
    # Both states persist across a reload.
    page.reload()
    page.get_by_role("tab", name="Media", exact=True).wait_for()
    page.wait_for_timeout(600)
    assert _cols(page) == [rail, 0, 0, w - rail - 36, 0, 36]
    # And come back from their owning controls.
    _tab(page, "Media").click()
    page.get_by_role("button", name="Show the Inspector and Chat panel").click()
    page.wait_for_timeout(250)
    assert _cols(page) == [rail, left, 6, centre, 6, right]
    page.context.close()


def test_dragged_width_wins_over_the_breakpoint_default(engine, base_url, sessions):  # noqa: F811
    page = _open(engine, base_url, sessions["full"], 1440, 900)
    handle = page.locator(".splitter-left").bounding_box()
    x, y = handle["x"] + handle["width"] / 2, handle["y"] + handle["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    for i in range(1, 9):
        page.mouse.move(x - 5 * i, y)
    page.mouse.up()
    page.wait_for_timeout(200)
    # 280 − 40, seeded from the drawn width: no dead first pixels.
    assert _cols(page)[1] == 240
    assert page.evaluate("localStorage.getItem('vai.leftW')") == "240"
    page.context.close()


# ---------------------------------------------------------------- case 6 ----

def test_focus_rescue_moves_only_focus_that_was_stranded(engine, base_url, sessions):  # noqa: F811
    page = _open(engine, base_url, sessions["full"], 1440, 900)

    # a. In the Media panel → another panel shows: focus goes to its tab.
    page.locator(".dropzone").focus()
    _js_click(_tab(page, "Stickers"))
    page.wait_for_timeout(150)
    assert _active(page)["id"] == "rail-tab-stickers", _active(page)

    # b. In the Stickers panel → the panel collapses: focus goes to its tab.
    page.locator(".sticker-picker input").first.focus()
    _js_click(_tab(page, "Stickers"))
    page.wait_for_timeout(150)
    assert not page.locator("#tool-panel").is_visible()
    assert _active(page)["id"] == "rail-tab-stickers", _active(page)
    # The header's own collapse button hands focus to the tab too (§2.4).
    _js_click(_tab(page, "Stickers"))
    page.locator(".sticker-picker input").first.focus()
    page.get_by_role("button", name="Hide the tool panel").click()
    page.wait_for_timeout(150)
    assert _active(page)["id"] == "rail-tab-stickers", _active(page)
    _js_click(_tab(page, "Media"))

    # d. Focus outside the panel stays put when the panel switches.
    undo = page.get_by_role("button", name="Undo", exact=True)
    assert undo.is_enabled()
    undo.focus()
    _js_click(_tab(page, "Audio"))
    page.wait_for_timeout(150)
    assert _active(page)["label"] == "Undo", _active(page)

    # e. A media row (a data-keymap-ignore scope) → AI shows: AI selected and
    #    focus on its tab. (The ⌥8 half of this case is R4's keymap scope.)
    _js_click(_tab(page, "Media"))
    page.locator("[data-media-row]").first.focus()
    _js_click(_tab(page, "AI"))
    page.wait_for_timeout(150)
    assert _tab(page, "AI").get_attribute("aria-selected") == "true"
    assert _active(page)["id"] == "rail-tab-ai", _active(page)

    # c. An Inspector slider → the right panel collapses: focus goes to
    #    "Show the Inspector and Chat panel"; expanding hands it back to the tab.
    cv = page.locator(".timeline-canvas-wrap canvas").first.bounding_box()
    page.mouse.click(cv["x"] + 140, cv["y"] + 40)
    slider = page.locator("#right-panel-inspect input[type=range]").first
    slider.wait_for(timeout=5000)
    slider.focus()
    _js_click(page.get_by_role("button", name="Hide the Inspector and Chat panel"))
    page.wait_for_timeout(150)
    assert _active(page)["label"] == "Show the Inspector and Chat panel", _active(page)
    _js_click(page.get_by_role("button", name="Show the Inspector and Chat panel"))
    page.wait_for_timeout(150)
    assert _active(page)["id"] == "right-tab-inspect", _active(page)

    # f. The right panel switching tab under focus: back to the selected tab.
    _tab(page, "Chat").click()
    page.locator("#right-panel-chat textarea").focus()
    _js_click(_tab(page, "Inspector"))
    page.wait_for_timeout(150)
    assert _active(page)["id"] == "right-tab-inspect", _active(page)
    page.context.close()


def test_show_the_chat_focuses_the_message_box(engine, base_url, sessions):  # noqa: F811
    page = _open(engine, base_url, sessions["full"], 1280, 800)
    page.get_by_role("button", name="Hide the Inspector and Chat panel").click()
    page.get_by_role("button", name="Show the Chat", exact=True).click()
    page.wait_for_timeout(300)
    assert _tab(page, "Chat").get_attribute("aria-selected") == "true"
    assert _active(page)["tag"] == "TEXTAREA", _active(page)
    page.context.close()


# ---------------------------------------------------------------- case 7 ----

def test_tooltip_delay_placement_escape_and_hover(engine, base_url, sessions):  # noqa: F811
    page = _open(engine, base_url, sessions["full"], 1024, 768)
    tip = page.locator(".rail-tip")
    rail_right = page.locator("nav.rail").bounding_box()
    rail_right = rail_right["x"] + rail_right["width"]
    _tab(page, "Stickers").hover()
    page.wait_for_timeout(200)
    assert not tip.is_visible()                       # not before 400 ms
    page.wait_for_timeout(250)
    assert tip.is_visible()
    box = tip.bounding_box()
    assert box["x"] >= rail_right, (box, rail_right)  # right of the rail, never clipped by it
    assert tip.inner_text().startswith("Stickers — " + TIPS["Stickers"])
    # R4 binds ⌥4 in every preset: the tip carries it and the tab names it.
    assert tip.locator("kbd").inner_text() in ("⌥4", "Alt+4")
    assert _tab(page, "Stickers").get_attribute("aria-keyshortcuts") == "Alt+4"
    page.keyboard.press("Escape")
    assert not tip.is_visible()
    # Hoverable (WCAG 1.4.13): the pointer can travel onto the tip.
    _tab(page, "Media").hover()
    page.wait_for_timeout(450)
    assert tip.is_visible()
    t = tip.bounding_box()
    page.mouse.move(t["x"] + 4, t["y"] + t["height"] / 2, steps=4)
    page.wait_for_timeout(300)
    assert tip.is_visible()
    page.mouse.move(700, 400)
    page.wait_for_timeout(300)
    assert not tip.is_visible()
    page.context.close()


def test_tooltip_carries_the_chord_once_the_keymap_binds_one(engine, base_url, sessions):  # noqa: F811
    # A user override replaces the preset chord (⌥4 since R4) and the tip follows.
    keymap = {"presetId": "capcut", "overrides": {"panelStickers": ["Alt+Shift+Digit4"]}}
    page = _open(engine, base_url, sessions["full"], 1440, 900, {"vae.keymap.v1": json.dumps(keymap)})
    tab = _tab(page, "Stickers")
    assert tab.get_attribute("aria-keyshortcuts") == "Alt+Shift+4"
    tab.hover()
    page.wait_for_timeout(450)
    kbd = page.locator(".rail-tip kbd")
    assert kbd.count() == 1 and kbd.inner_text() in ("⌥⇧4", "Alt+Shift+4")
    # The name is still exactly the label.
    assert _tab(page, "Stickers").count() == 1
    page.context.close()


# ---------------------------------------------------------------- case 4 ----

# The packaged app records through desktop.py's native bridge (WKWebView has no
# usable getUserMedia there); this stands in for it and logs every call, so the
# chip's Stop is proven to reach the SAME stop the panel's button uses.
FAKE_VO_BRIDGE = """
window.__vo = [];
window.pywebview = { api: {
  vo_start: async (sid) => { window.__vo.push(['start', sid]); return { ok: true } },
  vo_stop: async (sid, start, gain) => { window.__vo.push(['stop', sid]); return { ok: true, clip_id: null } },
} };
"""

# Every message the chip's live region ever says, in order (the region must
# speak state changes and 25/50/75 % only, never a clock tick).
LIVE_LOG = """
window.__live = [];
new MutationObserver(() => {
  const r = document.querySelector('[data-activity] [role=status]')
  const t = r && r.textContent
  if (t && window.__live[window.__live.length - 1] !== t) window.__live.push(t)
}).observe(document, { subtree: true, childList: true, characterData: true });
"""


def _open_with(browser, base_url, sid, script, width=1280, height=800):  # noqa: F811
    ctx = browser.new_context(viewport={"width": width, "height": height})
    ctx.add_init_script(script)
    ctx.add_init_script("try { " + " ".join(
        f"localStorage.setItem({json.dumps(k)}, {json.dumps(v)});"
        for k, v in {"vai.sessionId": sid, "vai.rightTab": "inspect"}.items()) + " } catch (e) {}")
    page = ctx.new_page()
    page.goto(base_url + "/")
    page.get_by_role("tab", name="Media", exact=True).wait_for()
    page.locator(".timeline-canvas-wrap canvas").first.wait_for()
    page.wait_for_timeout(1200)
    return page


def _inside_viewport(page, locator) -> bool:
    b = locator.bounding_box()
    vw = page.viewport_size["width"]
    return b is not None and b["x"] >= 0 and b["x"] + b["width"] <= vw and b["y"] >= 0 and b["width"] > 0


def _collapse_on_media(page):
    """Media selected AND the tool panel collapsed: neither Audio nor Captions shows."""
    _js_click(_tab(page, "Media"))
    page.get_by_role("button", name="Hide the tool panel").click()
    page.wait_for_timeout(200)
    assert not page.locator("#tool-panel").is_visible()
    assert _tab(page, "Media").get_attribute("aria-selected") == "true"


def test_activity_region_exists_before_anything_runs(engine, base_url, sessions):  # noqa: F811
    page = _open(engine, base_url, sessions["full"])
    region = page.locator(".topbar [data-activity] [role=status]")
    assert region.count() == 1
    assert region.get_attribute("aria-live") == "polite"
    assert region.inner_text() == ""
    assert page.get_by_role("button", name="Stop recording").count() == 0
    assert page.get_by_role("button", name="Cancel captions").count() == 0
    # Text and Captions left the top bar for the rail (R2).
    assert page.locator(".topbar [data-text-presets], .topbar .cc-caret, .topbar .cc-main").count() == 0
    page.context.close()


def test_stop_recording_from_the_chip_with_the_panel_collapsed(engine, base_url, sessions):  # noqa: F811
    page = _open_with(engine, base_url, sessions["full"], FAKE_VO_BRIDGE + LIVE_LOG)
    _tab(page, "Audio").click()
    page.get_by_role("button", name="Record voiceover").click()
    stop = page.get_by_role("button", name="Stop recording")
    stop.wait_for(timeout=8000)                      # after the 3-2-1 count-in
    assert page.evaluate("window.__vo.map(c => c[0])") == ["start"]
    _collapse_on_media(page)
    assert stop.is_visible() and _inside_viewport(page, stop)
    body = page.locator(".act-rec .act-body")
    assert re.fullmatch(r"Recording voiceover, 0:0\d\. Open the Audio panel", body.get_attribute("aria-label"))
    # The rail's Audio dot is the secondary cue, described not named.
    assert _tab(page, "Audio").get_attribute("aria-describedby") == "rail-desc-audio"
    assert page.locator("#rail-tab-audio .rail-dot-rec").is_visible()
    assert _tab(page, "Audio").count() == 1
    page.wait_for_timeout(1300)                      # a clock tick must not reach the live region
    page.screenshot(path=str(SHOTS / f"rail-r2-{engine.engine_name}-recording-collapsed.png"))
    stop.click()
    page.wait_for_timeout(500)
    assert page.evaluate("window.__vo.map(c => c[0])") == ["start", "stop"]
    assert stop.count() == 0
    assert _tab(page, "Audio").get_attribute("aria-describedby") is None
    assert page.evaluate("window.__live") == ["Recording a voiceover", "Recording stopped"]
    # The panel's recorder is back to idle too: one take, one stop.
    _js_click(_tab(page, "Audio"))
    page.get_by_role("button", name="Record voiceover").wait_for(timeout=3000)
    page.context.close()


def test_chip_body_opens_the_panel_that_owns_the_activity(engine, base_url, sessions):  # noqa: F811
    page = _open_with(engine, base_url, sessions["full"], FAKE_VO_BRIDGE)
    _tab(page, "Audio").click()
    page.get_by_role("button", name="Record voiceover").click()
    page.get_by_role("button", name="Stop recording").wait_for(timeout=8000)
    _collapse_on_media(page)
    page.locator(".act-rec .act-body").click()
    page.wait_for_timeout(200)
    assert _tab(page, "Audio").get_attribute("aria-selected") == "true"
    assert page.locator("#tool-panel-audio").is_visible()
    page.get_by_role("button", name="Stop recording").click()
    page.context.close()


def _stub_caption_job(page, ack_after_s=1.5):
    """A captions job the test drives: 42 % until Cancel, then (like the real
    decoder, which stops only between 30 s windows) `cancelled` a little later."""
    state = {"cancel_at": None, "polls": 0, "cancels": 0}

    def downloads(route):
        route.fulfill(json={"downloads": {
            "captions:large-v3": {"what": "the accurate caption model", "bytes": 3_100_000_000, "cached": True},
            "captions:large-v3-turbo": {"what": "the fast caption model", "bytes": 1_600_000_000, "cached": False}}})

    def dispatch(route):
        body = route.request.post_data_json or {}
        if body.get("tool") != "auto_caption":
            return route.continue_()
        route.fulfill(json={"job_id": "job-cc", "status": "running", "status_url": "/api/jobs/job-cc"})

    def job(route):
        if route.request.url.endswith("/cancel"):
            state["cancels"] += 1
            state["cancel_at"] = time.monotonic()
            return route.fulfill(json={"id": "job-cc", "status": "running", "progress": 0.42})
        state["polls"] += 1
        done = state["cancel_at"] is not None and time.monotonic() - state["cancel_at"] > ack_after_s
        route.fulfill(json={"id": "job-cc", "kind": "dispatch", "status": "cancelled" if done else "running",
                            "progress": 0.42, "result": None, "error": None})

    page.route(re.compile(r".*/api/downloads$"), downloads)
    page.route(re.compile(r".*/api/sessions/[^/]+/dispatch\?wait=0$"), dispatch)
    page.route(re.compile(r".*/api/jobs/job-cc(/cancel)?$"), job)
    return state


def test_cancel_captions_from_the_chip_shows_stopping(engine, base_url, sessions):  # noqa: F811
    page = _open_with(engine, base_url, sessions["full"], LIVE_LOG)
    state = _stub_caption_job(page)
    _tab(page, "Captions").click()
    page.get_by_role("button", name="Generate captions").click()
    cancel = page.get_by_role("button", name="Cancel captions")
    cancel.wait_for(timeout=5000)
    page.wait_for_function("document.querySelector('.act-cc .act-body').textContent.includes('42%')", timeout=5000)
    # The panel shows the SAME run: %, and its own Cancel.
    assert "42%" in page.locator("#tool-panel-captions .cc-progress").inner_text()
    assert _tab(page, "Captions").get_attribute("aria-describedby") == "rail-desc-captions"
    _collapse_on_media(page)
    assert cancel.is_visible() and _inside_viewport(page, cancel)
    page.screenshot(path=str(SHOTS / f"rail-r2-{engine.engine_name}-captions-collapsed.png"))
    cancel.click()
    page.wait_for_function("document.querySelector('.act-cc .act-body').textContent.includes('Stopping…')", timeout=3000)
    assert state["cancels"] == 1
    assert cancel.is_disabled()                      # one cancel per run: the job is already stopping
    # The panel agrees (one run, never two Cancel paths).
    _js_click(_tab(page, "Captions"))
    page.wait_for_timeout(150)
    assert "Stopping…" in page.locator("#tool-panel-captions .cc-progress").inner_text()
    assert page.locator("#tool-panel-captions .cc-cancel").count() == 0
    # The job acknowledges: the chip goes, the rail dot goes, the toast says so.
    cancel.wait_for(state="detached", timeout=8000)
    page.wait_for_timeout(300)
    assert _tab(page, "Captions").get_attribute("aria-describedby") is None
    assert "Auto captions was cancelled" in page.locator(".toast-host").inner_text()
    live = page.evaluate("window.__live")
    assert live == ["Captions started", "Captions 25%", "Stopping captions", "Captions cancelled"], live
    assert page.get_by_role("button", name="Generate captions").is_enabled()
    page.context.close()


def test_the_panel_cancel_is_the_chip_cancel(engine, base_url, sessions):  # noqa: F811
    page = _open_with(engine, base_url, sessions["full"], "")
    state = _stub_caption_job(page, ack_after_s=0.5)
    _tab(page, "Captions").click()
    page.get_by_role("button", name="Generate captions").click()
    page.locator("#tool-panel-captions .cc-cancel").click(timeout=5000)
    page.wait_for_function("document.querySelector('.act-cc .act-body')?.textContent.includes('Stopping…')", timeout=3000)
    assert state["cancels"] == 1
    assert page.get_by_role("button", name="Cancel captions").is_disabled()
    page.get_by_role("button", name="Cancel captions").wait_for(state="detached", timeout=8000)
    page.context.close()


def test_fastest_asks_before_downloading_and_sends_nothing_on_cancel(engine, base_url, sessions):  # noqa: F811
    page = _open_with(engine, base_url, sessions["full"], "")
    _stub_caption_job(page)
    sent = []
    page.on("request", lambda r: sent.append(r.post_data) if "/dispatch" in r.url else None)
    _tab(page, "Captions").click()
    page.wait_for_timeout(400)
    assert page.locator("#cc-speed-fast-dl").inner_text() == "Downloads 1.6 GB first"
    assert page.locator("#cc-speed-quality-dl").count() == 0
    page.get_by_role("radio", name="Fastest", exact=True).check()
    assert page.evaluate("localStorage.getItem('vai.captionSpeed')") == "fast"
    page.get_by_role("button", name="Generate captions").click()
    dlg = page.get_by_role("dialog", name="Download the caption model?")
    dlg.wait_for(timeout=3000)
    assert dlg.locator(".dialog-text").inner_text() == (
        "The first run downloads the fast caption model (1.6 GB, once). It stays on this Mac for next time. "
        "Captions start when it finishes.")
    dlg.get_by_role("button", name="Cancel", exact=True).click()
    page.wait_for_timeout(300)
    assert not dlg.is_visible()
    assert page.evaluate("document.activeElement.textContent.trim()") == "Generate captions"
    assert not [b for b in sent if b and "auto_caption" in b]
    assert page.get_by_role("button", name="Cancel captions").count() == 0
    page.context.close()


@pytest.fixture(scope="module")
def fake_mic_chromium(pw):
    try:
        b = pw.chromium.launch(args=["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"])
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"no Playwright chromium: {e}")
    b.engine_name = "chromium"
    yield b
    b.close()


def test_real_mediarecorder_take_stops_from_the_chip(fake_mic_chromium, base_url, sessions):  # noqa: F811
    """The browser path end to end (Chromium's fake mic): the chip's Stop ends a
    real MediaRecorder take, which uploads to /vo_record and lands a clip on
    the voiceover track of the real backend."""
    import httpx
    sid = sessions["full"]

    def vo_clips():
        edl = httpx.get(f"{base_url}/api/sessions/{sid}/edl", timeout=30).json()
        edl = edl.get("edl", edl)
        return [c["id"] for t in edl["tracks"] if t["id"] == "vo" for c in t["clips"]]

    before = vo_clips()
    page = _open(fake_mic_chromium, base_url, sid)
    _tab(page, "Audio").click()
    page.get_by_role("button", name="Record voiceover").click()
    stop = page.get_by_role("button", name="Stop recording")
    stop.wait_for(timeout=10_000)
    _collapse_on_media(page)
    page.wait_for_timeout(1500)                       # ≥ 1 s of fake-mic audio
    stop.click()
    page.locator(".toast-host").get_by_text("Voiceover recorded").wait_for(timeout=30_000)
    after = vo_clips()
    assert len(after) == len(before) + 1, (before, after)
    assert stop.count() == 0
    page.context.close()


@pytest.mark.parametrize("size", list(GRID))
def test_r2_reference_screenshots(engine, base_url, sessions, size):  # noqa: F811
    """The Text and Captions panels at the four spec sizes, beside the mock."""
    for label in ("Text", "Captions"):
        page = _open(engine, base_url, sessions["full"], *size)
        _tab(page, label).click()
        page.wait_for_timeout(500)
        assert page.evaluate("document.documentElement.scrollWidth") <= size[0]
        panel = page.locator(f"#tool-panel-{label.lower()}")
        sw, cw = panel.evaluate("el => [el.scrollWidth, el.clientWidth]")
        assert sw <= cw, (label, size, sw, cw)       # nothing in the panel overflows sideways
        page.screenshot(path=str(SHOTS / f"rail-r2-{engine.engine_name}-{label.lower()}-{size[0]}x{size[1]}.png"))
        page.context.close()


# --------------------------------------------------------------- case 10 ----

SEQ_JS = r"""
(startId) => {
  const all = [...document.querySelectorAll('button, input, select, textarea, a[href], [tabindex]')]
    .filter(el => el.tabIndex >= 0 && !el.disabled && el.getClientRects().length && !el.closest('[hidden], [inert]')
                  && getComputedStyle(el).visibility !== 'hidden')
  const i = all.indexOf(document.getElementById(startId))
  return all.slice(i + 1, i + 3).map(el => el.getAttribute('aria-label') || el.textContent.trim().slice(0, 30))
}
"""


def test_tab_goes_from_the_rail_into_its_panel(engine, base_url, sessions):  # noqa: F811
    page = _open(engine, base_url, sessions["full"], 1440, 900)
    # The sequential focus order (DOM order, no positive tabindex anywhere).
    nxt = page.evaluate(SEQ_JS, "rail-tab-media")
    assert nxt[0] == "Hide the tool panel", nxt
    assert nxt[1].startswith("Drop video"), nxt
    assert page.evaluate("document.querySelectorAll('[tabindex]:not([tabindex=\"0\"]):not([tabindex=\"-1\"])').length") == 0
    if engine.engine_name == "chromium":
        # WebKit skips buttons on Tab unless macOS Full Keyboard Access is on
        # (measured: Tab from an input lands on <body>), so the keypresses are
        # Chromium's; the order they follow is the DOM order checked above.
        _tab(page, "Media").focus()
        page.keyboard.press("Tab")
        assert _active(page)["label"] == "Hide the tool panel", _active(page)
        page.keyboard.press("Tab")
        assert _active(page)["text"].startswith("Drop video"), _active(page)
    page.context.close()


# ---------------------------------------------------------- screenshots ----

@pytest.mark.parametrize("size", list(GRID))
def test_reference_screenshots(engine, base_url, sessions, size):  # noqa: F811
    page = _open(engine, base_url, sessions["full"], *size)
    assert page.evaluate("document.documentElement.scrollWidth") <= size[0]
    page.screenshot(path=str(SHOTS / f"rail-r1-{engine.engine_name}-{size[0]}x{size[1]}.png"))
    page.context.close()
