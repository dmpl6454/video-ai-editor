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
from pathlib import Path

import pytest

from test_frontend_a11y import base_url, sessions  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")
SHOTS = Path(os.environ.get("VAE_RAIL_SHOTS", "/tmp"))

RAIL = ["Media", "Audio", "Stickers", "Effects", "Transitions", "AI"]
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
    # stays the global play/pause until R4's Command.scope, review RD1).
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
    shortcuts (Space plays, N snaps) — the rail is not an ignore scope."""
    page = _open(engine, base_url, sessions["full"], 1280, 800)
    _tab(page, "Media").focus()
    page.keyboard.press("ArrowDown")
    assert _active(page)["id"] == "rail-tab-audio"
    page.keyboard.press("Space")
    page.wait_for_timeout(250)
    assert _playing(page), "Space on a focused rail tab did not play"
    assert page.locator("#tool-panel").is_visible()
    page.keyboard.press("Space")
    page.wait_for_timeout(150)
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
    # R1 binds no panel chords, so the tip names none (a dead key would lie).
    assert tip.locator("kbd").count() == 0
    assert _tab(page, "Stickers").get_attribute("aria-keyshortcuts") is None
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
    keymap = {"presetId": "capcut", "overrides": {"panelStickers": ["Alt+Digit4"]}}
    page = _open(engine, base_url, sessions["full"], 1440, 900, {"vae.keymap.v1": json.dumps(keymap)})
    tab = _tab(page, "Stickers")
    assert tab.get_attribute("aria-keyshortcuts") == "Alt+4"
    tab.hover()
    page.wait_for_timeout(450)
    kbd = page.locator(".rail-tip kbd")
    assert kbd.count() == 1 and kbd.inner_text() in ("⌥4", "Alt+4")
    # The name is still exactly the label.
    assert _tab(page, "Stickers").count() == 1
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
