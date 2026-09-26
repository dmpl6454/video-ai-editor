"""Wave D, left tool rail, phase R4 — the keyboard, measured in a real browser,
in Chromium AND WebKit (docs/design/LEFT_RAIL_SPEC.md §4, §8.3 case 5).

What each test pins:

* Case 5 (critique H1): a panel chord is ``global`` — ⌥8 switches from a media
  row and ⌥1 from inside the AI panel, both ``[data-keymap-ignore]`` scopes;
  ⌘E opens Export from the right panel's tablist (another ignore scope); in
  the Prompt textarea ⌥1 is left to typing.
* §2.4 restored (review RD1 → R4): Space and Enter on the FOCUSED rail tab
  collapse / re-open the tool panel, and nothing else is taken from the rail.
* The keyboard pass: ⌘Z, J/K/L, N and Space keep working with focus on the
  rail, in a panel, on the timeline and in the Inspector — including a Speed
  preset radio and the Inspector tab (review RD2: ignore scopes there
  silenced them); ⌘Z right after picking a preset undoes it.
* Text fields (review RD2): ⌘E and ⌥⌘K work from the Prompt bar; ⌥0 then ⌥9
  goes Chat → Inspector; Esc in an empty Prompt bar or Chat box hands the
  keyboard back to the timeline; F6 reaches the timeline canvas.
* ⌥\\, ⌥9 / ⌥0, ⌥T, ⌥⌘K and F6 / ⇧F6, the "Panels" group in Help and in
  Keyboard shortcuts, and no chord acting behind an open modal.

Harness: test_frontend_a11y's server and sessions (VAE_A11Y_BASE_URL for a
running Vite dev server), test_wave_d_rail_ui's engines and page helpers.
Playwright's WebKit is the automated stand-in for the app's WKWebView.
"""
from __future__ import annotations

import json
import time

import httpx
import pytest

from test_frontend_a11y import base_url, sessions  # noqa: F401  (fixtures)
from test_wave_d_rail_ui import SHOTS, _active, _open, _playing, _snap, _tab, engine, pw  # noqa: F401

DIGIT = {"media": 1, "audio": 2, "text": 3, "stickers": 4, "effects": 5, "transitions": 6, "captions": 7, "ai": 8}
PANELS_ROWS = ["Show or hide the Media panel", "Show or hide the Audio panel", "Show or hide the tool panel",
               "Show the Inspector", "Show the Chat", "Focus the next region"]


def _markers(base_url, sid) -> int:  # noqa: F811
    return len(httpx.get(f"{base_url}/api/sessions/{sid}/edl", timeout=10).json().get("markers") or [])


def _text_clips(base_url, sid) -> int:  # noqa: F811
    edl = httpx.get(f"{base_url}/api/sessions/{sid}/edl", timeout=10).json()
    return sum(len(t.get("clips") or []) for t in edl.get("tracks") or [] if t.get("type") == "text")


def _until(fn, want, timeout=8.0):
    """Poll a server-side value until it equals ``want`` (an edit is async)."""
    deadline = time.monotonic() + timeout
    got = fn()
    while got != want and time.monotonic() < deadline:
        time.sleep(0.1)
        got = fn()
    return got


def _selected(page) -> str:
    return page.evaluate("document.querySelector('nav.rail [role=tab][aria-selected=true]').id.replace('rail-tab-', '')")


def _wait_selected(page, rid: str) -> None:
    page.wait_for_function("id => document.querySelector('nav.rail [role=tab][aria-selected=true]')?.id === 'rail-tab-' + id",
                           arg=rid, timeout=3000)


def _panel_open(page) -> bool:
    return page.locator("#tool-panel").is_visible()


# ------------------------------------------------------------ case 5 (H1) ----

