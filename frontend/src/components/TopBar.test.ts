// The TopBar rendered to static markup (no DOM library in the tree — vitest
// runs in node, so `react-dom/server` is the render path; effects do not run,
// which is exactly the state under test here: the boot render, before
// GET /api/version has answered).
//
// The defect this guards: the iPhone-pairing feature is TEMPORARILY gated off
// for the standalone desktop ship (backend flag `VAE_PHONE_PAIRING` /
// `PHONE_PAIRING_ENABLED` in api/pairing.py, surfaced as `phone_pairing` on
// /api/version). The affordance must be absent — not merely hidden by CSS, and
// not present-then-removed a tick later — so the boot markup must contain no
// phone/pairing wording at all, and the toolbar must close up with no empty gap
// or stray separator where the button used to be.
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

// store.ts reads persisted panel sizes at module scope. Node exposes a
// `localStorage` global that is a stub WITHOUT getItem, so the store's
// `typeof localStorage === 'undefined'` guard passes and the read then throws
// at import time. A minimal empty store keeps the import honest (real code
// path, default sizes) — hence the dynamic import after the stub.
vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {}, removeItem: () => {} })
const { TopBar } = await import('./TopBar')

const boot = () => renderToStaticMarkup(createElement(TopBar))

describe('the TopBar at boot, before /api/version answers', () => {
  it('renders no phone or pairing wording anywhere', () => {
    expect(boot()).not.toMatch(/phone|pair|\bQR\b/i)
  })

  it('renders no 📱 button', () => {
    expect(boot()).not.toContain('📱')
  })

  // The layout invariant the toolbar is built around: Export is the right-most
  // pinned control. Removing a neighbouring button must not disturb it.
  it('still ends with Export as the right-most pinned control', () => {
    const html = boot()
    const pinned = html.slice(html.lastIndexOf('topbar-pinned'))
    expect(pinned).toContain('Export')
    expect(pinned.lastIndexOf('Export')).toBeGreaterThan(pinned.indexOf('Save'))
  })

  // R3 (LEFT_RAIL_SPEC §8.1): the separators are gone with the tools that
  // stood between them — the bar is three groups, not a strip with dividers.
  it('has no separators left', () => {
    expect(boot()).not.toMatch(/width:1px/)
  })

  // LEFT_RAIL_SPEC §2.8: the activity chip is ALWAYS rendered, so its polite
  // live region exists before its first message — in the left group, after
  // the project chip and before the centre (Ratio).
  it('renders the activity chip\'s live region at boot, with nothing running', () => {
    const html = boot()
    const at = html.indexOf('data-activity')
    expect(at).toBeGreaterThan(html.indexOf('topbar-session'))
    expect(at).toBeGreaterThan(html.indexOf('class="tb-left"'))
    expect(at).toBeLessThan(html.indexOf('class="tb-center"'))
    expect(html).toMatch(/<div class="activity" data-activity="true"><span class="activity-sr-only" role="status" aria-live="polite"><\/span><\/div>/)
  })
})

// QA-012: every core control is IN the bar — none behind a sideways scroller.
// R3 (LEFT_RAIL_SPEC §1.4): the bar is a three-group grid; which words show at
// 900/1024/1280/1440 is measured in a real browser (test_wave_d_topbar_ui.py);
// this pins the structure that makes it so.
describe('the TopBar groups', () => {
  const group = (cls: string) => {
    const html = boot()
    const at = html.indexOf(`class="${cls}`)
    const ends = ['class="tb-left', 'class="tb-center', 'class="tb-right'].map((c) => html.indexOf(c)).filter((i) => i > at)
    return html.slice(at, ends.length ? Math.min(...ends) : undefined)
  }

  it('is a header named "Project" holding left, centre and right, in that order', () => {
    const html = boot()
    expect(html).toMatch(/^<header[^>]*class="topbar[^"]*"[^>]*aria-label="Project"/)
    const [l, c, r] = ['class="tb-left"', 'class="tb-center"', 'class="tb-right topbar-pinned"'].map((x) => html.indexOf(x))
    expect(l).toBeGreaterThan(-1)
    expect(c).toBeGreaterThan(l)
    expect(r).toBeGreaterThan(c)
    expect(html).toMatch(/data-density="\d"/)
  })

  it('opens with the aria-hidden brand mark, then the h1 wordmark and the project chip', () => {
    const left = group('tb-left')
    expect(left).toMatch(/^class="tb-left"><span class="tb-mark" aria-hidden="true"><svg[^>]*data-icon="brand"/)
    expect(left.indexOf('<h1 class="topbar-brand">Video AI Editor</h1>')).toBeLessThan(left.indexOf('topbar-session'))
  })

  it('has no horizontally scrolling strip', () => {
    expect(boot()).not.toContain('topbar-scroll')
  })

  it('centres one Ratio menu instead of nine loose aspect/preset buttons', () => {
    const t = group('tb-center')
    expect(t).toContain('ratio-trigger')
    expect(t).toMatch(/aria-haspopup="menu"/)
    expect(t).toMatch(/aria-label="Canvas ratio"/)
    const buttonTexts = [...t.matchAll(/<button\b[^>]*>([\s\S]*?)<\/button>/g)]
      .map((m) => m[1].replace(/<[^>]+>/g, '').trim())
    for (const label of ['Reels', 'Shorts', 'TikTok', 'IG 1:1', 'IG 4:5', '9:16', '16:9', '1:1', '4:5']) {
      expect(buttonTexts, label).not.toContain(label)
    }
  })

  // R2 (LEFT_RAIL_SPEC §8.1): Text and Captions moved to the left rail's
  // panels; the bar keeps only what is RUNNING (the activity chip).
  it('holds no Text or Captions tool any more', () => {
    const html = boot()
    expect(html).not.toContain('data-icon="text"')
    expect(html).not.toContain('data-text-presets')
    expect(html).not.toMatch(/cc-main|cc-caret|Text presets|>\s*Captions\s*</)
  })

  // R3 (LEFT_RAIL_SPEC §8.1, inverted): Help, Customize shortcuts and Settings
  // live at the rail foot (ToolRail.test.ts); the safe-zone <select>, the "⋯"
  // menu, the canvas facts pill and the version badge are gone from the bar.
  it('holds no Help, Shortcuts, Settings, safe-zone picker, "⋯" menu or version', () => {
    const html = boot()
    expect(html).not.toMatch(/Keyboard shortcuts|Customize keyboard|aria-label="Settings"/)
    expect(html).not.toMatch(/data-icon="(help|keyboard|settings|more)"/)
    expect(html).not.toContain('⌨')
    expect(html).not.toMatch(/<select|data-topbar-more|topbar-canvas|topbar-tools|topbar-wide/)
    expect(html).not.toMatch(/\bv\d+\.\d+/)
  })
})

