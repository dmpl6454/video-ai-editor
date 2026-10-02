"""Wave C review fixes, measured in a real browser against the real app
(ported to the 2026-10-02 desktop shell: the asset browser's tabs, the top
bar's File menu and dialog openers, the clip inspector's bare Properties).

Each test pins one reviewed defect of the wave C UI:

- ShortcutsSettings was a fourth hand-rolled modal (no role=dialog, no Tab
  trap, editor not inert, text "Close", a red preset fill).
- The missing-ffmpeg banner was a fixed overlay with no close, covering the
  sidebars' tabs and the prompt bar's Run.
- The inspector's controls were 16-30 px tall at 10-13 px, "Letter case" had
  no label association, alignment drew hand-made SVGs, and text Scale /
  Rotation were number boxes where stickers had sliders.
- The brain popover and Settings showed one list two ways.
- Bare <summary>s drew the browser's ▶ marker.
- Lane Mute / Solo were 12 px canvas boxes with "M"/"S", mouse only.
- The project menu's Rename had no icon and "Open .vae…" showed an extension.
- The Effects panel named its clip by the editing copy's disk file.
- AI forms listed raw enum values and lane ids.
- .kbd keycaps rendered in two fonts.
- A clip name sat in near-black straight on the dark waveform.

Harness (server, sessions, Chromium) is test_frontend_a11y's: set
VAE_A11Y_BASE_URL to run against a server that is already up (a Vite dev
server proxying /api), otherwise it serves frontend/dist.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from test_frontend_a11y import base_url, browser, sessions  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")
SHOTS = Path(os.environ.get("VAE_A11Y_SHOTS", "/tmp"))


def _open(browser, base_url, sid, width=1440, height=900, tab="inspect"):  # noqa: F811
    ctx = browser.new_context(viewport={"width": width, "height": height})
    ctx.add_init_script(f"try {{ localStorage.setItem('vai.sessionId', {sid!r}); "
                        f"localStorage.setItem('vai.rightTab', {tab!r}); localStorage.setItem('aive.assetTab', 'Media') }} catch (e) {{}}")
    page = ctx.new_page()
    page.goto(base_url + "/?vae-test")  # lib/testHook.ts: the store, in the built bundle too
    _tab(page, "Media").wait_for()
    page.locator(".timeline-canvas-wrap canvas").first.wait_for()
    page.wait_for_timeout(1200)
    return page


@pytest.fixture(scope="module")
def overlays(base_url, sessions, tmp_path_factory):  # noqa: F811
    """A project with a clip on v1, a text overlay and a sticker; returns ids.
    The sticker is a PNG (emoji artwork is fetched, and this harness may be
    offline)."""
    import httpx
    from PIL import Image
    png = tmp_path_factory.mktemp("cfix-sticker") / "dot.png"
    Image.new("RGBA", (96, 96), (255, 64, 64, 255)).save(png)
    sid = sessions["full"]
    with httpx.Client(base_url=base_url, timeout=60) as c:
        r = c.post(f"/api/sessions/{sid}/dispatch", json={"tool": "add_text", "args": {
            "text": "HELLO THERE", "start": 0.5, "end": 3.0}})
        assert r.status_code == 200, r.text
        r = c.post(f"/api/sessions/{sid}/dispatch", json={"tool": "add_sticker", "args": {
            "src": str(png), "start": 0.5, "end": 3.0}})
        assert r.status_code == 200, r.text
        edl = c.get(f"/api/sessions/{sid}/edl").json()
    text_id = next(cl["id"] for t in edl["tracks"] for cl in t["clips"] if "text" in cl and "style" in cl)
    sticker_id = next(cl["id"] for t in edl["tracks"] if t["type"] == "sticker" for cl in t["clips"])
    return {"sid": sid, "text": text_id, "sticker": sticker_id}


def _tab(page, name):
    """An asset-browser tab, scoped to the strip; "AI" is Media › AI media."""
    if name == "AI":
        page.locator(".ab-tabs").get_by_role("tab", name="Media", exact=True).click()
        page.get_by_role("button", name="AI media", exact=True).click()
        return page.locator(".ab-content[data-sub='AI media']")
    return page.locator(".ab-tabs").get_by_role("tab", name=name, exact=True)


def _select(page, clip_id):
    """Select a clip through the app's own store (lib/testHook.ts); the
    inspector shows it at once."""
    page.evaluate("async (id) => { const s = (await (window.__vaeTest ?? import('/src/store.ts'))).useStore.getState();"
                  " s.setSelection(id) }", clip_id)
    page.locator(f".in-clip[data-clip-id='{clip_id}']").wait_for(timeout=5000)
    page.locator(".props").first.wait_for(timeout=5000)
    page.wait_for_timeout(300)


# ------------------------------------------------------------- shortcuts dialog

def test_shortcut_settings_is_the_app_dialog(browser, base_url, sessions):  # noqa: F811
    page = _open(browser, base_url, sessions["full"])
    opener = page.locator("button[aria-label='Customize keyboard shortcuts']")
    opener.click()
    page.wait_for_timeout(400)
    dlg = page.locator("[role=dialog][aria-modal=true]")
    assert dlg.count() == 1
    assert dlg.get_attribute("aria-labelledby")
    assert page.locator(f"#{dlg.get_attribute('aria-labelledby')}").inner_text() == "Shortcuts"
    assert page.evaluate("document.getElementById('root').inert") is True
    assert page.evaluate("!!document.activeElement.closest('[role=dialog]')")
    # The design's footer actions, no text "Close".
    assert dlg.get_by_role("button", name="Cancel", exact=True).count() == 1
    assert dlg.get_by_role("button", name="Close", exact=True).count() == 0
    escaped = 0
    for _ in range(60):
        page.keyboard.press("Tab")
        if not page.evaluate("!!document.activeElement.closest('[role=dialog]')"):
            escaped += 1
    assert escaped == 0, f"focus left the dialog {escaped} times"
    # The profile picker is the shared dropdown (the keymap presets), not a red fill.
    profile = dlg.locator("button[aria-label='Shortcut profile']")
    assert profile.count() == 1
    bg = profile.evaluate("el => getComputedStyle(el).backgroundColor")
    tokens = page.evaluate("""() => { const cs = getComputedStyle(document.documentElement)
      const probe = document.createElement('div'); document.body.appendChild(probe)
      const col = (v) => { probe.style.backgroundColor = cs.getPropertyValue(v).trim(); return getComputedStyle(probe).backgroundColor }
      const out = { accent: col('--accent-fill') }; probe.remove(); return out }""")
    assert bg != tokens["accent"], (bg, tokens)
    page.screenshot(path=str(SHOTS / "cfix_shortcuts_dialog.png"))
    # Escape while capturing a chord cancels the capture, not the dialog.
    dlg.locator("button[data-keycap]").first.click()
    assert dlg.locator(".dlg-cap.is-capturing").count() == 1
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    assert dlg.count() == 1 and dlg.locator(".dlg-cap.is-capturing").count() == 0
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    assert page.locator("[role=dialog]").count() == 0
    assert page.evaluate("document.getElementById('root').inert") is False
    assert page.evaluate("document.activeElement.getAttribute('aria-label')") == "Customize keyboard shortcuts"
    page.context.close()


# ------------------------------------------------------------- ffmpeg banner

def _rects_overlap(a, b) -> bool:
    return a["x"] < b["x"] + b["width"] and b["x"] < a["x"] + a["width"] \
        and a["y"] < b["y"] + b["height"] and b["y"] < a["y"] + a["height"]


@pytest.mark.parametrize("width,height", [(1024, 768), (1440, 900)])
def test_missing_ffmpeg_banner_sits_in_flow_and_can_be_hidden(browser, base_url, sessions, width, height):  # noqa: F811
    ctx = browser.new_context(viewport={"width": width, "height": height})
    ctx.add_init_script(f"try {{ localStorage.setItem('vai.sessionId', {sessions['full']!r}) }} catch (e) {{}}")

    def fake_health(route):
        resp = route.fetch()
        body = resp.json()
        body["media_tools"] = {"ok": False, "missing": ["ffmpeg", "ffprobe"],
                               "install_command": "brew install ffmpeg", "message": None}
        route.fulfill(response=resp, body=json.dumps(body))

    ctx.route("**/api/health", fake_health)
    page = ctx.new_page()
    page.goto(base_url + "/")
    banner = page.locator("[data-media-tools-banner]")
    banner.wait_for()
    page.wait_for_timeout(600)
    b = banner.bounding_box()
    page.screenshot(path=str(SHOTS / f"cfix_banner_{width}.png"))
    assert page.evaluate("el => getComputedStyle(el).position", banner.element_handle()) != "fixed"
    covered = []
    for sel in (".ab-tabs [role=tab]", "#right-panel", ".ab-subnav button", "form[aria-label='Prompt editor'] button"):
        for i in range(page.locator(sel).count()):
            el = page.locator(sel).nth(i)
            if not el.is_visible():
                continue
            r = el.bounding_box()
            if r and _rects_overlap(b, r):
                covered.append((sel, el.inner_text()[:30]))
    assert not covered, covered
    # Every icon through the app's map.
    assert banner.locator("svg").count() == banner.locator("svg[data-icon]").count() == 4
    banner.get_by_role("button", name="Hide this notice").click()
    page.wait_for_timeout(200)
    assert banner.count() == 0
    page.reload()
    page.locator(".ab-tabs").get_by_role("tab", name="Media", exact=True).wait_for()
    page.wait_for_timeout(800)
    assert page.locator("[data-media-tools-banner]").count() == 0      # hidden for the session
    # Settings keeps it reachable.
    page.locator("button[aria-label='Settings']").click()
    page.wait_for_timeout(500)
    sect = page.locator("section.settings-section", has_text="Video engine")
    assert "brew install ffmpeg" in sect.inner_text()
    assert sect.get_by_role("button", name="Check again").count() == 1
    ctx.close()


# ------------------------------------------------------------- inspector

CONTROL_HEIGHTS_JS = r"""
() => [...document.querySelectorAll('.props select, .props input, .props button, .props textarea')]
  .filter(el => el.offsetParent && !['checkbox', 'radio', 'range', 'color'].includes(el.type))
  .map(el => ({ tag: el.tagName.toLowerCase(), type: el.type || '', h: Math.round(el.getBoundingClientRect().height),
                fs: parseFloat(getComputedStyle(el).fontSize), text: (el.getAttribute('aria-label') || el.textContent || '').trim().slice(0, 24) }))