def test_panel_chords_run_inside_ignore_scopes_and_not_in_text(engine, base_url, sessions):  # noqa: F811
    page = _open(engine, base_url, sessions["full"], 1440, 900)
    assert _selected(page) == "media"
    # 1. A media row is a [data-keymap-ignore] scope: ⌥8 still selects AI.
    row = page.locator("[data-media-row]").first
    row.focus()
    assert row.evaluate("el => !!el.closest('[data-keymap-ignore]')")
    page.keyboard.press("Alt+8")
    _wait_selected(page, "ai")
    assert page.locator("#tool-panel-ai").is_visible()
    # Focus was inside the Media panel that hid: rescued to the new tab (§5.3).
    assert _active(page)["id"] == "rail-tab-ai", _active(page)
    # 2. Inside the AI panel (also an ignore scope), ⌥1 selects Media.
    tool = page.locator("#tool-panel-ai [data-keymap-ignore] button:visible").first
    tool.focus()
    assert tool.evaluate("el => !!el.closest('[data-keymap-ignore]')")
    page.keyboard.press("Alt+1")
    _wait_selected(page, "media")
    # 3. From the right panel's Inspector tab ⌘E opens Export.
    _tab(page, "Inspector").focus()
    page.keyboard.press("ControlOrMeta+e")
    dialog = page.get_by_role("dialog")
    dialog.wait_for(timeout=3000)
    assert dialog.locator("h2").inner_text().startswith("Export"), dialog.locator("h2").inner_text()
    # No chord acts behind a modal: ⌥5 inside the Export dialog switches nothing.
    page.keyboard.press("Alt+5")
    page.wait_for_timeout(200)
    assert _selected(page) == "media"
    page.keyboard.press("Escape")
    dialog.wait_for(state="detached", timeout=3000)
    # 4. In the Prompt textarea ⌥1 is typing, not a panel switch.
    page.get_by_role("application", name="Timeline").focus()
    page.keyboard.press("Alt+5")
    _wait_selected(page, "effects")
    prompt = page.locator(".center-head textarea").first
    prompt.focus()
    page.keyboard.press("Alt+1")
    page.wait_for_timeout(200)
    assert _selected(page) == "effects"
    assert _active(page)["tag"] == "TEXTAREA"
    # 5. ...but a ⌘ chord that types nothing runs from there (review RD2):
    # ⌘E opens Export, and ⌘A stays the field's own select-all.
    page.keyboard.press("ControlOrMeta+e")
    dialog.wait_for(timeout=3000)
    page.keyboard.press("Escape")
    dialog.wait_for(state="detached", timeout=3000)
    prompt.focus()
    prompt.fill("keep me")
    page.keyboard.press("ControlOrMeta+a")
    assert prompt.evaluate("el => el.selectionEnd - el.selectionStart") == len("keep me")
    page.context.close()


def test_a_panel_chord_toggles_and_never_moves_focus(engine, base_url, sessions):  # noqa: F811
    page = _open(engine, base_url, sessions["full"], 1280, 800)
    timeline = page.get_by_role("application", name="Timeline")
    timeline.focus()
    # ⌥1…⌥8 select every panel the rail shows, by the spec's digit (§2.2).
    shown = page.evaluate("[...document.querySelectorAll('nav.rail [role=tab]')].map(t => t.id.replace('rail-tab-', ''))")
    assert shown[0] == _selected(page) == "media"
    for rid in shown[1:] + shown[:1]:                  # each press is a switch, Media last
        page.keyboard.press(f"Alt+{DIGIT[rid]}")
        _wait_selected(page, rid)
        assert page.locator(f"#tool-panel-{rid}").is_visible(), rid
        assert _tab(page, page.locator(f"#rail-tab-{rid} .rail-label").inner_text()).get_attribute(
            "aria-keyshortcuts") == f"Alt+{DIGIT[rid]}"
    page.keyboard.press("Alt+2")
    _wait_selected(page, "audio")
    assert _panel_open(page)
    page.keyboard.press("Alt+2")                      # its own chord again collapses
    page.wait_for_function("() => document.getElementById('tool-panel').hidden")
    assert _tab(page, "Audio").get_attribute("aria-expanded") == "false"
    page.keyboard.press("Alt+2")
    page.wait_for_function("() => !document.getElementById('tool-panel').hidden")
    page.keyboard.press("Alt+Backslash")              # ⌥\ toggles the panel
    page.wait_for_function("() => document.getElementById('tool-panel').hidden")
    page.keyboard.press("Alt+Backslash")
    page.wait_for_function("() => !document.getElementById('tool-panel').hidden")
    assert _selected(page) == "audio"
    assert _active(page)["label"] == "Timeline", _active(page)
    page.context.close()


