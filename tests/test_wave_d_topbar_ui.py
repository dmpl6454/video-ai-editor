"""Wave D, left tool rail, phase R3 — the top-bar diet, measured in a real
browser, in Chromium AND WebKit (docs/design/LEFT_RAIL_SPEC.md §1.4, §2.10,
§8.3 cases 3 and 9).

R3 turned the top bar into a `minmax(0,1fr) auto minmax(max-content,1fr)` grid
— left: brand · wordmark · project · Applying · activity; centre: Ratio; right:
Save · Open · links · error · Export — and moved Help, Customize shortcuts and
Settings to the rail foot and the iPhone action to the Media panel header. When
content does not fit, `useTopBarFit` steps up a density that hides words, never
controls. What each case pins:

3.  Worst case, at 900, 1024, 1280 and 1440 (one page per state, resized down
    and back up): a 60+ character project name, a voiceover recording and a
    captions run in the activity chip, a stale .vae, and one of — a stale MP4
    plus "Applying", a long export error, or an export running. Nothing clips:
    `.topbar` does not overflow, Export's box is inside the viewport at
    viewport − 12, Stop recording / Cancel captions and every other control
    sit fully inside their group, only the project name truncates, Ratio is
    centred within 1 px whenever the right cluster fits in half the bar, and
    the density is the LOWEST step that fits (one step less overflows).
9.  The Ratio menu: "Canvas ratio" with three named groups, "TikTok safe zone"
    once, every pick closes the menu and returns focus to the trigger, the
    safe-zone pick drives the preview overlay, Esc returns focus.

Plus the rail foot (names, icons, last in DOM order, each opens its dialog,
tooltip to the right) and the From iPhone header action: absent — no markup,
no /api/pair request, not even the panel's code — while the backend reports
`phone_pairing: false`; present, in the §2.1 tab order, when it reports true.

Harness (server, sessions) is test_frontend_a11y's: set VAE_A11Y_BASE_URL to
run against a server that is already up (a Vite dev server proxying /api),
otherwise it serves frontend/dist. Screenshots go to VAE_RAIL_SHOTS (default
/tmp).
"""
from __future__ import annotations

import json
import re
import shutil

import httpx
import pytest

from test_frontend_a11y import _clip, base_url, sessions  # noqa: F401  (fixtures)
from test_wave_d_rail_ui import (  # noqa: F401  (fixtures)
    FAKE_VO_BRIDGE, SHOTS, _js_click, _open, _open_with, _stub_caption_job, _tab, engine, pw,
)

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")

WIDTHS = [(1440, 900), (1280, 800), (1024, 768), (900, 724)]
LONG_NAME = "Episode 14 — the part nobody tells you about pricing (final cut v3)"
LONG_ERROR = ("RuntimeError: the encoder ran out of disk space while writing the audio track "
              "(free some space on the export volume and export again)")


def _baseline(width: int) -> int:
    return 0 if width >= 1440 else 1 if width >= 1280 else 2 if width >= 1100 else 3


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

def _stub_export(page, outcome: str):
    """POST /export?wait=0 answers a job; its poll says `outcome`:
    'stale' completes with an EDL hash the timeline does not have (→ "MP4
    (outdated)"), 'error' fails with LONG_ERROR, 'running' never finishes."""
    def post(route):
        route.fulfill(json={"job_id": "job-ex", "status": "queued", "status_url": "/api/jobs/job-ex"})

    def job(route):
        if outcome == "running":
            return route.fulfill(json={"id": "job-ex", "status": "running", "progress": 0.3, "result": None, "error": None})
        if outcome == "error":
            return route.fulfill(json={"id": "job-ex", "status": "failed", "progress": 0.3, "result": None,
                                       "error": LONG_ERROR})
        route.fulfill(json={"id": "job-ex", "status": "completed", "progress": 1, "error": None, "result": {
            "url": "/api/sessions/x/files/exports/export_stale.mp4", "filename": "export_stale.mp4",
            "edl_hash": "0000000000000000"}})

    page.route(re.compile(r".*/api/sessions/[^/]+/export\?wait=0$"), post)
    page.route(re.compile(r".*/api/jobs/job-ex$"), job)
    page.route(re.compile(r".*/exports/export_stale\.mp4.*"), lambda r: r.fulfill(body=b"", content_type="video/mp4"))