// QA-012 / QA-026: Export stays the RIGHT-MOST pinned control even when the
// cluster also shows a finished export's "↓ MP4" link and an export error.
// Both used to render AFTER the Export button, so at every width the download
// link (or a 340 px error chip) sat to Export's right.
// The trigger's markup: its chevron is a decorative lucide icon, hidden from
// assistive tech (QA-102, QA-125).
const EXPORT_LABEL = 'Export<svg'

describe('the pinned cluster with an export link and an export error', () => {
  const seeded = async (edlHash = 'aaaaaaaaaaaaaaa1') => {
    vi.resetModules()
    const { useStore } = await import('../store')
    // SSR reads the store's INITIAL state (zustand's server snapshot), so the
    // finished-export state is seeded there — the real component, the real store.
    Object.assign(useStore.getInitialState(), {
      sessionId: 'A', sessionName: 'A', edlHash,
      edl: { canvas: { w: 1080, h: 1920, fps: 30, bg: '#000' }, duration: 4, tracks: [] },
      exportLinks: { A: { sid: 'A', url: '/api/sessions/A/files/exports/export_aaaaaaaaaaaaaaa1.mp4',
                          filename: 'export_aaaaaaaaaaaaaaa1.mp4', edlHash: 'aaaaaaaaaaaaaaa1' } },
      exportError: 'RuntimeError: the encoder ran out of disk',
    })
    const { TopBar: Bar } = await import('./TopBar')
    const html = renderToStaticMarkup(createElement(Bar))
    return html.slice(html.lastIndexOf('topbar-pinned'))
  }

  it('renders the link and the error, both before Export', async () => {
    const pinned = await seeded()
    const exportAt = pinned.indexOf(EXPORT_LABEL)
    expect(exportAt).toBeGreaterThan(-1)
    expect(pinned).toMatch(/data-icon="download"[^>]*>(?:<[^>]+>)*<\/svg> MP4/)
    expect(pinned).toContain('the encoder ran out of disk')
    expect(pinned.indexOf(' MP4')).toBeLessThan(exportAt)
    expect(pinned.indexOf('the encoder ran out of disk')).toBeLessThan(exportAt)
  })

  it('ends with the Export button: no control follows it', async () => {
    const pinned = await seeded()
    const tail = pinned.slice(pinned.indexOf(EXPORT_LABEL))
    expect(tail).not.toMatch(/<button|<a\b|⚠/)
  })

  // R3 density step 2: "(outdated)" becomes a warn dot — so the words must
  // live in the NAME, where no step can hide them.
  it('names a stale MP4 "(outdated)" and carries both the words and the dot', async () => {
    const pinned = await seeded('bbbbbbbbbbbbbbb2')
    expect(pinned).toMatch(/<button[^>]*class="tb-dl stale-dl"[^>]*aria-label="Save exported MP4 \(outdated\)"/)
    expect(pinned).toMatch(/ MP4<span class="tb-stale-dot" aria-hidden="true"><\/span><span class="tb-stale-word" aria-hidden="true"> \(outdated\)<\/span>/)
  })

  it('keeps a fresh MP4 unflagged', async () => {
    const pinned = await seeded()
    expect(pinned).toMatch(/<button[^>]*class="tb-dl"[^>]*aria-label="Save exported MP4"/)
    expect(pinned).not.toContain('tb-stale')
  })

  // Step 3 shows the error chip icon-only: the whole message stays its name.
  it('names the export error with the whole message, prefix stripped', async () => {
    const pinned = await seeded()
    expect(pinned).toMatch(/aria-label="Export failed: the encoder ran out of disk\. Dismiss"/)
    expect(pinned).toContain('<span class="tb-err-text">the encoder ran out of disk</span>')
    expect(pinned).not.toContain('RuntimeError')
  })
})