# ------------------------------------------------- §2.4 restored (RD1) ----

def test_space_and_enter_toggle_only_the_focused_rail_tab(engine, base_url, sessions):  # noqa: F811
    page = _open(engine, base_url, sessions["full"], 1280, 800)
    _tab(page, "Media").focus()
    page.keyboard.press("ArrowDown")
    assert _active(page)["id"] == "rail-tab-audio"
    for key in ("Space", "Enter"):
        page.keyboard.press(key)
        page.wait_for_function("() => document.getElementById('tool-panel').hidden")
        assert _tab(page, "Audio").get_attribute("aria-expanded") == "false", key
        assert not _playing(page), f"{key} on the focused rail tab also played"
        page.keyboard.press(key)
        page.wait_for_function("() => !document.getElementById('tool-panel').hidden")
        assert _tab(page, "Audio").get_attribute("aria-expanded") == "true", key
    assert _active(page)["id"] == "rail-tab-audio"
    # A click on another tab while the rail has keyboard focus takes the focus
    # along (roving tabindex), so the next Space toggles what was clicked.
    _tab(page, "Effects").click()
    assert _active(page)["id"] == "rail-tab-effects", _active(page)
    page.keyboard.press("Space")
    page.wait_for_function("() => document.getElementById('tool-panel').hidden")
    page.keyboard.press("Space")
    page.wait_for_function("() => !document.getElementById('tool-panel').hidden")
    # Off the rail, Space is play/pause: a mouse click never PUTS focus on a tab.
    page.get_by_role("application", name="Timeline").focus()
    _tab(page, "Stickers").click()
    assert _active(page)["label"] == "Timeline", _active(page)
    page.keyboard.press("Space")
    page.wait_for_function("() => document.querySelector('.timeline-toolbar button[aria-keyshortcuts=Space]').getAttribute('aria-label') === 'Pause'")
    assert _panel_open(page)
    page.keyboard.press("Space")
    page.context.close()


# ------------------------------------------------------- keyboard pass ----

# Records every label the transport button takes from now on.
RECORD_TRANSPORT = """() => {
  window.__r4Obs?.disconnect()
  const btn = document.querySelector('.timeline-toolbar button[aria-keyshortcuts=Space]')
  window.__r4Labels = []
  window.__r4Obs = new MutationObserver(() => window.__r4Labels.push(btn.getAttribute('aria-label')))
  window.__r4Obs.observe(btn, { attributes: true, attributeFilter: ['aria-label'] })
}"""


def _focus_on(page, where: str):
    if where == "rail":
        page.locator("nav.rail [role=tab][aria-selected=true]").focus()
    elif where == "panel":
        page.get_by_role("button", name="Add music…").focus()
    elif where == "timeline":
        page.get_by_role("application", name="Timeline").focus()
    elif where == "inspector":
        page.get_by_role("slider", name="Brightness").focus()
    elif where == "inspector-tab":
        _tab(page, "Inspector").focus()
    elif where == "speed-preset":
        _pick_speed_preset(page, "montage")
    return _active(page)


def _pick_speed_preset(page, preset: str):
    """Curve mode, then a preset chosen with the mouse (an edit); focus left
    on its radio as Chromium leaves it (WebKit does not focus a clicked button,
    so it is focused explicitly: the review's repro)."""
    page.get_by_role("radio", name="Curve").click()
    radio = page.locator(f".speed-preset[data-preset={preset}]")
    radio.click()
    page.wait_for_function("p => document.querySelector(`.speed-preset[data-preset=${p}]`)?.getAttribute('aria-checked') === 'true'", arg=preset)
    radio.focus()
    return radio


