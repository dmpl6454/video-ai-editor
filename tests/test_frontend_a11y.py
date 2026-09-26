"""Keyboard access, accessible names and text contrast, measured in a real
browser against the real app (QA-102, QA-103).

What 0.7.2 shipped, and what each test below pins:

- The import dropzone was a clickable <div> Tab skipped, so there was no
  keyboard way to import video. The project picker's rows and the timeline
  context menu's items were click-only <div>s; Escape closed neither the
  picker nor the Export popover; focus never entered any popover.
- Icon buttons announced their glyph ('?', '⌨', '▶', '›', '×', '▾') and range
  sliders had no name at all.
- White on the pink accent was 3.21:1 on every primary button, the empty
  timeline hint 2.46-2.65:1, the version badge 3.84:1.

The server is the real backend (a subprocess with its own HOME and WORKDIR, so
nothing touches the owner's profile) serving frontend/dist — build the
frontend first. Set VAE_A11Y_BASE_URL to run against a server that is already
up instead (e.g. a Vite dev server proxying /api to a backend). Skips cleanly
without Playwright/Chromium or a built frontend.
"""
from __future__ import annotations

import glob
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "frontend" / "dist"

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")

# Every text node's colour against its effective background (ancestors'
# backgrounds composited, element opacity applied). Skips text that is
# aria-hidden (decorative glyphs), inside disabled controls (WCAG exempts
# inactive components) and anything over an image/gradient.
CONTRAST_JS = r"""
() => {
  const parse = (c) => { const m = c.match(/rgba?\(([^)]+)\)/); if (!m) return null;
    const p = m[1].split(/[ ,\/]+/).filter(Boolean).map(Number); return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1] }
  const lin = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4) }
  const L = (c) => 0.2126 * lin(c[0]) + 0.7152 * lin(c[1]) + 0.0722 * lin(c[2])
  const blend = (fg, bg) => [0, 1, 2].map(i => fg[i] * fg[3] + bg[i] * (1 - fg[3])).concat([1])
  const bgOf = (el) => {
    const stack = []
    for (let e = el; e; e = e.parentElement) {
      const cs = getComputedStyle(e)
      if (cs.backgroundImage && cs.backgroundImage !== 'none') return null
      const c = parse(cs.backgroundColor)
      if (c && c[3] > 0) { stack.push(c); if (c[3] >= 1) break }
    }
    let bg = [14, 14, 16, 1]
    for (let i = stack.length - 1; i >= 0; i--) bg = blend(stack[i], bg)
    return bg
  }
  const opac = (el) => { let o = 1; for (let e = el; e; e = e.parentElement) o *= Number(getComputedStyle(e).opacity); return o }
  const out = []
  const seen = new Set()
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT)
  while (walker.nextNode()) {
    const t = walker.currentNode
    if (!t.textContent.trim()) continue
    const el = t.parentElement
    if (!el || seen.has(el)) continue
    seen.add(el)
    const r = el.getBoundingClientRect()
    if (!r.width || !r.height) continue
    const cs = getComputedStyle(el)
    if (cs.visibility === 'hidden' || el.closest('[aria-hidden="true"]')) continue
    if (el.closest('button:disabled, [aria-disabled="true"], fieldset:disabled, select:disabled, option')) continue
    const bg = bgOf(el)
    if (!bg) continue
    const c = parse(cs.color)
    const f = blend([c[0], c[1], c[2], c[3] * opac(el)], bg)
    const a = L(f), b = L(bg)
    const ratio = (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)
    const size = parseFloat(cs.fontSize), bold = Number(cs.fontWeight) >= 700
    const need = (size >= 24 || (bold && size >= 18.66)) ? 3 : 4.5
    if (ratio < need - 0.005) out.push(`"${t.textContent.trim().slice(0, 40)}" ${ratio.toFixed(2)}:1 < ${need} (${cs.color} on rgb(${bg.slice(0, 3).map(Math.round)}))`)
  }
  return out
}
"""

