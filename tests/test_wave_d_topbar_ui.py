"""The editor's top bar measured in a real browser, in Chromium AND WebKit.

Written for wave D's top-bar diet (docs/design/LEFT_RAIL_SPEC.md §1.4, §2.10,
§8.3 cases 3 and 9) and ported to the 2026-10-02 desktop shell: the bar is a
fixed flex row — home · brand · project name · save status · activity chip ·
File · Layout · Help · Settings · Shortcuts · Chat · Share · Export — with no
density steps and no Ratio in the middle (Ratio is the Player footer's menu;
the .vae links are the File menu). What each case pins now:

3.  Worst case at 900, 1024, 1280 and 1440 (one page per state, resized down
    and back up): a 60+ character project name, a voiceover recording and a
    captions run in the activity chip, and an export running. Nothing clips:
    the bar does not overflow, Export's box is inside the viewport and
    right-most, Stop recording / Cancel captions and every other control sit
    fully inside the bar, only the project name truncates.
9.  The Ratio menu: named, every pick closes the menu, returns focus to the
    trigger and changes the canvas; Esc returns focus.

Plus the three dialog openers (Help, Customize keyboard shortcuts, Settings —
one of each on the page, each opens its dialog), the .vae link going stale in
the File menu, and "From iPhone": absent — no markup, no /api/pair request —
while the backend reports `phone_pairing: false`.

Harness (server, sessions) is test_frontend_a11y's: set VAE_A11Y_BASE_URL to
run against a server that is already up (a Vite dev server proxying /api),
otherwise it serves frontend/dist. Screenshots go to VAE_RAIL_SHOTS (default
/tmp).
"""
from __future__ import annotations

import json
import re
import shutil
import time

import httpx
import pytest

from test_frontend_a11y import _clip, base_url, sessions  # noqa: F401  (fixtures)
from test_wave_d_rail_ui import (  # noqa: F401  (fixtures)
    FAKE_VO_BRIDGE, SHOTS, _js_click, _open, _open_with, _stub_caption_job, _tab, engine, pw,
)

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")

WIDTHS = [(1440, 900), (1280, 800), (1024, 768), (900, 724)]
LONG_NAME = "Episode 14 — the part nobody tells you about pricing (final cut v3)"
SUPERSEDED = "superseded: the 2026-10-02 top bar has no density steps (fixed controls, the name alone truncates)"


@pytest.fixture(scope="module")
def long_session(base_url, tmp_path_factory):  # noqa: F811
    """A project with a clip on v1 and the worst-case project name."""
    clip = _clip(tmp_path_factory.mktemp("topbar") / "topbar_clip.mp4")
    with httpx.Client(base_url=base_url, timeout=120) as c:
        sid = c.post("/api/sessions", json={"name": LONG_NAME}).json()["id"]
        with clip.open("rb") as fh:
            r = c.post(f"/api/sessions/{sid}/upload", files={"file": ("topbar_clip.mp4", fh, "video/mp4")},
                       data={"add_to_timeline": "true", "transcribe": "false"})
        assert r.status_code in (200, 202), r.text
    return sid


def _fresh_session(base_url, tmp_path_factory, name):  # noqa: F811
    clip = _clip(tmp_path_factory.mktemp("topbar") / "clip.mp4")
    with httpx.Client(base_url=base_url, timeout=120) as c:
        sid = c.post("/api/sessions", json={"name": name}).json()["id"]
        with clip.open("rb") as fh:
            c.post(f"/api/sessions/{sid}/upload", files={"file": ("clip.mp4", fh, "video/mp4")},
                   data={"add_to_timeline": "true", "transcribe": "false"})
    return sid


# ------------------------------------------------------------- seeding ----

def _stub_export(page):
    """POST /export?wait=0 answers a job whose poll never finishes."""
    def post(route):
        route.fulfill(json={"job_id": "job-ex", "status": "queued", "status_url": "/api/jobs/job-ex"})

    def job(route):
        route.fulfill(json={"id": "job-ex", "status": "running", "progress": 0.3, "result": None, "error": None})

    page.route(re.compile(r".*/api/sessions/[^/]+/export\?wait=0$"), post)
    page.route(re.compile(r".*/api/jobs/job-ex$"), job)