@pytest.mark.parametrize("where", ["rail", "panel", "timeline", "inspector", "inspector-tab", "speed-preset"])
def test_global_shortcuts_work_with_focus_anywhere(engine, base_url, sessions, where):  # noqa: F811
    sid = sessions["full"]
    page = _open(engine, base_url, sid, 1440, 900)
    _tab(page, "Audio").click()                          # the panel whose control we focus
    timeline = page.get_by_role("application", name="Timeline")
    timeline.focus()
    page.keyboard.press("ControlOrMeta+a")                        # select the clip: the Inspector fills
    page.get_by_role("slider", name="Brightness").wait_for(timeout=5000)
    page.keyboard.press("Shift+ArrowRight")              # playhead to 1 s, so J has room to play
    page.wait_for_timeout(150)
    at = _focus_on(page, where)

    # ⌘Z: add a marker (M), then undo it, without moving focus.
    before = _markers(base_url, sid)
    page.keyboard.press("m")
    assert _until(lambda: _markers(base_url, sid), before + 1) == before + 1, f"M did nothing on {where}"
    page.keyboard.press("ControlOrMeta+z")
    assert _until(lambda: _markers(base_url, sid), before) == before, f"⌘Z did nothing on {where}"
    assert _active(page) == at, (where, _active(page))

    # N toggles snapping.
    snap = _snap(page)
    page.keyboard.press("n")
    page.wait_for_function("was => document.querySelector('[aria-label=Snapping]')?.getAttribute('aria-pressed') !== was", arg=snap)
    page.keyboard.press("n")
    page.wait_for_function("was => document.querySelector('[aria-label=Snapping]')?.getAttribute('aria-pressed') === was", arg=snap)

    # J / K / L. The transport button's label is recorded on every change, so
    # a reverse play that reaches frame 0 and stops within a frame still
    # counts. K must be seen stopping a RUNNING playback: if the play before
    # it already ran out on its own, the pair is pressed again.
    playing = "() => document.querySelector('.timeline-toolbar button[aria-keyshortcuts=Space]').getAttribute('aria-label') === 'Pause'"
    stopped = playing.replace("=== 'Pause'", "!== 'Pause'")

    def press(key: str, want: str) -> None:
        page.evaluate(RECORD_TRANSPORT)
        page.keyboard.press(key)
        try:
            page.wait_for_function("want => (window.__r4Labels || []).includes(want)", arg=want, timeout=3000)
        except Exception as e:  # noqa: BLE001 — name the key that failed
            raise AssertionError(f"{key.upper()} on {where}: labels {page.evaluate('window.__r4Labels')}, "
                                 f"focus {_active(page)}") from e

    for _attempt in range(3):
        press("l", "Pause")
        if page.evaluate(playing):
            press("k", "Play")
            break
    else:
        pytest.fail(f"L never left playback running long enough to press K on {where}")
    press("j", "Pause")
    page.keyboard.press("k")
    page.wait_for_function(stopped, timeout=3000)

    # Space: play / pause — except on a focused tab: the rail's toggles the
    # tool panel (§2.4), the Inspector tab activates itself (APG), and the
    # transport is left alone.
    page.keyboard.press("Space")
    if where == "rail":
        page.wait_for_function("() => document.getElementById('tool-panel').hidden")
        assert not _playing(page)
        page.keyboard.press("Space")
        page.wait_for_function("() => !document.getElementById('tool-panel').hidden")
    elif where == "inspector-tab":
        page.wait_for_timeout(300)
        assert not _playing(page)
        assert _tab(page, "Inspector").get_attribute("aria-selected") == "true"
    else:
        page.wait_for_function(playing, timeout=3000)
        assert _panel_open(page)
        page.keyboard.press("Space")
        page.wait_for_function(stopped, timeout=3000)
    assert _active(page) == at, (where, _active(page))
    page.context.close()


# ------------------------------------------- the rest of §4.1 ----