# The empty-timeline hint is canvas text, so it is measured in pixels: in a box
# around where the hint is centred (mid-lane-area, mid-width), the brightest
# NEUTRAL pixel (the hint is grey; the red playhead and coloured marks are
# not) against the box's commonest colour. Device pixels, so glyph cores reach
# the fill colour. `grey` counts the neutral pixels brighter than the ground,
# i.e. that something was drawn there at all.
CANVAS_HINT_JS = r"""
() => {
  const cv = document.querySelector('.timeline-canvas-wrap canvas[aria-label="Timeline"]')
    || document.querySelector('.timeline-canvas-wrap canvas')
  const ctx = cv.getContext('2d')
  const dpr = cv.width / cv.getBoundingClientRect().width
  const W = cv.width, H = cv.height, head = 24 * dpr
  const cy = head + (H - head) / 2, cx = 80 * dpr + (W - 80 * dpr) / 2
  const x0 = Math.max(0, Math.round(cx - 180 * dpr)), x1 = Math.min(W, Math.round(cx + 180 * dpr))
  const y0 = Math.max(0, Math.round(cy - 14 * dpr)), y1 = Math.min(H, Math.round(cy + 14 * dpr))
  const d = ctx.getImageData(x0, y0, x1 - x0, y1 - y0).data
  const lin = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4) }
  const L = (r, g, b) => 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
  const counts = new Map()
  for (let i = 0; i < d.length; i += 4) {
    const k = (d[i] << 16) | (d[i + 1] << 8) | d[i + 2]
    counts.set(k, (counts.get(k) || 0) + 1)
  }
  let bgK = 0, n = -1
  for (const [k, c] of counts) if (c > n) { n = c; bgK = k }
  const bg = [bgK >> 16, (bgK >> 8) & 255, bgK & 255], lb = L(...bg)
  let best = lb, bestPx = bg, grey = 0
  for (let i = 0; i < d.length; i += 4) {
    const r = d[i], g = d[i + 1], b = d[i + 2]
    if (Math.max(r, g, b) - Math.min(r, g, b) > 24) continue      // not neutral
    const l = L(r, g, b)
    if (l > lb * 1.5) grey++
    if (l > best) { best = l; bestPx = [r, g, b] }
  }
  return { ratio: (Math.max(best, lb) + 0.05) / (Math.min(best, lb) + 0.05), text: bestPx, bg, grey }
}
"""

# The top bar's Export trigger (the one primary button in the pinned cluster).
EXPORT_TRIGGER = ".topbar-pinned button.primary"

# Controls whose accessible name has no letter or digit ('×', '▾', '' …).
UNNAMED_ROLES = {"button", "slider", "textbox", "combobox", "checkbox", "spinbutton",
                 "menuitem", "menuitemradio", "menuitemcheckbox", "link", "tab", "radio", "switch"}


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _clip(dst: Path) -> Path:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=4",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(dst)],
        check=True, capture_output=True,
    )
    return dst


@pytest.fixture(scope="module")
def base_url(tmp_path_factory):
    url = os.environ.get("VAE_A11Y_BASE_URL")
    if url:
        yield url.rstrip("/")
        return
    if not (DIST / "index.html").exists():
        pytest.skip("frontend/dist is not built (npx vite build in frontend/)")
    tmp = tmp_path_factory.mktemp("a11y")
    home = tmp / "home"
    home.mkdir()
    port = _free_port()
    env = {**os.environ, "HOME": str(home), "WORKDIR": str(tmp / "wd"),
           "ANTHROPIC_API_KEY": "", "HUGGINGFACE_TOKEN": "", "PYTHONPATH": str(ROOT / "src")}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "video_ai_editor.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    import httpx
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            if httpx.get(f"{url}/api/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        if proc.poll() is not None:
            pytest.fail("backend exited during startup")
        time.sleep(0.3)
    else:
        proc.kill()
        pytest.fail("backend did not come up")
    try:
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="module")
def sessions(base_url, tmp_path_factory):
    """One project with a clip on v1, one empty project."""
    import httpx
    clip = _clip(tmp_path_factory.mktemp("media") / "a11y_clip.mp4")
    with httpx.Client(base_url=base_url, timeout=120) as c:
        full = c.post("/api/sessions", json={"name": "a11y full"}).json()["id"]
        with clip.open("rb") as fh:
            r = c.post(f"/api/sessions/{full}/upload", files={"file": ("a11y_clip.mp4", fh, "video/mp4")},
                       data={"add_to_timeline": "true", "transcribe": "false"})
        assert r.status_code in (200, 202), r.text
        empty = c.post("/api/sessions", json={"name": "a11y empty"}).json()["id"]
    return {"full": full, "empty": empty}


@pytest.fixture(scope="module")
def browser():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed")
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:
            shells = sorted(glob.glob(os.path.expanduser(
                "~/Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell")))
            exe = os.environ.get("VAE_CHROMIUM") or (shells[-1] if shells else None)
            if not exe:
                pytest.skip("no Chromium for Playwright")
            b = p.chromium.launch(executable_path=exe)
        yield b
        b.close()


def _open(browser, base_url, sid, width=1440, height=900):
    ctx = browser.new_context(viewport={"width": width, "height": height})
    ctx.add_init_script(f"try {{ localStorage.setItem('vai.sessionId', {sid!r}) }} catch (e) {{}}")
    page = ctx.new_page()
    page.goto(base_url + "/")
    page.get_by_role("tab", name="Media").wait_for()
    page.locator(".timeline-canvas-wrap canvas").first.wait_for()
    page.wait_for_timeout(1200)
    return page