def _export(page):
    page.locator(".topbar-pinned button.primary").click()
    dlg = page.locator(".dlg-export")
    dlg.wait_for(timeout=5000)
    dlg.get_by_role("button", name="Export", exact=True).click()
    page.wait_for_function("() => /Exporting/.test(document.querySelector('.topbar-pinned button.primary').textContent)",
                           timeout=10_000)


def _start_activities(page):
    _tab(page, "Audio").click()
    page.get_by_role("button", name="Record voiceover").click()
    page.get_by_role("button", name="Stop recording").wait_for(timeout=10_000)
    _js_click(_tab(page, "Captions"))
    page.locator("[data-auto-captions]").get_by_role("button", name="Generate", exact=True).click()
    page.get_by_role("button", name="Cancel captions").wait_for(timeout=5000)
    page.wait_for_function("document.querySelector('.act-cc .act-body').textContent.includes('42%')", timeout=5000)
    _js_click(_tab(page, "Media"))


def _worst(engine, base_url, sid):  # noqa: F811
    page = _open_with(engine, base_url, sid, FAKE_VO_BRIDGE, 1440, 900)
    _stub_caption_job(page)
    _stub_export(page)
    _start_activities(page)             # before the export: its progress modal takes the pointer
    _export(page)
    return page


# ------------------------------------------------------------ measuring ----

MEASURE_JS = r"""
() => {
  const vw = document.documentElement.clientWidth
  const q = (s) => document.querySelector(s)
  const bar = q('.ed-topbar')
  const R = (e) => { const b = e.getBoundingClientRect(); return { l: b.left, r: b.right, w: b.width } }
  const name = (e) => e.getAttribute('aria-label') || e.textContent.trim().slice(0, 30)
  const bb = R(bar)
  const clipped = []
  for (const e of bar.querySelectorAll('button, a[href]')) {
    if (!e.getClientRects().length) continue
    const b = R(e)
    if (b.l < bb.l - 0.5 || b.r > bb.r + 0.5 || b.w < 1 || e.scrollWidth > e.clientWidth + 1) clipped.push([name(e), b.l, b.r])
  }
  const exp = R(q('.topbar-pinned button.primary'))
  const chip = R(q('.ed-project'))
  const stop = [...bar.querySelectorAll('button')].find(b => b.getAttribute('aria-label') === 'Stop recording')
  const cancel = [...bar.querySelectorAll('button')].find(b => b.getAttribute('aria-label') === 'Cancel captions')
  const inBar = (e) => { if (!e) return null; const b = R(e); return b.l >= bb.l - 0.5 && b.r <= bb.r + 0.5 && b.w > 0 }
  const last = [...bar.querySelectorAll('button, a[href]')].filter(e => e.getClientRects().length).map(R)
  const rightMost = Math.max(...last.map(b => b.r))
  return {
    vw, barSw: bar.scrollWidth, barCw: bar.clientWidth,
    exportL: exp.l, exportR: exp.r, rightMost, chipW: Math.round(chip.w),
    stopIn: inBar(stop), cancelIn: inBar(cancel), clipped,
    h1Display: getComputedStyle(q('h1.ed-brand')).display,
  }
}
"""


def _check(m: dict, w: int, ctx, busy: bool = False):
    assert m["barSw"] <= m["barCw"], (ctx, m)                         # the bar never overflows
    assert m["exportL"] >= 0 and m["exportR"] <= w, (ctx, m)          # Export fully on screen…
    assert abs(m["exportR"] - m["rightMost"]) <= 0.5, (ctx, m)        # …and right-most
    if busy:
        assert m["stopIn"] is True and m["cancelIn"] is True, (ctx, m)  # §8.3 case 3
    assert m["clipped"] == [], (ctx, m)                               # every control reachable
    assert m["chipW"] >= 40, (ctx, m)                                 # the name truncates, down to a floor
    assert m["h1Display"] != "none", (ctx, m)                         # the wordmark stays the page's h1


# ---------------------------------------------------------------- case 3 ----

