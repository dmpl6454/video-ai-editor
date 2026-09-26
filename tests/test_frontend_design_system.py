"""The visual system, measured in a real browser against the real app (wave C:
QA-104, QA-125, QA-127, the disabled state, control rhythm, font licences).

What 0.7.2 / wave B shipped, and what each test below pins:

- QA-104: section headers restated their style inline per panel; two copies
  computed 0.8em / 0.6em tracking (`0.08 * 10 + 'em'`), so "RECENT" and the
  sticker search count read "R E C E N T". One `.section-label` now.
- QA-125: colour emoji (💾 📂 🎵 ✨ 🔪 📱) sat next to typographic stand-ins
  (▾ ▶ × ✕ ⋯ ‹ ›) and a hand-drawn SVG set. Every icon is lucide-react now:
  16 px, `currentColor`, class `lucide`.
- QA-127: the export progress sweep (1.15 s, infinite) and the toast entrance
  animated under prefers-reduced-motion.
- Disabled controls were `opacity: 0.4` (a faded pink primary, 0.5 / 0.55
  per-panel copies); they are one neutral colour at full opacity now.
- Top-bar controls ran 24-34 px tall at 11-13 px; they are one height now.
- The bundled OFL fonts shipped with no licence; Help lists it now.

Harness (server, sessions, Chromium) is test_frontend_a11y's: set
VAE_A11Y_BASE_URL to run against a server that is already up (e.g. a Vite dev
server proxying /api), otherwise it serves frontend/dist.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import pytest

from test_frontend_a11y import base_url, browser, sessions  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")
SHOTS = Path(os.environ.get("VAE_A11Y_SHOTS", "/tmp"))


def _open(browser, base_url, sid, width=1440, height=900, reduced_motion=None):  # noqa: F811
    opts = {"viewport": {"width": width, "height": height}}
    if reduced_motion:
        opts["reduced_motion"] = reduced_motion
    ctx = browser.new_context(**opts)
    ctx.add_init_script(f"try {{ localStorage.setItem('vai.sessionId', {sid!r}); localStorage.setItem('vai.rightTab', 'inspect') }} catch (e) {{}}")
    page = ctx.new_page()
    page.goto(base_url + "/")
    page.get_by_role("tab", name="Media").wait_for()
    page.locator(".timeline-canvas-wrap canvas").first.wait_for()
    page.wait_for_timeout(1200)
    return page


def _select_first_clip(page):
    cv = page.locator(".timeline-canvas-wrap canvas").first
    bb = cv.bounding_box()
    # The main-video lane is the first lane under the ruler on this project.
    page.mouse.click(bb["x"] + 140, bb["y"] + 40)
    page.get_by_role("tab", name="Inspector").click()
    page.wait_for_timeout(600)


def _select_new_text_clip(page) -> bool:
    """Add a text clip and inspect it (REVIEW-C4-ALIGN-GLYPH-SVG: the text
    inspector's alignment icons were hand-made SVG, and no surface here ever
    opened it). Selection goes through the app's own store module, which
    only a Vite dev server serves; against a built bundle this step is
    skipped."""
    ok = page.evaluate("""async () => {
      try {
        const m = await import('/src/store.ts')
        const st = m.useStore.getState()
        if (!st.sessionId) return false
        await st.dispatch('add_text', { text: 'DESIGN CHECK', start: 0.2, end: 1.5 })
        const edl = m.useStore.getState().edl
        const clip = edl.tracks.flatMap((t) => t.clips).find((c) => c.text === 'DESIGN CHECK')
        if (!clip) return false
        m.useStore.getState().setSelection(clip.id)
        return true
      } catch (e) { return false }
    }""")
    if not ok:
        return False
    page.get_by_role("tab", name="Inspector").click()
    page.wait_for_timeout(600)
    return page.locator("[role=radiogroup][aria-label=Alignment]").count() == 1


def _open_media_panels(page):
    for title in ("Filters, effects & LUT looks", "Emoji & sticker picker"):   # the picker closes on an outside click
        btn = page.locator(f".sidebar.left button[title='{title}']").first
        if btn.get_attribute("aria-expanded") != "true":
            btn.click()
        page.wait_for_timeout(300)


def _surfaces(page):
    """Walk the app through its major surfaces, yielding a name at each."""
    yield "default"
    _open_media_panels(page)
    page.locator(".sticker-picker input").first.fill("cat")
    page.wait_for_timeout(500)
    yield "media panels + sticker search"
    _select_first_clip(page)
    yield "clip inspector"
    if _select_new_text_clip(page):
        yield "text inspector"
    for tab in ("Transitions", "AI"):
        page.get_by_role("tab", name=tab).click()
        page.wait_for_timeout(400)
        yield f"{tab} tab"
    page.get_by_role("tab", name="Media").click()
    page.get_by_role("tab", name="Chat").click()
    page.wait_for_timeout(300)
    yield "chat"
    page.get_by_role("tab", name="Inspector").click()
    for trigger, name in ((".topbar-pinned button.primary", "export dialog"),
                          ("button[aria-label='Keyboard shortcuts']", "help"),
                          ("button.topbar-session", "project menu"),
                          ("button[aria-label='Text presets']", "text presets"),
                          ("button.cc-caret", "captions menu"),
                          ("button.ratio-trigger", "ratio menu")):
        page.locator(trigger).click()
        page.wait_for_timeout(400)
        yield name
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
    page.locator("button[aria-label='Customize keyboard shortcuts']").click()
    page.wait_for_timeout(400)
    yield "shortcut settings"
    page.keyboard.press("Escape")


# ---------------------------------------------------------------- QA-104 ----

HEADER_JS = r"""
() => [...document.querySelectorAll('body *')].filter(el => {
  const cs = getComputedStyle(el), r = el.getBoundingClientRect()
  return r.width && r.height && cs.textTransform === 'uppercase' && el.textContent.trim()
    && [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim())
}).map(el => { const cs = getComputedStyle(el), fs = parseFloat(cs.fontSize)
  const lh = cs.lineHeight === 'normal' ? fs * 1.25 : parseFloat(cs.lineHeight)
  return { text: el.textContent.trim().slice(0, 40), em: parseFloat(cs.letterSpacing) / fs,
           lines: Math.round(el.getBoundingClientRect().height / lh), cls: el.className } })