def _hold_add_text(page) -> dict:
    """Holds the NEXT add_text dispatch open while `hold['on']` (the bar shows
    "Applying" for as long as it is pending); `release()` lets it through."""
    hold = {"on": False, "held": []}

    def handler(route):
        body = route.request.post_data_json or {}
        if hold["on"] and body.get("tool") == "add_text":
            hold["held"].append(route)
            return None
        return route.fallback()

    page.route(re.compile(r".*/api/sessions/[^/]+/dispatch(\?.*)?$"), handler)

    def release():
        hold["on"] = False
        for r in hold["held"]:
            r.fallback()
        hold["held"].clear()
    hold["release"] = release
    return hold


def _stale_vae(page):
    """Save, then one edit (⌥T adds a text clip): the .vae link goes stale."""
    page.get_by_role("button", name="Save", exact=True).click()
    page.wait_for_selector(".tb-right a.tb-dl", timeout=30_000)
    page.keyboard.press("Alt+t")
    page.wait_for_function(
        "() => (document.querySelector('.tb-right a.tb-dl')?.getAttribute('aria-label') || '').includes('(outdated)')",
        timeout=15_000)


def _export(page, then: str):
    page.locator(".topbar-pinned button.primary").click()
    dlg = page.get_by_role("dialog").filter(has=page.locator(".export-dialog-grid"))
    dlg.wait_for(timeout=5000)
    dlg.locator("button.primary", has_text="Export").click()
    if then == "stale":
        page.get_by_role("button", name="Save exported MP4 (outdated)").wait_for(timeout=10_000)
    elif then == "error":
        page.locator(".topbar-export-error").wait_for(timeout=10_000)
    else:
        page.wait_for_function("() => /Exporting/.test(document.querySelector('.topbar-pinned button.primary').textContent)",
                               timeout=10_000)


def _start_activities(page):
    _tab(page, "Audio").click()
    page.get_by_role("button", name="Record voiceover").click()
    page.get_by_role("button", name="Stop recording").wait_for(timeout=10_000)
    _js_click(_tab(page, "Captions"))
    page.get_by_role("button", name="Generate captions").click()
    page.get_by_role("button", name="Cancel captions").wait_for(timeout=5000)
    page.wait_for_function("document.querySelector('.act-cc .act-body').textContent.includes('42%')", timeout=5000)
    _js_click(_tab(page, "Media"))


def _worst(engine, base_url, sid, kind):  # noqa: F811
    page = _open_with(engine, base_url, sid, FAKE_VO_BRIDGE, 1440, 900)
    _stub_caption_job(page)
    _stub_export(page, {"links": "stale", "error": "error", "exporting": "running"}[kind])
    hold = _hold_add_text(page)
    _stale_vae(page)
    _start_activities(page)             # before the export: its progress modal takes the pointer
    _export(page, {"links": "stale", "error": "error", "exporting": "running"}[kind])
    if kind == "links":
        hold["on"] = True
        page.keyboard.press("Alt+t")
        page.locator(".tb-applying").wait_for(timeout=5000)
    return page, hold


# ------------------------------------------------------------ measuring ----