def test_top_bar_worst_case_fits_at_every_width(engine, base_url, long_session):  # noqa: F811
    page = _worst(engine, base_url, long_session)
    try:
        for w, h in WIDTHS + [(1440, 900)]:
            page.set_viewport_size({"width": w, "height": h})
            page.wait_for_timeout(250)
            m = page.evaluate(MEASURE_JS)
            _check(m, w, (engine.engine_name, w), busy=True)
            page.screenshot(path=str(SHOTS / f"rail-r3-{engine.engine_name}-worst-{w}-topbar.png"),
                            clip={"x": 0, "y": 0, "width": w, "height": 44})
            if w == 1024:
                page.screenshot(path=str(SHOTS / f"rail-r3-{engine.engine_name}-worst-{w}x{h}.png"))
    finally:
        page.context.close()


@pytest.mark.skip(reason=SUPERSEDED)
def test_the_density_follows_content_not_just_width(engine, base_url, long_session):  # noqa: F811
    pass


@pytest.mark.parametrize("size", WIDTHS)
def test_everyday_bar_matches_the_baseline(engine, base_url, sessions, size):  # noqa: F811
    """No activity, a short name: nothing clips, the Share and Export controls
    keep their words, the save status is a live region."""
    page = _open(engine, base_url, sessions["full"], *size)
    m = page.evaluate(MEASURE_JS)
    _check(m, size[0], ("everyday", size))
    assert page.locator(".ed-save-status").get_attribute("role") == "status"
    # The word hides at narrow widths; the status's name always carries it.
    assert page.locator(".ed-save-status").get_attribute("aria-label") in ("Saved", "Saving…", "Offline")
    assert page.locator(".topbar-pinned button.primary").inner_text().strip() == "Export"
    page.screenshot(path=str(SHOTS / f"rail-r3-{engine.engine_name}-{size[0]}x{size[1]}.png"))
    page.context.close()


def test_a_saved_vae_goes_stale_in_the_file_menu(engine, base_url, long_session):  # noqa: F811
    """Save, then one edit (⌥T adds a text clip): the File menu's download link
    says the .vae is outdated, in its name."""
    page = _open_with(engine, base_url, long_session, "", 1440, 900)
    page.locator("button[aria-label='File']").click()
    menu = page.get_by_role("menu", name="File")
    menu.wait_for(timeout=3000)
    menu.get_by_role("menuitem", name="Save project file (.vae)").click()
    page.wait_for_timeout(500)
    page.locator("button[aria-label='File']").click()
    menu.get_by_role("menuitem", name="Download .vae", exact=True).wait_for(timeout=30_000)
    page.keyboard.press("Escape")
    page.wait_for_timeout(150)
    page.keyboard.press("Alt+t")
    page.wait_for_timeout(800)
    page.locator("button[aria-label='File']").click()
    menu.get_by_role("menuitem", name="Download .vae (outdated)", exact=True).wait_for(timeout=15_000)
    page.keyboard.press("Escape")
    page.context.close()


# ---------------------------------------------------------------- case 9 ----

def test_ratio_menu_picks_change_the_canvas_and_return_focus(engine, base_url, tmp_path_factory):  # noqa: F811
    sid = _fresh_session(base_url, tmp_path_factory, "ratio menu")
    page = _open(engine, base_url, sid, 1280, 800)
    trig = page.locator("button[aria-label='Ratio']")
    trig.focus()
    page.keyboard.press("Enter")
    menu = page.get_by_role("menu", name="Ratio")
    menu.wait_for(timeout=3000)
    for label in ("Original", "16:9", "9:16", "1:1", "Custom"):
        assert menu.get_by_role("menuitemradio", name=label, exact=True).count() == 1, label
    # Focus is in the menu; ↓ moves; Esc closes to the trigger.
    first = page.evaluate("document.activeElement.textContent")
    page.keyboard.press("ArrowDown")
    assert page.evaluate("document.activeElement.textContent") != first
    page.keyboard.press("Escape")
    page.wait_for_timeout(150)
    assert menu.count() == 0
    assert page.evaluate("document.activeElement === document.querySelector(\"button[aria-label='Ratio']\")")
    # An aspect pick closes the menu, returns focus and changes the canvas.
    trig.click()
    page.get_by_role("menuitemradio", name="9:16", exact=True).click()
    page.wait_for_timeout(150)
    assert menu.count() == 0
    assert page.evaluate("document.activeElement === document.querySelector(\"button[aria-label='Ratio']\")")
    deadline = time.time() + 15
    while True:
        canvas = httpx.get(f"{base_url}/api/sessions/{sid}/edl", timeout=30).json()["canvas"]
        if (canvas["w"], canvas["h"]) == (1080, 1920):
            break
        assert time.time() < deadline, canvas
        time.sleep(0.3)
    page.wait_for_function("() => document.querySelector(\"button[aria-label='Ratio']\").textContent.includes('9:16')",
                           timeout=10_000)
    trig.click()
    assert page.get_by_role("menuitemradio", name="9:16", exact=True).get_attribute("aria-checked") == "true"
    page.screenshot(path=str(SHOTS / f"rail-r3-{engine.engine_name}-ratio-menu.png"))
    page.keyboard.press("Escape")
    # No "⋯" menu and no bare <select> in the bar.
    assert page.locator(".ed-topbar select, [data-topbar-more]").count() == 0
    page.context.close()