def _active(page) -> dict:
    return page.evaluate("""() => { const a = document.activeElement; return {
      tag: a.tagName, role: a.getAttribute('role'), label: a.getAttribute('aria-label'),
      text: (a.textContent || '').trim().slice(0, 60),
      inMenu: !!a.closest('[role="menu"],[role="dialog"]'),
      menu: (() => { const m = a.closest('[role="menu"],[role="dialog"]'); if (!m) return null;
        const by = m.getAttribute('aria-labelledby');
        return m.getAttribute('aria-label') ?? (by ? document.getElementById(by)?.textContent.trim() : null) })() } }""")


def _unnamed(page) -> list:
    import re
    cdp = page.context.new_cdp_session(page)
    try:
        nodes = cdp.send("Accessibility.getFullAXTree")["nodes"]
    finally:
        cdp.detach()
    bad = []
    for n in nodes:
        if n.get("ignored"):
            continue
        role = (n.get("role") or {}).get("value")
        name = (n.get("name") or {}).get("value", "") or ""
        if role in UNNAMED_ROLES and not re.search(r"[A-Za-z0-9]", name):
            bad.append((role, name))
    return bad


def _add_text_clip(page):
    """Adds a text clip at the playhead through the Text tool; it is selected."""
    page.locator("[data-text-presets] > button").first.click()
    page.get_by_role("slider", name="Opacity").first.wait_for(timeout=10_000)


# ---------------------------------------------------------------- QA-102 ----

def test_every_control_has_an_accessible_name(browser, base_url, sessions):
    page = _open(browser, base_url, sessions["full"])
    problems = []
    problems += [("default",) + b for b in _unnamed(page)]
    # Every rail panel (LEFT_RAIL_SPEC §8.1; Text and Captions join in R2).
    for tab in ("Audio", "Stickers", "Effects", "Transitions", "AI", "Media"):
        page.get_by_role("tab", name=tab, exact=True).click()
        page.wait_for_timeout(500)
        problems += [(tab,) + b for b in _unnamed(page)]
    _add_text_clip(page)
    problems += [("text selected",) + b for b in _unnamed(page)]
    for trigger in (EXPORT_TRIGGER, "button[aria-label='Text presets']"):
        page.locator(trigger).click()
        page.wait_for_timeout(300)
        problems += [(trigger + " open",) + b for b in _unnamed(page)]
        page.keyboard.press("Escape")
    page.context.close()
    assert problems == [], problems


def test_dropzone_is_reachable_by_tab_and_opens_the_picker_from_the_keyboard(browser, base_url, sessions):
    page = _open(browser, base_url, sessions["full"])
    page.get_by_role("tab", name="Media").focus()
    for _ in range(4):
        page.keyboard.press("Tab")
        # The dropzone's copy became "Drop video, audio or photos" (QA-090);
        # the test is about reaching it, so match its stable start.
        if "Drop video" in _active(page)["text"]:
            break
    assert "Drop video" in _active(page)["text"], _active(page)
    page.screenshot(path=str(Path(os.environ.get("VAE_A11Y_SHOTS", "/tmp")) / "a11y_dropzone_focus.png"))
    with page.expect_file_chooser(timeout=3000) as fc:
        page.keyboard.press("Enter")
    assert fc.value is not None
    page.context.close()


@pytest.mark.parametrize("trigger, mode", [
    ("button.topbar-session", "menu"),
    (EXPORT_TRIGGER, "dialog"),
    ("button[aria-label='Text presets']", "dialog"),
    ("button.cc-caret", "menu"),
])
def test_popovers_open_move_and_close_from_the_keyboard(browser, base_url, sessions, trigger, mode):
    page = _open(browser, base_url, sessions["full"])
    page.locator(trigger).focus()
    page.keyboard.press("Enter")
    page.wait_for_timeout(250)
    a = _active(page)
    popup = a["menu"]
    assert a["inMenu"] and popup, a                         # focus entered a NAMED popover
    assert page.get_by_role(mode, name=popup).count() == 1, (mode, popup)
    first = a["text"]
    if mode == "menu":
        page.keyboard.press("ArrowDown")
        b = _active(page)
        assert b["inMenu"] and b["text"] != first, (a, b)   # roving focus
    else:
        for _ in range(6):                                  # Tab stays inside
            page.keyboard.press("Tab")
            assert _active(page)["menu"] == popup, _active(page)
    shots = Path(os.environ.get("VAE_A11Y_SHOTS", "/tmp"))
    page.screenshot(path=str(shots / f"a11y_{popup.replace(' ', '_').lower()}_open.png"))
    page.keyboard.press("Escape")
    page.wait_for_timeout(150)
    assert page.get_by_role(mode, name=popup).count() == 0                 # closed
    assert page.evaluate("(sel) => document.activeElement === document.querySelector(sel)", trigger)
    page.context.close()