"""


@pytest.mark.parametrize("width", [1024, 1440])
def test_text_inspector_controls_share_one_rhythm(browser, base_url, overlays, width):  # noqa: F811
    page = _open(browser, base_url, overlays["sid"], width=width)
    _select(page, overlays["text"])
    page.screenshot(path=str(SHOTS / f"cfix_text_inspector_{width}.png"), full_page=True)
    ctrls = page.evaluate(CONTROL_HEIGHTS_JS)
    assert ctrls
    fields = [c for c in ctrls if c["tag"] in ("select", "input")]
    buttons = [c for c in ctrls if c["tag"] == "button"]
    heights = {c["h"] for c in fields}
    assert fields and len(heights) == 1 and heights <= {28, 30} and all(c["fs"] >= 12 for c in fields), fields
    assert all(c["h"] >= 24 and c["fs"] >= 11 for c in buttons), [c for c in buttons if c["h"] < 24 or c["fs"] < 11]
    # "Letter case" names its select (it fell back to the long title).
    case = page.get_by_label("Letter case", exact=True)
    assert case.count() == 1 and case.evaluate("el => el.tagName") == "SELECT"
    # Alignment through the app's icon map, not hand-made SVG rects.
    radios = page.locator("[role=radiogroup][aria-label=Alignment] [role=radio]")
    assert radios.count() == 3
    icons = [radios.nth(i).locator("svg").get_attribute("data-icon") for i in range(3)]
    assert icons == ["alignLeft", "alignCenter", "alignRight"]
    assert page.locator(".props svg rect").count() == 0 or \
        page.locator(".props svg:not([data-icon]) rect").count() == 0
    # Scale / Rotation are sliders, as on a sticker.
    for name in ("Scale", "Rotation"):
        assert page.locator(f".props input[type=range][aria-label='{name}']").count() == 1, name
    page.context.close()


def test_sticker_and_text_use_one_control_type_for_scale_and_rotation(browser, base_url, overlays):  # noqa: F811
    page = _open(browser, base_url, overlays["sid"])
    kinds = {}
    for key in ("text", "sticker"):
        _select(page, overlays[key])
        kinds[key] = [page.locator(f".props [aria-label='{n}']").first.get_attribute("type")
                      for n in ("Scale", "Rotation")]
    assert kinds["text"] == kinds["sticker"] == ["range", "range"], kinds
    page.context.close()


# ------------------------------------------------------------- brains

def test_brain_popover_and_settings_speak_one_language(browser, base_url, sessions):  # noqa: F811
    page = _open(browser, base_url, sessions["full"])
    page.locator("button.brain-pill").first.click()
    page.wait_for_timeout(800)
    pop = page.locator(".brain-pop")
    assert pop.locator("h3").inner_text() == "Who answers your prompts"
    assert pop.get_by_role("button", name="Check again").count() == 1
    pop_rows = pop.locator(".brain-list-row").count()
    assert pop_rows >= 3 and pop.locator(".brain-list-dot").count() == pop_rows
    assert pop.locator(".brain-row-fix, pre, code").count() == 0
    page.screenshot(path=str(SHOTS / "cfix_brain_popover.png"))
    page.keyboard.press("Escape")
    page.locator("button[aria-label='Settings']").click()
    page.wait_for_timeout(800)
    sect = page.locator("section.settings-section", has_text="Who answers your prompts")
    assert sect.count() == 1
    assert sect.locator(".brain-list-row").count() == pop_rows
    assert sect.get_by_role("button", name="Check again").count() == 1
    page.context.close()


# ------------------------------------------------------------- disclosures

def test_every_summary_carries_the_app_chevron(browser, base_url, sessions):  # noqa: F811
    page = _open(browser, base_url, sessions["full"])
    _tab(page, "AI")
    page.wait_for_timeout(600)
    page.locator("button[aria-label='Help']").click()
    page.wait_for_timeout(500)
    summaries = page.evaluate("""() => [...document.querySelectorAll('summary')].map(s => ({
      text: s.textContent.trim().slice(0, 40), icon: !!s.querySelector(':scope > svg[data-icon]'),
      marker: getComputedStyle(s, '::-webkit-details-marker').display,
      listStyle: getComputedStyle(s).listStyleType }))""")
    assert summaries, "no <summary> rendered on these surfaces"
    bad = [s for s in summaries if not s["icon"] or s["listStyle"] != "none"]
    assert not bad, bad
    page.context.close()


# ------------------------------------------------------------- lane monitors

def test_lane_mute_and_solo_are_keyboard_buttons(browser, base_url, sessions):  # noqa: F811
    page = _open(browser, base_url, sessions["full"])
    mute = page.get_by_role("button", name="Mute Main video", exact=True)
    solo = page.get_by_role("button", name="Solo Main video", exact=True)
    assert mute.count() == 1 and solo.count() == 1
    for b in (mute, solo):
        r = b.bounding_box()
        assert r["width"] >= 20 and r["height"] >= 18, r
        assert b.get_attribute("aria-pressed") == "false"
        assert b.locator("svg[data-icon]").count() == 1
    mute.focus()
    page.keyboard.press("Enter")
    page.wait_for_function("() => document.querySelector(\"button[aria-label='Mute Main video']\")"
                           ".getAttribute('aria-pressed') === 'true'", timeout=5000)
    assert mute.locator("svg").get_attribute("data-icon") == "laneMuted"
    page.screenshot(path=str(SHOTS / "cfix_lane_monitors.png"))
    page.keyboard.press("Enter")                     # unmute again: leave the shared project as it was
    page.wait_for_function("() => document.querySelector(\"button[aria-label='Mute Main video']\")"
                           ".getAttribute('aria-pressed') === 'false'", timeout=5000)
    page.context.close()


# ------------------------------------------------------------- project menu

def test_project_menu_items_line_up_and_speak_plainly(browser, base_url, sessions):  # noqa: F811
    """The File menu (the project menu's successor in the top bar)."""
    page = _open(browser, base_url, sessions["full"])
    page.locator("button[aria-label='File']").click()
    page.wait_for_timeout(400)
    menu = page.locator("[role=menu][aria-label=File]")
    items = [menu.get_by_role("menuitem", name=n) for n in ("Save project file (.vae)", "Import media…", "Open project file…", "Rename project…")]
    xs = []
    for it in items:
        assert it.count() == 1
        assert it.locator("svg[data-icon]").count() == 1
        xs.append(it.evaluate("el => { const r = document.createRange(); const t = [...el.childNodes].find(n => n.nodeType === 3 && n.textContent.trim()); r.selectNodeContents(t); return Math.round(r.getBoundingClientRect().left) }"))
    assert max(xs) - min(xs) <= 1, xs
    # The format is named once, on the save row the design keeps it on; no bare extension elsewhere.
    assert menu.inner_text().count(".vae") == 1, menu.inner_text()
    page.context.close()


# ------------------------------------------------------------- effects / AI forms

def test_effects_panel_names_the_clip_like_the_media_panel(browser, base_url, sessions):  # noqa: F811
    page = _open(browser, base_url, sessions["full"])
    _tab(page, "Effects").click()
    page.wait_for_timeout(500)
    tgt = page.locator(".fx-target")
    text = tgt.inner_text().strip()
    assert text.startswith("Applies to"), text
    assert "normalized" not in text and "a11y_clip" in text
    assert tgt.locator("svg").get_attribute("data-icon") == "film"
    page.context.close()


def test_ai_form_choices_are_words(browser, base_url, sessions):  # noqa: F811
    import re
    page = _open(browser, base_url, sessions["full"])
    _tab(page, "AI")
    page.wait_for_timeout(600)
    raw = re.compile(r"^[a-z0-9_]+$")
    seen = {}
    for card in ("Remove silences", "Cut range", "Auto captions", "Translate captions"):
        head = page.locator("button.ai-card-head", has_text=card).first
        if head.count() == 0:
            continue
        if head.get_attribute("aria-expanded") != "true":
            head.click()
            page.wait_for_timeout(300)
        body = page.locator(f"#{head.get_attribute('aria-controls')}")
        opts = body.locator("select option").all_inner_texts()
        seen[card] = opts
    assert seen, "no AI cards found"
    flat = [o for opts in seen.values() for o in opts]
    assert "Main video" in flat and "v1" not in flat, seen
    assert not [o for o in flat if raw.match(o.strip())], seen
    assert "Keep pad" not in page.locator(".ai-form").first.inner_text()
    page.context.close()


# ------------------------------------------------------------- keycaps / label plate

def test_keycaps_use_one_face(browser, base_url, sessions):  # noqa: F811
    fams = set()
    for key in ("full", "empty"):
        page = _open(browser, base_url, sessions[key])
        fams |= set(page.evaluate("() => [...document.querySelectorAll('.kbd')].map(e => getComputedStyle(e).fontFamily)"))
        page.context.close()
    assert fams and len(fams) == 1 and "monospace" not in next(iter(fams)), fams


@pytest.fixture(scope="module")
def main_only(base_url, tmp_path_factory):  # noqa: F811
    """A project whose ONLY clip is one on Main video, so v1 is the first row."""
    import httpx
    from test_frontend_a11y import _clip
    clip = _clip(tmp_path_factory.mktemp("plate") / "plate_clip.mp4")
    with httpx.Client(base_url=base_url, timeout=120) as c:
        sid = c.post("/api/sessions", json={"name": "plate"}).json()["id"]
        with clip.open("rb") as fh:
            r = c.post(f"/api/sessions/{sid}/upload", files={"file": ("plate_clip.mp4", fh, "video/mp4")},
                       data={"add_to_timeline": "true", "transcribe": "false"})
        assert r.status_code in (200, 202), r.text
        tracks = [t["id"] for t in c.get(f"/api/sessions/{sid}/edl").json()["tracks"] if t["clips"]]
    assert tracks == ["v1"], tracks
    return sid


def test_a_clip_name_has_a_dark_plate_under_it(browser, base_url, main_only):  # noqa: F811
    """The name was drawn in #0e0e10 straight over the clip colour / dark
    waveform. Sample the main canvas just left of the first glyph, inside the
    plate: it must be dark, and the text colour must be the light --text."""
    page = _open(browser, base_url, main_only)
    page.wait_for_timeout(800)
    out = page.evaluate("""() => {
      const cv = document.querySelector('.timeline-canvas-wrap canvas')
      const dpr = cv.width / cv.getBoundingClientRect().width
      const ctx = cv.getContext('2d')
      const lum = (x, y) => { const d = ctx.getImageData(Math.round(x * dpr), Math.round(y * dpr), 1, 1).data
        const f = (c) => { c /= 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4 }
        return 0.2126 * f(d[0]) + 0.7152 * f(d[1]) + 0.0722 * f(d[2]) }
      // v1 row: header 24, row 36; its first clip starts at x = 80 (label column);
      // the name starts 6 px in, the plate 4 px before that.
      const rowY = 24, midY = rowY + 18
      const plate = [lum(83, midY - 6), lum(83, midY), lum(83, midY + 6)]
      let brightest = 0
      for (let x = 86; x < 140; x++) for (let y = midY - 5; y <= midY + 4; y++) brightest = Math.max(brightest, lum(x, y))
      return { plate, brightest }
    }""")
    assert max(out["plate"]) < 0.12, out
    assert out["brightest"] > 0.5, out
    page.locator(".timeline-canvas-wrap").screenshot(path=str(SHOTS / "cfix_clip_label.png"))
    page.context.close()