def test_inspector_and_chat_chords(engine, base_url, sessions):  # noqa: F811
    page = _open(engine, base_url, sessions["full"], 1280, 800)
    page.get_by_role("button", name="Hide the Inspector and Chat panel").click()
    timeline = page.get_by_role("application", name="Timeline")
    timeline.focus()
    page.keyboard.press("Alt+9")
    page.wait_for_function("() => document.getElementById('right-tab-inspect')?.getAttribute('aria-selected') === 'true' && !document.getElementById('right-panel-inspect').hidden")
    assert _active(page)["label"] == "Timeline"         # the Inspector does not take focus
    page.keyboard.press("Alt+0")
    in_chat = "() => document.activeElement?.tagName === 'TEXTAREA' && !!document.activeElement.closest('#right-panel-chat')"
    page.wait_for_function(in_chat)
    assert _tab(page, "Chat").get_attribute("aria-selected") == "true"
    # Review RD2: ⌥9 from the Chat box brings the Inspector back, focus on its tab.
    page.keyboard.press("Alt+9")
    page.wait_for_function("() => document.getElementById('right-tab-inspect')?.getAttribute('aria-selected') === 'true'")
    page.wait_for_function("() => document.activeElement?.id === 'right-tab-inspect'")
    # Esc in an EMPTY chat box hands the keyboard back to the timeline.
    page.keyboard.press("Alt+0")
    page.wait_for_function(in_chat)
    page.keyboard.press("Escape")
    page.wait_for_function("() => document.activeElement?.getAttribute('aria-label') === 'Timeline'")
    # ...and so does Esc in an empty Prompt bar.
    page.locator(".center-head textarea").first.focus()
    page.keyboard.press("Escape")
    page.wait_for_function("() => document.activeElement?.getAttribute('aria-label') === 'Timeline'")
    page.context.close()


def test_undo_right_after_picking_a_speed_preset_undoes_it(engine, base_url, sessions):  # noqa: F811
    """Review RD2 repro: click Curve, click Montage (focus on its radio), ⌘Z.
    The Speed radiogroups were ignore scopes, so ⌘Z did nothing there."""
    sid = sessions["full"]
    page = _open(engine, base_url, sid, 1440, 900)
    page.get_by_role("application", name="Timeline").focus()
    page.keyboard.press("ControlOrMeta+a")
    page.get_by_role("slider", name="Brightness").wait_for(timeout=5000)

    def speeds() -> str:
        edl = httpx.get(f"{base_url}/api/sessions/{sid}/edl", timeout=10).json()
        return json.dumps([c.get("speed") for t in edl.get("tracks") or [] for c in t.get("clips") or []], sort_keys=True)

    before = speeds()
    # a preset the clip does not already play (the session is shared)
    page.get_by_role("radio", name="Curve").click()
    current = page.evaluate("() => document.querySelector('.speed-preset[aria-checked=true]')?.dataset.preset ?? ''")
    radio = _pick_speed_preset(page, "hero" if current != "hero" else "bullet")
    deadline = time.monotonic() + 8
    while speeds() == before and time.monotonic() < deadline:
        time.sleep(0.1)
    assert speeds() != before, "picking the preset changed nothing"
    assert _active(page)["tag"] == "BUTTON" and radio.get_attribute("aria-checked") == "true"
    page.keyboard.press("ControlOrMeta+z")
    assert _until(speeds, before) == before, "⌘Z on the focused preset radio did not undo it"
    page.screenshot(path=str(SHOTS / f"rd2_speed_undo_{engine.engine_name}.png"))
    page.context.close()