def test_escape_closes_a_mouse_opened_popover(browser, base_url, sessions):
    page = _open(browser, base_url, sessions["full"])
    for trigger, role in ((EXPORT_TRIGGER, "dialog"), ("button.topbar-session", "menu")):
        page.locator(trigger).click()
        page.get_by_role(role).first.wait_for()
        # Escape with focus inside the popover, then again from the trigger.
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)
        assert page.get_by_role(role).count() == 0, trigger
        page.locator(trigger).click()
        page.get_by_role(role).first.wait_for()
        page.locator(trigger).focus()
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)
        assert page.get_by_role(role).count() == 0, trigger
    page.context.close()


def test_timeline_context_menu_is_a_keyboard_menu(browser, base_url, sessions):
    page = _open(browser, base_url, sessions["full"])
    _add_text_clip(page)
    page.get_by_role("application", name="Timeline").focus()
    page.keyboard.press("Shift+F10")
    menu = page.get_by_role("menu", name="Clip actions")
    menu.wait_for(timeout=3000)
    a = _active(page)
    assert a["role"] == "menuitem" and a["menu"] == "Clip actions", a
    page.keyboard.press("ArrowDown")
    assert _active(page)["text"] != a["text"]
    page.screenshot(path=str(Path(os.environ.get("VAE_A11Y_SHOTS", "/tmp")) / "a11y_clip_menu.png"))
    page.keyboard.press("Escape")
    page.wait_for_timeout(150)
    assert menu.count() == 0
    assert _active(page)["label"] == "Timeline"
    page.context.close()


def test_more_menu_at_laptop_width(browser, base_url, sessions):
    page = _open(browser, base_url, sessions["full"], width=1280, height=800)
    trig = page.locator("[data-topbar-more] > button")
    trig.focus()
    page.keyboard.press("Enter")
    page.wait_for_timeout(200)
    assert _active(page)["menu"] == "More options"
    page.keyboard.press("Escape")
    page.wait_for_timeout(150)
    assert page.evaluate("() => document.activeElement === document.querySelector('[data-topbar-more] > button')")
    page.context.close()


# ---------------------------------------------------------------- QA-103 ----

def test_rendered_text_meets_aa_contrast(browser, base_url, sessions):
    page = _open(browser, base_url, sessions["full"])
    fails = [("default", f) for f in page.evaluate(CONTRAST_JS)]
    # Every rail panel (§5.2; Text and Captions join in R2).
    for tab, settle in (("Audio", 600), ("Stickers", 800), ("Effects", 1000), ("Transitions", 600), ("AI", 1500)):
        page.get_by_role("tab", name=tab, exact=True).click()
        page.wait_for_timeout(settle)
        fails += [(tab, f) for f in page.evaluate(CONTRAST_JS)]
    page.get_by_role("tab", name="Media", exact=True).click()
    page.wait_for_timeout(600)  # the tool-panel-in fade (--dur-normal) must finish
    _add_text_clip(page)
    fails += [("text selected", f) for f in page.evaluate(CONTRAST_JS)]
    page.locator(EXPORT_TRIGGER).click()
    page.wait_for_timeout(200)
    fails += [("export open", f) for f in page.evaluate(CONTRAST_JS)]
    page.context.close()
    assert fails == [], fails


def test_primary_button_is_readable(browser, base_url, sessions):
    page = _open(browser, base_url, sessions["full"])
    ratio = page.evaluate("""() => {
      const b = document.querySelector('.topbar-pinned button.primary')
      const p = (c) => c.match(/\\d+(\\.\\d+)?/g).map(Number)
      const lin = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4) }
      const L = (c) => 0.2126 * lin(c[0]) + 0.7152 * lin(c[1]) + 0.0722 * lin(c[2])
      const cs = getComputedStyle(b), f = L(p(cs.color)), g = L(p(cs.backgroundColor))
      return (Math.max(f, g) + 0.05) / (Math.min(f, g) + 0.05)
    }""")
    page.context.close()
    assert ratio >= 4.5, ratio


def test_empty_timeline_hint_meets_aa_contrast(browser, base_url, sessions):
    page = _open(browser, base_url, sessions["empty"])
    res = page.evaluate(CANVAS_HINT_JS)
    page.context.close()
    assert res["grey"] > 40, res          # the hint is actually drawn in the box
    assert res["ratio"] >= 4.5, res
