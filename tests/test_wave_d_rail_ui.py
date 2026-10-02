"""The editor shell measured in a real browser, in Chromium AND WebKit.

Written for wave D's left tool rail (docs/design/LEFT_RAIL_SPEC.md §8.3) and
ported to the desktop shell of the 2026-10-02 design handoff: the rail's
eight panels are the asset browser's tab strip (`.ab-tabs`, lib/assetTabs),
the collapsible tool panel and the right panel's collapse rail are gone (the
three panels are grid tracks with splitters), and the transport lives in the
Player footer. What each surviving case pins:

1.  Names: every asset tab matches exactly one element by its exact label;
    the strip is one Tab stop with a roving tabindex (←/→/Home/End).
3.  A clicked tab leaves every global shortcut working (Space plays, N
    toggles snapping) and a keyboard-focused tab does not play on Space.
4.  Activity (§2.8): the top bar's activity chip keeps a voiceover's Stop and
    the captions' Cancel reachable with another tab showing; its polite live
    region exists before anything runs and speaks state changes only; the
    chip's body opens the tab that owns the run; the panel's own Cancel is
    the chip's Cancel; Fastest asks before a model download.
10. Tab order: Media tab → the sub-nav → the Import control.

Cases whose surface the redesign removed are skipped with that reason, not
rewritten against something else: the §1.2 grid columns and the tool-panel
collapse, the dragged width, the focus rescue on collapse, and the rail
tooltip (asset tabs carry native titles).

Harness (server, sessions) is test_frontend_a11y's: set VAE_A11Y_BASE_URL to
run against a server that is already up (a Vite dev server proxying /api),
otherwise it serves frontend/dist. Engines missing from the Playwright install
skip. Reference screenshots go to VAE_RAIL_SHOTS (default /tmp).
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

# lib/assetTabs.ASSET_TABS, in the reference order.
TABS = ["Media", "Audio", "Text", "Stickers", "Effects", "Transitions", "Captions", "Filters", "Adjustment",
        "Templates", "AI avatars"]
SIZES = [(1024, 768), (1280, 800), (1440, 900), (1920, 1080)]
SUPERSEDED = "superseded: the 2026-10-02 desktop shell has no tool rail, no collapsible tool panel and no right-panel rail"


def _slug(label: str) -> str:
    return label.replace(" ", "-").lower()


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
    items = {"vai.sessionId": sid, "vai.rightTab": "inspect", "aive.assetTab": "Media", **(extra or {})}
    ctx.add_init_script("try { " + " ".join(
        f"localStorage.setItem({json.dumps(k)}, {json.dumps(v)});" for k, v in items.items()) + " } catch (e) {}")
    page = ctx.new_page()
    page.goto(base_url + "/?vae-test")  # lib/testHook.ts: the store, in the built bundle too
    page.locator(".ab-tabs").get_by_role("tab", name="Media", exact=True).wait_for()
    page.locator(".timeline-canvas-wrap canvas").first.wait_for()
    page.wait_for_timeout(1200)
    return page


def _tab(page, label):
    """An asset-browser tab, scoped to the strip (the clip inspector has tabs
    of the same names)."""
    return page.locator(".ab-tabs").get_by_role("tab", name=label, exact=True)


def _panel(page, label):
    return page.locator(f"#asset-panel-{_slug(label)}")


def _active(page) -> dict:
    return page.evaluate("""() => { const a = document.activeElement
      return { id: a.id, tag: a.tagName, label: a.getAttribute('aria-label'), text: (a.textContent || '').trim().slice(0, 40) } }""")


def _js_click(locator):
    """Activate without moving focus — what a keyboard chord does (R4)."""
    locator.evaluate("el => el.click()")


# ---------------------------------------------------------------- case 1 ----

@pytest.mark.parametrize("size", [(1440, 900), (1024, 768)])
def test_each_asset_tab_has_exactly_its_label_as_name(engine, base_url, sessions, size):  # noqa: F811
    page = _open(engine, base_url, sessions["full"], *size)
    assert page.locator(".ab-tabs [role=tab]").count() == len(TABS)
    for label in TABS:
        assert _tab(page, label).count() == 1, label
        assert _tab(page, label).get_attribute("id") == f"asset-tab-{_slug(label)}"
    # A hover does not change any name.
    _tab(page, "Stickers").hover()
    page.wait_for_timeout(500)
    for label in TABS:
        assert _tab(page, label).count() == 1, label
    assert page.locator(".ab-tabs").get_by_role("tab", name=re.compile("—")).count() == 0
    page.context.close()


def test_the_strip_is_one_tab_stop_with_arrow_keys(engine, base_url, sessions):  # noqa: F811
    page = _open(engine, base_url, sessions["full"])
    tabbable = page.evaluate("[...document.querySelectorAll('.ab-tabs [role=tab]')].map(t => t.tabIndex)")
    assert sorted(tabbable) == [-1] * (len(TABS) - 1) + [0]
    _tab(page, "Media").focus()
    for key, want in (("ArrowRight", "Audio"), ("End", "AI avatars"), ("ArrowRight", "Media"), ("ArrowLeft", "AI avatars"),
                      ("Home", "Media")):
        page.keyboard.press(key)
        assert _tab(page, want).get_attribute("aria-selected") == "true", (key, want)
        assert _active(page)["id"] == f"asset-tab-{_slug(want)}", (key, _active(page))
        assert _panel(page, want).is_visible()
    page.context.close()


def _playing(page) -> bool:
    return page.locator(".pl-play").get_attribute("aria-label") == "Pause"


def _snap(page) -> str:
    return page.get_by_role("button", name="Main-track magnet", exact=True).get_attribute("aria-pressed")


def test_a_clicked_tab_leaves_every_global_shortcut_working(engine, base_url, sessions):  # noqa: F811
    """Review RD1 (HIGH): after a MOUSE click on a tab, Space must play and N
    must toggle snapping. The click must not park focus on the tab."""
    page = _open(engine, base_url, sessions["full"], 1280, 800)
    _tab(page, "Effects").click()
    page.wait_for_timeout(150)
    assert _tab(page, "Effects").get_attribute("aria-selected") == "true"
    assert _active(page)["id"] != "asset-tab-effects", _active(page)
    assert not _playing(page)
    page.keyboard.press("Space")
    page.wait_for_timeout(250)
    assert _playing(page), "Space after a tab click did not play"
    assert _panel(page, "Effects").is_visible(), "Space hid the panel"
    page.keyboard.press("Space")
    page.wait_for_timeout(150)
    before = _snap(page)
    page.keyboard.press("n")
    page.wait_for_timeout(150)
    assert _snap(page) != before, "N after a tab click did not toggle snapping"
    page.keyboard.press("n")
    page.context.close()


def test_keyboard_focus_on_the_strip_keeps_undo_and_playback(engine, base_url, sessions):  # noqa: F811
    """Review RD1: a keyboard user arrowing through the strip keeps the global
    shortcuts (N snaps; ⌘Z, J/K/L in test_wave_d_rail_keys_ui) — the strip is
    not an ignore scope. Space on the focused tab is the tab's own key (it
    presses the button) and does not play."""
    page = _open(engine, base_url, sessions["full"], 1280, 800)
    _tab(page, "Media").focus()
    page.keyboard.press("ArrowRight")
    assert _active(page)["id"] == "asset-tab-audio"
    page.keyboard.press("Space")
    page.wait_for_timeout(250)
    assert not _playing(page), "Space on a focused tab played"
    assert _panel(page, "Audio").is_visible()
    before = _snap(page)
    page.keyboard.press("n")
    page.wait_for_timeout(150)
    assert _snap(page) != before
    page.keyboard.press("n")
    page.context.close()


@pytest.mark.skip(reason=SUPERSEDED + " (no Tools navigation)")
def test_idle_ai_description_is_not_in_the_reading_order(engine, base_url, sessions):  # noqa: F811
    pass


@pytest.mark.parametrize("size", [(1000, 768), (1024, 768)])
def test_timeline_toolbar_is_never_clipped(engine, base_url, sessions, size):  # noqa: F811
    """The timeline spans the window now, but the toolbar must still fit at
    the narrowest supported widths with 'Zoom to fit' on screen."""
    page = _open(engine, base_url, sessions["full"], *size)
    bar = page.locator(".timeline-toolbar")
    sw, cw = bar.evaluate("el => [el.scrollWidth, el.clientWidth]")
    assert sw <= cw, (sw, cw)
    fit = page.get_by_role("button", name="Zoom to fit").bounding_box()
    box = bar.bounding_box()
    assert fit["x"] + fit["width"] <= box["x"] + box["width"] + 0.5, (fit, box)
    page.context.close()


# ---------------------------------------------------------------- case 2 ----

@pytest.mark.skip(reason=SUPERSEDED + " (the panels are fluid grid tracks, lib/workspaceLayout)")
@pytest.mark.parametrize("size", SIZES)
def test_grid_columns_per_breakpoint_and_no_hole_when_collapsed(engine, base_url, sessions, size):  # noqa: F811
    pass


@pytest.mark.skip(reason=SUPERSEDED + " (a splitter drag moves grid fractions, not a pixel width)")
def test_dragged_width_wins_over_the_breakpoint_default(engine, base_url, sessions):  # noqa: F811
    pass


# ---------------------------------------------------------------- case 6 ----

def test_focus_outside_the_browser_stays_put_when_the_tab_switches(engine, base_url, sessions):  # noqa: F811
    """What is left of the focus-rescue case: a tab switch never moves focus
    that was outside the asset browser, and a selected clip's inspector slider
    keeps focus across a switch too."""
    page = _open(engine, base_url, sessions["full"], 1440, 900)
    undo = page.get_by_role("button", name="Undo", exact=True)
    assert undo.is_enabled()
    undo.focus()
    _js_click(_tab(page, "Audio"))
    page.wait_for_timeout(150)
    assert _active(page)["label"] == "Undo", _active(page)
    cv = page.locator(".timeline-canvas-wrap canvas").first.bounding_box()
    page.mouse.click(cv["x"] + 140, cv["y"] + 40)
    slider = page.locator("#right-panel input[type=range]").first
    slider.wait_for(timeout=5000)
    slider.focus()
    _js_click(_tab(page, "Text"))
    page.wait_for_timeout(150)
    assert _active(page)["tag"] == "INPUT", _active(page)
    page.context.close()


def test_show_the_chat_focuses_the_message_box(engine, base_url, sessions):  # noqa: F811
    page = _open(engine, base_url, sessions["full"], 1280, 800)
    page.locator("button[aria-label='Chat with the assistant']").click()
    page.locator("#right-panel-chat textarea").wait_for(timeout=3000)
    # The chord (⌥0, the "Show the Chat" command) puts focus in the box.
    page.locator("button[aria-label='Chat with the assistant']").click()     # off
    page.wait_for_timeout(150)
    assert page.locator("#right-panel-chat").count() == 0
    page.locator(".timeline-canvas-wrap canvas").first.click(position={"x": 400, "y": 10})
    page.keyboard.press("Alt+Digit0")
    page.wait_for_timeout(300)
    assert page.locator("#right-panel-chat").count() == 1
    assert _active(page)["tag"] == "TEXTAREA", _active(page)
    # ⌥9 brings the Inspector back.
    page.keyboard.press("Alt+Digit9")
    page.wait_for_timeout(300)
    assert page.locator("#right-panel-chat").count() == 0
    page.context.close()


# ---------------------------------------------------------------- case 7 ----

@pytest.mark.skip(reason=SUPERSEDED + " (asset tabs carry native titles; the shared tooltip serves the chip)")
def test_tooltip_delay_placement_escape_and_hover(engine, base_url, sessions):  # noqa: F811
    pass


@pytest.mark.skip(reason=SUPERSEDED + " (asset tabs carry native titles; the chords stay in Help)")
def test_tooltip_carries_the_chord_once_the_keymap_binds_one(engine, base_url, sessions):  # noqa: F811
    pass


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
        for k, v in {"vai.sessionId": sid, "vai.rightTab": "inspect", "aive.assetTab": "Media"}.items()) + " } catch (e) {}")
    page = ctx.new_page()
    page.goto(base_url + "/?vae-test")  # lib/testHook.ts: the store, in the built bundle too
    page.locator(".ab-tabs").get_by_role("tab", name="Media", exact=True).wait_for()
    page.locator(".timeline-canvas-wrap canvas").first.wait_for()
    page.wait_for_timeout(1200)
    return page


def _inside_viewport(page, locator) -> bool:
    b = locator.bounding_box()
    vw = page.viewport_size["width"]
    return b is not None and b["x"] >= 0 and b["x"] + b["width"] <= vw and b["y"] >= 0 and b["width"] > 0


def _show_media(page):
    """Media selected: neither the Audio recorder nor the Captions form shows."""
    _js_click(_tab(page, "Media"))
    page.wait_for_timeout(200)
    assert _tab(page, "Media").get_attribute("aria-selected") == "true"
    assert page.locator("[data-auto-captions]").count() == 0
    assert page.get_by_role("button", name="Record voiceover").count() == 0


def _captions_progress(page):
    return page.locator("[data-auto-captions] .ab-progress")


def test_activity_region_exists_before_anything_runs(engine, base_url, sessions):  # noqa: F811
    page = _open(engine, base_url, sessions["full"])
    region = page.locator(".topbar [data-activity] [role=status]")
    assert region.count() == 1
    assert region.get_attribute("aria-live") == "polite"
    assert region.inner_text() == ""
    assert page.get_by_role("button", name="Stop recording").count() == 0
    assert page.get_by_role("button", name="Cancel captions").count() == 0
    # Text and Captions are asset tabs, not top-bar controls.
    assert page.locator(".topbar [data-text-presets], .topbar [data-auto-captions]").count() == 0
    page.context.close()


def test_stop_recording_from_the_chip_with_another_tab_showing(engine, base_url, sessions):  # noqa: F811
    page = _open_with(engine, base_url, sessions["full"], FAKE_VO_BRIDGE + LIVE_LOG)
    _tab(page, "Audio").click()
    page.get_by_role("button", name="Record voiceover").click()
    stop = page.get_by_role("button", name="Stop recording")
    stop.wait_for(timeout=8000)                      # after the 3-2-1 count-in
    assert page.evaluate("window.__vo.map(c => c[0])") == ["start"]
    _show_media(page)
    assert stop.is_visible() and _inside_viewport(page, stop)
    body = page.locator(".act-rec .act-body")
    assert re.fullmatch(r"Recording voiceover, 0:0\d\. Open the Audio panel", body.get_attribute("aria-label"))
    assert _tab(page, "Audio").count() == 1
    page.wait_for_timeout(1300)                      # a clock tick must not reach the live region
    page.screenshot(path=str(SHOTS / f"rail-r2-{engine.engine_name}-recording-collapsed.png"))
    stop.click()
    page.wait_for_timeout(500)
    assert page.evaluate("window.__vo.map(c => c[0])") == ["start", "stop"]
    assert stop.count() == 0
    assert page.evaluate("window.__live") == ["Recording a voiceover", "Recording stopped"]
    # The panel's recorder is back to idle too: one take, one stop.
    _js_click(_tab(page, "Audio"))
    page.get_by_role("button", name="Record voiceover").wait_for(timeout=3000)
    page.context.close()


def test_chip_body_opens_the_tab_that_owns_the_activity(engine, base_url, sessions):  # noqa: F811
    page = _open_with(engine, base_url, sessions["full"], FAKE_VO_BRIDGE)
    _tab(page, "Audio").click()
    page.get_by_role("button", name="Record voiceover").click()
    page.get_by_role("button", name="Stop recording").wait_for(timeout=8000)
    _show_media(page)
    page.locator(".act-rec .act-body").click()
    page.wait_for_timeout(200)
    assert _tab(page, "Audio").get_attribute("aria-selected") == "true"
    assert _panel(page, "Audio").is_visible()
    assert page.get_by_role("button", name="Stop recording").count() == 1
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


def _generate(page):
    _tab(page, "Captions").click()
    page.locator("[data-auto-captions]").wait_for()
    page.locator("[data-auto-captions]").get_by_role("button", name="Generate", exact=True).click()


def test_cancel_captions_from_the_chip_shows_stopping(engine, base_url, sessions):  # noqa: F811
    page = _open_with(engine, base_url, sessions["full"], LIVE_LOG)
    state = _stub_caption_job(page)
    _generate(page)
    cancel = page.get_by_role("button", name="Cancel captions")
    cancel.wait_for(timeout=5000)
    page.wait_for_function("document.querySelector('.act-cc .act-body').textContent.includes('42%')", timeout=5000)
    # The panel shows the SAME run: %, and its own Cancel.
    assert "42%" in _captions_progress(page).inner_text()
    _show_media(page)
    assert cancel.is_visible() and _inside_viewport(page, cancel)
    page.screenshot(path=str(SHOTS / f"rail-r2-{engine.engine_name}-captions-collapsed.png"))
    cancel.click()
    page.wait_for_function("document.querySelector('.act-cc .act-body').textContent.includes('Stopping…')", timeout=3000)
    assert state["cancels"] == 1
    assert cancel.is_disabled()                      # one cancel per run: the job is already stopping
    # The panel agrees (one run, never two Cancel paths).
    _js_click(_tab(page, "Captions"))
    page.wait_for_timeout(150)
    assert "Cancelling…" in _captions_progress(page).inner_text()
    assert _captions_progress(page).get_by_role("button", name="Cancel", exact=True).is_disabled()
    # The job acknowledges: the chip goes, the toast says so.
    cancel.wait_for(state="detached", timeout=8000)
    page.wait_for_timeout(300)
    assert "Auto captions was cancelled" in page.locator(".toast-host").inner_text()
    live = page.evaluate("window.__live")
    assert live == ["Captions started", "Captions 25%", "Stopping captions", "Captions cancelled"], live
    assert page.locator("[data-auto-captions]").get_by_role("button", name="Generate", exact=True).is_enabled()
    page.context.close()


def test_the_panel_cancel_is_the_chip_cancel(engine, base_url, sessions):  # noqa: F811
    page = _open_with(engine, base_url, sessions["full"], "")
    state = _stub_caption_job(page, ack_after_s=0.5)
    _generate(page)
    _captions_progress(page).get_by_role("button", name="Cancel", exact=True).click(timeout=5000)
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
    form = page.locator("[data-auto-captions]")
    form.wait_for()
    page.wait_for_timeout(400)
    form.get_by_role("radio", name="Fastest", exact=True).click()
    assert page.evaluate("localStorage.getItem('vai.captionSpeed')") == "fast"
    assert "1.6 GB" in form.inner_text()
    form.get_by_role("button", name="Generate", exact=True).click()
    dlg = page.get_by_role("dialog", name="Download a caption model?")
    dlg.wait_for(timeout=3000)
    text = dlg.locator(".dialog-text").inner_text()
    assert "1.6 GB" in text and text.endswith("Captions start when it finishes."), text
    dlg.get_by_role("button", name="Not now", exact=True).click()
    page.wait_for_timeout(300)
    assert not dlg.is_visible()
    assert page.evaluate("document.activeElement.textContent.trim()") == "Generate"
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
    _show_media(page)
    page.wait_for_timeout(1500)                       # ≥ 1 s of fake-mic audio
    stop.click()
    page.locator(".toast-host").get_by_text("Voiceover recorded").wait_for(timeout=30_000)
    after = vo_clips()
    assert len(after) == len(before) + 1, (before, after)
    assert stop.count() == 0
    page.context.close()


@pytest.mark.parametrize("size", SIZES)
def test_r2_reference_screenshots(engine, base_url, sessions, size):  # noqa: F811
    """The Text and Captions tabs at the four spec sizes, beside the mock."""
    for label in ("Text", "Captions"):
        page = _open(engine, base_url, sessions["full"], *size)
        _tab(page, label).click()
        page.wait_for_timeout(500)
        assert page.evaluate("document.documentElement.scrollWidth") <= size[0]
        panel = _panel(page, label)
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


def test_tab_goes_from_the_strip_into_its_panel(engine, base_url, sessions):  # noqa: F811
    page = _open(engine, base_url, sessions["full"], 1440, 900)
    # The sequential focus order (DOM order, no positive tabindex anywhere):
    # the tab strip, then the Media sub-nav (Import first), then its rows.
    nxt = page.evaluate(SEQ_JS, "asset-tab-media")
    assert nxt[0] == "Import", nxt
    assert nxt[1] == "Media", nxt
    assert page.evaluate("document.querySelectorAll('[tabindex]:not([tabindex=\"0\"]):not([tabindex=\"-1\"])').length") == 0
    if engine.engine_name == "chromium":
        # WebKit skips buttons on Tab unless macOS Full Keyboard Access is on
        # (measured: Tab from an input lands on <body>), so the keypresses are
        # Chromium's; the order they follow is the DOM order checked above.
        _tab(page, "Media").focus()
        page.keyboard.press("Tab")
        assert _active(page)["text"] == "Import", _active(page)
        page.keyboard.press("Tab")
        assert _active(page)["text"] == "Media", _active(page)
    page.context.close()


# ---------------------------------------------------------- screenshots ----

@pytest.mark.parametrize("size", SIZES)
def test_reference_screenshots(engine, base_url, sessions, size):  # noqa: F811
    page = _open(engine, base_url, sessions["full"], *size)
    assert page.evaluate("document.documentElement.scrollWidth") <= size[0]
    page.screenshot(path=str(SHOTS / f"rail-r1-{engine.engine_name}-{size[0]}x{size[1]}.png"))
    page.context.close()