"""


def test_section_headers_share_one_tracking_and_never_wrap(browser, base_url, sessions):  # noqa: F811
    page = _open(browser, base_url, sessions["full"])
    bad = []
    seen = 0
    for where in _surfaces(page):
        for h in page.evaluate(HEADER_JS):
            seen += 1
            if h["em"] > 0.1 + 1e-3:
                bad.append((where, h["text"], f"{h['em']:.2f}em"))
    labels = page.evaluate("""() => [...document.querySelectorAll('.section-label, .sidebar h2')]
      .filter(e => e.getBoundingClientRect().height).map(e => { const cs = getComputedStyle(e)
        return [e.textContent.trim(), cs.fontSize, cs.letterSpacing, cs.textTransform, cs.color] })""")
    page.context.close()
    assert seen > 10, "no uppercase headers found — the walk did not reach the panels"
    assert bad == [], bad
    # ONE style: every section label computes the same size, tracking and colour.
    assert len({tuple(label[1:]) for label in labels}) == 1, labels


# ---------------------------------------------------------------- QA-125 ----

# Text that is an icon stand-in. Emoji anywhere; the typographic marks when they
# stand alone as a node (× in "1920×1080" or "4×" is multiplication, not an icon).
GLYPHS_JS = r"""
() => {
  const emoji = /(?![©®™])\p{Extended_Pictographic}/u
  const marks = /^[▾▼▶◀◆◇×✕✓✗⋯‹›⚠↓↔↕⚡★↺→]$|^[▾▼▶◀◆◇✕✓✗⋯‹›⚠↓↔↕⚡★↺] | [▾▼▶◀◆◇✕✓✗⋯‹›⚠↓↔↕⚡★↺]$/u
  const out = []
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT)
  while (w.nextNode()) {
    const t = w.currentNode, s = t.textContent.trim(), el = t.parentElement
    if (!s || !el || el.closest('kbd, .kbd, [data-keycap], textarea, [data-emoji-content], script, style, pre')) continue
    const r = el.getBoundingClientRect()
    if (!r.width || !r.height) continue
    // React splits `{w}×{h}` into three text nodes; a × between numbers is maths.
    if (s === '×' && /\d\s*×\s*\d/.test(el.textContent)) continue
    if (emoji.test(s) || marks.test(s)) out.push(`${el.tagName}.${el.className}: ${JSON.stringify(s.slice(0, 40))}`)
  }
  return out
}
"""

ICONS_JS = r"""
() => [...document.querySelectorAll('button svg, [role=menuitem] svg, [role=menuitemradio] svg, a svg, [role=tab] svg, .section-label svg')]
  .filter(s => s.getBoundingClientRect().width)
  .map(s => ({ cls: s.getAttribute('class') || '', w: s.getBoundingClientRect().width, h: s.getBoundingClientRect().height,
               stroke: s.getAttribute('stroke'), owner: (s.closest('button,[role],a') || {}).className || '' }))
  .filter(s => !s.cls.includes('lucide') || Math.abs(s.w - 16) > 0.5 || Math.abs(s.h - 16) > 0.5 || s.stroke !== 'currentColor')