MEASURE_JS = r"""
() => {
  const vw = document.documentElement.clientWidth
  const q = (s) => document.querySelector(s)
  const bar = q('.topbar'), left = q('.tb-left'), center = q('.tb-center'), right = q('.tb-right')
  const R = (e) => { const b = e.getBoundingClientRect(); return { l: b.left, r: b.right, w: b.width } }
  const name = (e) => e.getAttribute('aria-label') || e.textContent.trim().slice(0, 30)
  const lb = R(left)
  const clipped = []
  for (const e of bar.querySelectorAll('button, a[href]')) {
    if (!e.getClientRects().length) continue
    const b = R(e)
    const group = e.closest('.tb-left') ? lb : { l: 0, r: vw }
    if (b.l < group.l - 0.5 || b.r > group.r + 0.5 || b.w < 1) clipped.push([name(e), b.l, b.r])
  }
  const exp = R(q('.topbar-pinned button.primary'))
  const trig = R(q('.ratio-trigger'))
  const chip = R(q('.topbar-session'))
  const rightW = R(right).w
  // The right cluster "fits" when its max-content is within its half of the bar.
  const fitsHalf = rightW <= (vw - R(center).w - 24) / 2
  const density = Number(bar.dataset.density)
  // Would one step LESS dense still fit? (It must not: the fit is the lowest step.)
  let lessFits = null
  if (density > 0) {
    bar.classList.remove('tb-d' + density)
    lessFits = left.scrollWidth <= left.clientWidth && bar.scrollWidth <= bar.clientWidth
    bar.classList.add('tb-d' + density)
  }
  const stop = [...bar.querySelectorAll('button')].find(b => b.getAttribute('aria-label') === 'Stop recording')
  const cancel = [...bar.querySelectorAll('button')].find(b => b.getAttribute('aria-label') === 'Cancel captions')
  const inLeft = (e) => { if (!e) return null; const b = R(e); return b.l >= lb.l - 0.5 && b.r <= lb.r + 0.5 && b.w > 0 }
  return {
    vw, density, lessFits, barSw: bar.scrollWidth, barCw: bar.clientWidth, leftSw: left.scrollWidth, leftCw: left.clientWidth,
    exportL: exp.l, exportR: exp.r, trigMid: (trig.l + trig.r) / 2, fitsHalf, chipW: Math.round(chip.w),
    stopIn: inLeft(stop), cancelIn: inLeft(cancel), clipped,
    h1Display: getComputedStyle(q('h1.topbar-brand')).display,
    actWords: q('.act-word') ? getComputedStyle(q('.act-word')).display : null,
    classes: bar.className,
  }
}
"""


def _check(m: dict, w: int, ctx, busy: bool = False):
    assert m["barSw"] == m["barCw"], (ctx, m)                         # the bar never overflows
    assert m["leftSw"] <= m["leftCw"], (ctx, m)                       # nor its left group
    assert m["exportL"] >= 0 and m["exportR"] <= w, (ctx, m)          # Export fully on screen…
    assert abs(m["exportR"] - (w - 12)) <= 0.5, (ctx, m)              # …and right-most, at viewport − 12
    if busy:
        assert m["stopIn"] is True and m["cancelIn"] is True, (ctx, m)  # §8.3 case 3
    assert m["clipped"] == [], (ctx, m)                               # every control reachable
    assert m["chipW"] >= 80, (ctx, m)                                 # the name truncates, down to its floor
    assert m["h1Display"] != "none", (ctx, m)                         # the wordmark stays the page's h1
    assert _baseline(w) <= m["density"] <= 4, (ctx, m)
    if m["density"] > _baseline(w):
        assert m["lessFits"] is False, (ctx, m)                       # the lowest step that fits
    if m["actWords"] is not None:                                   # words hide at step 4 only
        assert (m["actWords"] == "none") == (m["density"] == 4), (ctx, m)
    if m["fitsHalf"]:
        assert abs(m["trigMid"] - w / 2) <= 1, (ctx, m)               # Ratio centred (M7)


# ---------------------------------------------------------------- case 3 ----

@pytest.mark.parametrize("kind", ["links", "error", "exporting"])
def test_top_bar_worst_case_fits_at_every_width(engine, base_url, long_session, kind):  # noqa: F811
    page, hold = _worst(engine, base_url, long_session, kind)
    seen = {}
    try:
        for w, h in WIDTHS + [(1440, 900)]:
            page.set_viewport_size({"width": w, "height": h})
            page.wait_for_timeout(250)                    # resize → re-fit, plus the observer's frame
            m = page.evaluate(MEASURE_JS)
            _check(m, w, (engine.engine_name, kind, w), busy=True)
            if w in seen:                                 # back up at 1440: no density stuck from 900
                assert m["density"] == seen[w], (seen[w], m)
            seen[w] = m["density"]
            page.screenshot(path=str(SHOTS / f"rail-r3-{engine.engine_name}-worst-{kind}-{w}-topbar.png"),
                            clip={"x": 0, "y": 0, "width": w, "height": 44})
            if w == 1024:
                page.screenshot(path=str(SHOTS / f"rail-r3-{engine.engine_name}-worst-{kind}-{w}x{h}.png"))
        # The worst case really is worse than the everyday bar: somewhere a
        # step beyond the viewport baseline was needed.
        assert any(seen[w] > _baseline(w) for w, _ in WIDTHS), seen
    finally:
        hold["release"]()
        page.context.close()