def test_f6_walks_the_regions_even_from_a_text_field(engine, base_url, sessions):  # noqa: F811
    page = _open(engine, base_url, sessions["full"], 1440, 900)
    region = """() => { const a = document.activeElement
      for (const [id, sel] of [['topbar', 'header.topbar'], ['rail', 'nav.rail'], ['panel', '#tool-panel'],
                               ['prompt', 'main.center .prompt-bar'], ['timeline', 'main.center'],
                               ['right', '#right-panel'], ['foot', 'nav.rail-foot']])
        if (document.querySelector(sel)?.contains(a)) return id
      return 'none' }"""
    _tab(page, "Media").focus()
    seen = []
    for _ in range(7):
        page.keyboard.press("F6")
        seen.append(page.evaluate(region))
        if seen[-1] == "timeline":
            # review RD2: the timeline stop is the timeline canvas itself
            assert _active(page)["label"] == "Timeline", _active(page)
    # The rail foot (Help, Shortcuts, Settings) is the last region since R3.
    assert seen == ["panel", "prompt", "timeline", "right", "foot", "topbar", "rail"], seen
    assert _active(page)["id"] == "rail-tab-media"      # the rail's stop is the selected tab
    page.keyboard.press("Shift+F6")
    assert page.evaluate(region) == "topbar"
    # From the Prompt textarea (text entry): F6 is 'anywhere'.
    page.locator(".center-head textarea").first.focus()
    page.keyboard.press("F6")
    assert page.evaluate(region) == "timeline"
    # A collapsed tool panel is skipped.
    page.keyboard.press("Alt+Backslash")
    page.wait_for_function("() => document.getElementById('tool-panel').hidden")
    _tab(page, "Media").focus()
    page.keyboard.press("F6")
    assert page.evaluate(region) == "prompt"
    page.context.close()


def test_alt_t_adds_a_selected_text_clip_at_the_playhead(engine, base_url, sessions):  # noqa: F811
    sid = sessions["full"]
    page = _open(engine, base_url, sid, 1440, 900)
    before = _text_clips(base_url, sid)
    # From inside an ignore scope (a media row): ⌥T is global.
    page.locator("[data-media-row]").first.focus()
    page.keyboard.press("Alt+t")
    assert _until(lambda: _text_clips(base_url, sid), before + 1) == before + 1
    # Selected: the Inspector shows the text clip's own field.
    page.get_by_role("textbox", name="Text").first.wait_for(timeout=5000)
    page.get_by_role("application", name="Timeline").focus()
    page.keyboard.press("ControlOrMeta+z")
    assert _until(lambda: _text_clips(base_url, sid), before) == before
    page.context.close()


def test_option_command_k_opens_keyboard_shortcuts_with_a_panels_group(engine, base_url, sessions):  # noqa: F811
    page = _open(engine, base_url, sessions["full"], 1440, 900)
    page.get_by_role("application", name="Timeline").focus()
    page.keyboard.press("ControlOrMeta+Alt+k")
    dialog = page.get_by_role("dialog", name="Keyboard shortcuts")
    dialog.wait_for(timeout=3000)
    group = dialog.get_by_role("region", name="Panels")
    names = group.locator(".shortcuts-name").all_inner_texts()
    for want in PANELS_ROWS:
        assert any(n.startswith(want) for n in names), (want, names)
    caps = group.locator("[data-keycap]").all_inner_texts()
    assert any(c in ("⌥1", "Alt+1") for c in caps), caps
    assert any(c in ("⌥\\", "Alt+\\") for c in caps), caps
    group.screenshot(path=str(SHOTS / f"r4_shortcuts_panels_{engine.engine_name}.png"))
    page.keyboard.press("Escape")
    dialog.wait_for(state="detached", timeout=3000)
    # Help ("?") lists the same group, generated from the same registry.
    page.keyboard.press("Shift+Slash")
    help_ = page.get_by_role("dialog", name="Keyboard shortcuts")
    help_.wait_for(timeout=3000)
    rows = help_.get_by_role("region", name="Panels").locator("tr")
    assert rows.count() >= len(PANELS_ROWS)
    first = help_.locator("[data-help-row=panelMedia]")
    assert first.locator(".help-label").inner_text() == "Show or hide the Media panel"
    assert first.locator("kbd").inner_text() in ("⌥1", "Alt+1")
    help_.get_by_role("region", name="Panels").screenshot(path=str(SHOTS / f"r4_help_panels_{engine.engine_name}.png"))
    page.context.close()