"""


def test_no_emoji_or_text_glyph_used_as_an_icon(browser, base_url, sessions):  # noqa: F811
    problems = []
    for width, height in ((1440, 900), (1024, 768)):
        page = _open(browser, base_url, sessions["full"], width, height)
        for where in _surfaces(page):
            problems += [(width, where, p) for p in page.evaluate(GLYPHS_JS)]
        if width < 1440:
            page.locator("button[aria-label^='More']").click()
            page.wait_for_timeout(300)
            problems += [(width, "more menu", p) for p in page.evaluate(GLYPHS_JS)]
        page.context.close()
    page = _open(browser, base_url, sessions["empty"])
    problems += [("empty", p) for p in page.evaluate(GLYPHS_JS)]
    page.context.close()
    assert problems == [], problems


def test_every_control_icon_is_lucide_at_16px_in_current_colour(browser, base_url, sessions):  # noqa: F811
    page = _open(browser, base_url, sessions["full"])
    bad = []
    for where in _surfaces(page):
        bad += [(where, b) for b in page.evaluate(ICONS_JS)]
    page.screenshot(path=str(SHOTS / "design_icons_last_surface.png"))
    page.context.close()
    assert bad == [], bad


# ---------------------------------------------------------------- QA-127 ----

# The export progress track and a toast exactly as ExportModal / Toast render
# them, plus every animation the page is running at that moment.
MOTION_JS = r"""
async () => {
  const host = document.createElement('div')
  host.innerHTML = `<div class="export-progress-track indeterminate"><div class="export-progress-fill"></div></div>
    <div class="toast-host"><div class="toast toast-info"><span class="toast-dot"></span><span class="toast-msg">x</span></div></div>`
  document.body.appendChild(host)
  await new Promise(r => setTimeout(r, 120))
  const fill = getComputedStyle(host.querySelector('.export-progress-fill'))
  const sweep = { animationName: fill.animationName, animationIterationCount: fill.animationIterationCount }
  const toast = { animationName: getComputedStyle(host.querySelector('.toast')).animationName }
  const running = document.getAnimations()
    .filter(a => a.playState === 'running')
    .map(a => ({ name: a.animationName || a.transitionProperty || '?', dur: a.effect.getComputedTiming().duration,
                 it: a.effect.getComputedTiming().iterations,
                 el: (a.effect.target && a.effect.target.className && String(a.effect.target.className).slice(0, 40)) || '' }))
  host.remove()
  return { sweep: sweep.animationName, sweepIter: sweep.animationIterationCount, toast: toast.animationName, running }
}
"""


@pytest.mark.parametrize("motion", ["reduce", "no-preference"])
def test_reduced_motion_is_honoured_everywhere(browser, base_url, sessions, motion):  # noqa: F811
    page = _open(browser, base_url, sessions["full"], reduced_motion=motion)
    page.get_by_role("tab", name="Transitions").click()
    page.wait_for_timeout(300)
    page.locator(".trp-tile").first.hover()
    page.wait_for_timeout(150)
    m = page.evaluate(MOTION_JS)
    assert page.evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches") is (motion == "reduce")
    page.context.close()
    if motion == "no-preference":
        # The control: the same probe sees the motion when motion is allowed.
        assert m["sweep"] == "export-sweep" and m["sweepIter"] == "infinite", m
        assert m["toast"] == "toast-in", m
        return
    assert m["sweep"] == "none", m
    assert m["toast"] == "none", m
    # Nothing on the page is still moving: every running animation is either a
    # paused transition preview (the tiles' still frame) or already over.
    moving = [a for a in m["running"] if (a["dur"] or 0) > 1 or a["it"] == float("inf")]
    assert moving == [], moving


# ------------------------------------------------------- disabled state ----

DISABLED_JS = r"""
() => {
  const want = getComputedStyle(document.documentElement).getPropertyValue('--text-disabled').trim()
  const probe = document.createElement('span'); probe.style.color = want; document.body.appendChild(probe)
  const rgb = getComputedStyle(probe).color; probe.remove()
  const accents = ['--accent', '--accent-fill', '--accent-2', '--accent-2-fill'].map(v => {
    const p = document.createElement('span'); p.style.color = getComputedStyle(document.documentElement).getPropertyValue(v).trim()
    document.body.appendChild(p); const c = getComputedStyle(p).color; p.remove(); return c })
  const eff = (el) => { let o = 1; for (let e = el; e; e = e.parentElement) o *= Number(getComputedStyle(e).opacity); return o }
  return [...document.querySelectorAll('button:disabled, input:disabled, select:disabled, textarea:disabled')]
    .filter(el => el.getBoundingClientRect().width && el.type !== 'range' && el.type !== 'checkbox' && el.type !== 'radio')
    .map(el => { const cs = getComputedStyle(el)
      return { what: (el.getAttribute('aria-label') || el.textContent || el.tagName).trim().slice(0, 30),
               opacity: eff(el), color: cs.color, want: rgb, accentBg: accents.includes(cs.backgroundColor) } })
}
"""


def test_every_disabled_control_is_one_neutral_state(browser, base_url, sessions):  # noqa: F811
    found = []
    for sid in (sessions["empty"], sessions["full"]):
        page = _open(browser, base_url, sid)
        found += page.evaluate(DISABLED_JS)
        page.context.close()
    assert len(found) >= 3, found   # Save, Export, Captions … on the empty project
    bad = [d for d in found if abs(d["opacity"] - 1) > 1e-3 or d["color"] != d["want"] or d["accentBg"]]
    assert bad == [], bad


# ---------------------------------------------------------- control rhythm ----

def test_top_bar_controls_share_one_height_and_type_size(browser, base_url, sessions):  # noqa: F811
    for width, height in ((1440, 900), (1024, 768)):
        page = _open(browser, base_url, sessions["full"], width, height)
        rows = page.evaluate("""() => [...document.querySelectorAll('.topbar button, .topbar select')]
          .filter(e => e.getBoundingClientRect().width).map(e => ({
            name: e.getAttribute('aria-label') || e.textContent.trim().slice(0, 20),
            h: Math.round(e.getBoundingClientRect().height * 10) / 10,
            fs: getComputedStyle(e).fontSize,
            pill: e.classList.contains('pill'),
            icon: e.classList.contains('icon-btn') ? Math.round(e.getBoundingClientRect().width) : null }))""")
        page.screenshot(path=str(SHOTS / f"design_topbar_{width}.png"), clip={"x": 0, "y": 0, "width": width, "height": 44})
        page.context.close()
        assert len(rows) >= 8, rows
        assert {r["h"] for r in rows} == {28.0}, rows
        # One type size for the controls; the project-name pill keeps the pills' 11 px.
        assert {r["fs"] for r in rows if not r["pill"]} == {"12px"}, rows
        assert {r["icon"] for r in rows if r["icon"] is not None} <= {28}, rows


# ----------------------------------------------------------- font licences ----

def test_help_lists_every_bundled_font_and_the_ofl_text(browser, base_url, sessions):  # noqa: F811
    page = _open(browser, base_url, sessions["full"])
    page.locator("button[aria-label='Keyboard shortcuts']").click()
    section = page.locator(".help-licences")
    section.get_by_text("Anton").first.wait_for(timeout=10_000)
    families = page.locator("[data-font-licence]").evaluate_all("els => els.map(e => e.dataset.fontLicence)")
    section.locator("summary").click()
    text = section.locator("pre").inner_text()
    page.locator(".help-dialog .dialog-body").evaluate("e => { e.scrollTop = e.scrollHeight }")
    page.screenshot(path=str(SHOTS / "design_help_licences.png"))
    page.context.close()
    for fam in ("Anton", "Bebas Neue", "Inter", "Montserrat", "Noto Sans Arabic", "Noto Sans Devanagari"):
        assert fam in families, families
    assert "SIL OPEN FONT LICENSE Version 1.1" in text and "DISCLAIMER" in text
    assert re.search(r"OTHER DEALINGS IN THE FONT SOFTWARE\.\s*$", text)