def test_the_density_follows_content_not_just_width(engine, base_url, long_session):  # noqa: F811
    """With the activity running the bar needs a denser step than without, at
    the same width — and it steps back down when the activity ends."""
    page = _open_with(engine, base_url, long_session, FAKE_VO_BRIDGE, 1024, 768)
    _stub_caption_job(page, ack_after_s=0.2)
    idle = page.evaluate(MEASURE_JS)
    assert idle["density"] == 3, idle
    _check(idle, 1024, "idle")
    _start_activities(page)
    page.wait_for_timeout(250)
    busy = page.evaluate(MEASURE_JS)
    _check(busy, 1024, "busy", busy=True)
    assert busy["density"] == 4, busy
    page.get_by_role("button", name="Stop recording").click()
    page.get_by_role("button", name="Cancel captions").click()
    page.get_by_role("button", name="Cancel captions").wait_for(state="detached", timeout=8000)
    page.wait_for_timeout(250)
    after = page.evaluate(MEASURE_JS)
    assert after["density"] == 3, after
    page.context.close()


@pytest.mark.parametrize("size", WIDTHS)
def test_everyday_bar_matches_the_baseline(engine, base_url, sessions, size):  # noqa: F811
    """No activity, a short name: the viewport's baseline step, nothing clips,
    Ratio exactly centred (the right cluster fits)."""
    page = _open(engine, base_url, sessions["full"], *size)
    m = page.evaluate(MEASURE_JS)
    _check(m, size[0], ("everyday", size))
    assert m["density"] == _baseline(size[0]), m
    assert m["fitsHalf"] and abs(m["trigMid"] - size[0] / 2) <= 1, m
    # The facts ride on the trigger at density 0 only; its name always has them.
    trig = page.locator(".ratio-trigger")
    assert re.fullmatch(r"Canvas ratio: [^,]+, \d+ by \d+, [\d.]+ fps", trig.get_attribute("aria-label"))
    assert page.locator(".ratio-trigger .ratio-facts").is_visible() == (size[0] >= 1440)
    page.screenshot(path=str(SHOTS / f"rail-r3-{engine.engine_name}-{size[0]}x{size[1]}.png"))
    page.context.close()


def test_stale_words_become_a_dot_and_stay_in_the_name(engine, base_url, long_session):  # noqa: F811
    page = _open_with(engine, base_url, long_session, "", 1440, 900)
    _stub_export(page, "stale")
    _stale_vae(page)
    _export(page, "stale")
    vae = page.locator(".tb-right a.tb-dl")
    mp4 = page.get_by_role("button", name="Save exported MP4 (outdated)")
    assert vae.get_attribute("aria-label") == "Download the saved .vae project (outdated)"
    for w, h, words in ((1440, 900, True), (1100, 800, False)):
        page.set_viewport_size({"width": w, "height": h})
        page.wait_for_timeout(200)
        for link in (vae, mp4):
            assert link.locator(".tb-stale-word").is_visible() == words, (w, link)
            assert link.locator(".tb-stale-dot").is_visible() != words, (w, link)
        assert mp4.count() == 1                          # the name keeps "(outdated)" at every step
    page.context.close()


# ---------------------------------------------------------------- case 9 ----