# ------------------------------------------------------------ openers ----

OPENERS = ["Help", "Customize keyboard shortcuts", "Settings"]


def test_the_bar_holds_help_shortcuts_and_settings(engine, base_url, sessions):  # noqa: F811
    page = _open(engine, base_url, sessions["full"], 1024, 768)
    bar = page.locator("header.ed-topbar")
    for n in OPENERS:
        assert page.get_by_role("button", name=n, exact=True).count() == 1, n
        assert bar.get_by_role("button", name=n, exact=True).count() == 1, n
    for n, dialog in (("Help", "Keyboard shortcuts"), ("Settings", "Settings")):
        bar.get_by_role("button", name=n, exact=True).click()
        page.get_by_role("dialog", name=dialog).wait_for(timeout=3000)
        page.keyboard.press("Escape")
        page.get_by_role("dialog", name=dialog).wait_for(state="detached", timeout=3000)
    bar.get_by_role("button", name="Customize keyboard shortcuts", exact=True).click()
    page.locator("[role=dialog]").first.wait_for(timeout=3000)
    page.screenshot(path=str(SHOTS / f"rail-r3-{engine.engine_name}-foot-shortcuts.png"))
    page.context.close()


# ------------------------------------------------------------ From iPhone ----

def test_from_iphone_is_absent_while_phone_pairing_is_off(engine, base_url, sessions):  # noqa: F811
    urls = []
    page = _open(engine, base_url, sessions["full"], 1440, 900)
    page.on("request", lambda r: urls.append(r.url))
    # The real backend of this test run reports phone_pairing: false.
    version = page.evaluate("fetch('/api/version').then(r => r.json())")
    assert version.get("phone_pairing") is not True, version
    _js_click(_tab(page, "Audio"))
    _js_click(_tab(page, "Media"))
    page.wait_for_timeout(300)
    assert page.get_by_role("button", name="From iPhone").count() == 0
    assert not re.search(r"iphone|pair|\bQR\b", page.locator("body").inner_text(), re.I)
    assert page.locator("[data-icon=phone]").count() == 0
    assert [u for u in urls if "/api/pair" in u or "PhonePanel" in u] == []
    page.context.close()


@pytest.mark.skip(reason="superseded: the 2026-10-02 desktop shell ships without the phone companion (0.7.1 gate)")
def test_from_iphone_is_the_media_header_action_when_pairing_is_on(engine, base_url, sessions):  # noqa: F811
    pass


def test_the_bar_carries_only_the_designs_controls(engine, base_url, sessions):  # noqa: F811
    page = _open(engine, base_url, sessions["full"], 1440, 900)
    bar = page.locator("header.ed-topbar")
    assert bar.get_attribute("aria-label") == "Project"
    text = bar.inner_text()
    assert not re.search(r"\bv\d+\.\d+", text), text                 # the version lives in Help now
    assert bar.locator("select, [data-topbar-more], .topbar-canvas, .topbar-tools, .ratio-trigger").count() == 0
    names = bar.locator("button, a[href]").evaluate_all(
        "els => els.map(e => e.getAttribute('aria-label') || e.textContent.trim())")
    for want in ("Home", "File", "Layout", "Help", "Settings", "Customize keyboard shortcuts", "Chat with the assistant",
                 "Share", "Export"):
        assert want in names, (want, names)
    # Export is the last control in the bar.
    last = bar.locator("button, a[href]").last
    assert last.evaluate("e => e.matches('.topbar-pinned button.primary')")
    assert json.loads(page.evaluate("JSON.stringify(document.querySelector('.ed-project') !== null)"))
    page.context.close()