def test_ratio_menu_groups_picks_and_safe_zones(engine, base_url, tmp_path_factory):  # noqa: F811
    sid = _fresh_session(base_url, tmp_path_factory, "ratio menu")
    page = _open(engine, base_url, sid, 1280, 800)
    trig = page.locator("button.ratio-trigger")
    trig.focus()
    page.keyboard.press("Enter")
    menu = page.get_by_role("menu", name="Canvas ratio")
    menu.wait_for(timeout=3000)
    for g in ("Aspect ratio", "Platform presets", "Safe-zone overlay"):
        assert menu.get_by_role("group", name=g, exact=True).count() == 1, g
    assert page.get_by_role("menuitemradio", name="TikTok safe zone", exact=True).count() == 1
    assert page.get_by_role("menuitemradio", name=re.compile(r"^TikTok 1080")).count() == 1  # the preset
    assert page.get_by_role("menuitemradio", name="Safe zones Off", exact=True).get_attribute("aria-checked") == "true"
    # Focus is in the menu; ↓ moves; Esc closes to the trigger.
    first = page.evaluate("document.activeElement.textContent")
    page.keyboard.press("ArrowDown")
    assert page.evaluate("document.activeElement.textContent") != first
    page.keyboard.press("Escape")
    page.wait_for_timeout(150)
    assert menu.count() == 0
    assert page.evaluate("document.activeElement === document.querySelector('button.ratio-trigger')")
    # An aspect pick closes the menu, returns focus and changes the canvas.
    trig.click()
    page.get_by_role("menuitemradio", name="9:16", exact=True).click()
    page.wait_for_timeout(150)
    assert menu.count() == 0
    assert page.evaluate("document.activeElement === document.querySelector('button.ratio-trigger')")
    page.wait_for_function(
        "() => document.querySelector('button.ratio-trigger').getAttribute('aria-label').startsWith('Canvas ratio: 9:16, 1080 by 1920')",
        timeout=10_000)
    # A safe-zone pick closes the menu too and draws the overlay.
    trig.click()
    page.get_by_role("menuitemradio", name="Reels safe zone", exact=True).click()
    page.wait_for_timeout(200)
    assert menu.count() == 0
    assert page.evaluate("document.activeElement === document.querySelector('button.ratio-trigger')")
    assert page.locator(".safe-zones-platform").inner_text().startswith("Reels")
    assert page.evaluate("localStorage.getItem('vai.safeZones')") == "reels"
    trig.click()
    assert page.get_by_role("menuitemradio", name="Reels safe zone", exact=True).get_attribute("aria-checked") == "true"
    page.screenshot(path=str(SHOTS / f"rail-r3-{engine.engine_name}-ratio-menu.png"))
    page.get_by_role("menuitemradio", name="Safe zones Off", exact=True).click()
    page.wait_for_timeout(200)
    assert page.locator(".safe-zones-platform").count() == 0
    # No safe-zone <select> and no "⋯" menu anywhere any more.
    assert page.locator(".topbar select, [data-topbar-more]").count() == 0
    page.context.close()


# ------------------------------------------------------------ rail foot ----

FOOT = ["Keyboard shortcuts", "Customize keyboard shortcuts", "Settings"]


def test_rail_foot_holds_help_shortcuts_and_settings(engine, base_url, sessions):  # noqa: F811
    page = _open(engine, base_url, sessions["full"], 1024, 768)
    foot = page.get_by_role("navigation", name="Help and settings")
    names = foot.locator("button").evaluate_all("els => els.map(e => e.getAttribute('aria-label'))")
    assert names == FOOT, names
    # They left the top bar: one of each on the page, none in the header.
    for n in FOOT:
        assert page.get_by_role("button", name=n, exact=True).count() == 1, n
        assert page.locator("header.topbar").get_by_role("button", name=n, exact=True).count() == 0, n
    assert foot.get_by_role("button", name="Keyboard shortcuts", exact=True).get_attribute("aria-keyshortcuts") == "?"
    # The foot is the LAST focusable region in DOM order (§2.1).
    last = page.evaluate("""() => { const all = [...document.querySelectorAll('button, input, select, textarea, a[href]')]
        .filter(el => el.tabIndex >= 0 && !el.disabled && el.getClientRects().length && !el.closest('[hidden], [inert]'))
        return all[all.length - 1].getAttribute('aria-label') }""")
    assert last == "Settings", last
    # Under the rail, the full rail width.
    fb, rb = foot.bounding_box(), page.locator("nav.rail").bounding_box()
    assert abs(fb["x"] - rb["x"]) < 1 and abs(fb["width"] - rb["width"]) < 1 and fb["y"] >= rb["y"] + rb["height"] - 1
    # Tooltip to the right, with the chord.
    btn = foot.get_by_role("button", name="Customize keyboard shortcuts", exact=True)
    btn.hover()
    page.wait_for_timeout(500)
    tip = page.locator(".rail-tip")
    assert tip.is_visible() and tip.bounding_box()["x"] >= fb["x"] + fb["width"]
    assert tip.locator("kbd").count() == 1
    # Each opens its dialog.
    for n, dialog in (("Keyboard shortcuts", "Keyboard shortcuts"), ("Settings", "Settings")):
        foot.get_by_role("button", name=n, exact=True).click()
        page.get_by_role("dialog", name=dialog).wait_for(timeout=3000)
        page.keyboard.press("Escape")
        page.get_by_role("dialog", name=dialog).wait_for(state="detached", timeout=3000)
    btn.click()
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
    head = page.locator(".tool-panel-head")
    assert head.get_by_role("button", name="From iPhone").count() == 0
    assert not re.search(r"phone|pair|\bQR\b", page.locator("body").inner_text(), re.I)
    assert page.locator("[data-icon=phone]").count() == 0
    assert [u for u in urls if "/api/pair" in u or "PhonePanel" in u] == []
    page.context.close()


def test_from_iphone_is_the_media_header_action_when_pairing_is_on(engine, base_url, sessions):  # noqa: F811
    ctx = engine.new_context(viewport={"width": 1440, "height": 900})
    ctx.add_init_script("try { localStorage.setItem('vai.sessionId', %s); localStorage.setItem('vai.rightTab', 'inspect') } catch (e) {}"
                        % json.dumps(sessions["full"]))
    page = ctx.new_page()
    urls = []
    page.on("request", lambda r: urls.append(r.url))

    def version(route):
        real = route.fetch().json()
        route.fulfill(json={**real, "phone_pairing": True})
    page.route(re.compile(r".*/api/version$"), version)
    page.route(re.compile(r".*/api/pair/.*"), lambda r: r.fulfill(status=404, json={"detail": "stub"}))
    page.goto(base_url + "/")
    _tab(page, "Media").wait_for()
    act = page.locator(".tool-panel-head").get_by_role("button", name="From iPhone", exact=True)
    act.wait_for(timeout=5000)
    # §8.3 case 10 with the flag: Media tab → From iPhone → Hide the tool panel → dropzone.
    seq = page.evaluate("""() => { const all = [...document.querySelectorAll('button, input, select, textarea, a[href], [tabindex]')]
        .filter(el => el.tabIndex >= 0 && !el.disabled && el.getClientRects().length && !el.closest('[hidden], [inert]'))
      const i = all.indexOf(document.getElementById('rail-tab-media'))
      return all.slice(i + 1, i + 4).map(el => el.getAttribute('aria-label') || el.textContent.trim().slice(0, 12)) }""")
    assert seq[:2] == ["From iPhone", "Hide the tool panel"] and seq[2].startswith("Drop video"), seq
    # The panel's code has not been fetched yet; nothing asked /api/pair.
    assert [u for u in urls if "/api/pair" in u or "PhonePanel" in u] == []
    # Only the Media panel has it.
    _js_click(_tab(page, "Audio"))
    assert page.locator(".tool-panel-head").get_by_role("button", name="From iPhone").count() == 0
    _js_click(_tab(page, "Media"))
    page.screenshot(path=str(SHOTS / f"rail-r3-{engine.engine_name}-from-iphone.png"))
    act.click()
    page.wait_for_function("() => !!document.querySelector('.phone-scrim')", timeout=5000)
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    assert page.locator(".phone-scrim").count() == 0
    ctx.close()


def test_the_bar_no_longer_carries_the_moved_controls(engine, base_url, sessions):  # noqa: F811
    page = _open(engine, base_url, sessions["full"], 1440, 900)
    bar = page.locator("header.topbar")
    assert bar.get_attribute("aria-label") == "Project"
    assert bar.locator(".tb-left").count() == 1 and bar.locator(".tb-center").count() == 1
    assert bar.locator(".tb-right.topbar-pinned").count() == 1
    assert bar.locator(".tb-center button.ratio-trigger").count() == 1
    text = bar.inner_text()
    assert not re.search(r"\bv\d+\.\d+", text), text                 # the version lives in Help now
    assert bar.locator("select, [data-topbar-more], .topbar-canvas, .topbar-tools").count() == 0
    # No separators left: every direct child of a group is a control or a group.
    assert bar.locator(".tb-right > span, .tb-center > span").count() == 0
    # Export is the last control in the right cluster.
    last = bar.locator(".tb-right > button, .tb-right > a").last
    assert last.evaluate("e => e.matches('button.primary')")
    page.context.close()
