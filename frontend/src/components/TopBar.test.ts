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

  // No empty gap and no orphaned separator where the button was: the two 1px
  // separators in .topbar-tools are the ones before TextTool and before Help,
  // and both still sit between real controls.
  it('leaves no trailing separator at the end of the tools section', () => {
    const html = boot()
    const scroll = html.slice(html.indexOf('topbar-tools'), html.lastIndexOf('topbar-pinned'))
    // The last thing in the tools section is a button (⌨ / ⋯), not a
    // separator span left behind by a removed neighbour.
    expect(scroll.lastIndexOf('</button>')).toBeGreaterThan(scroll.lastIndexOf('width:1px'))
  })
})

// QA-012: every core control is IN the bar — none behind a sideways scroller.
// Which of them is visible at 1024/1280/1440/1920 is measured in a real browser
// (qa-fix/A7-panels live_check.py); this pins the structure that makes it so.
describe('the TopBar tools', () => {
  const tools = () => {
    const html = boot()
    return html.slice(html.indexOf('topbar-tools'), html.lastIndexOf('topbar-pinned'))
  }

  it('has no horizontally scrolling strip', () => {
    expect(boot()).not.toContain('topbar-scroll')
  })

  it('holds one Ratio menu instead of nine loose aspect/preset buttons', () => {
    const t = tools()
    expect(t).toContain('ratio-trigger')
    expect(t).toMatch(/aria-haspopup="menu"/)
    const buttonTexts = [...t.matchAll(/<button\b[^>]*>([\s\S]*?)<\/button>/g)]
      .map((m) => m[1].replace(/<[^>]+>/g, '').trim())
    for (const label of ['Reels', 'Shorts', 'TikTok', 'IG 1:1', 'IG 4:5', '9:16', '16:9', '1:1', '4:5']) {
      expect(buttonTexts, label).not.toContain(label)
    }
  })

  it('keeps Text, Captions, Help and Shortcuts inline', () => {
    const t = tools()
    expect(t).toMatch(/<b>T<\/b> Text/)
    expect(t).toContain('Captions')
    // Glyph buttons carry a name; the glyph itself is aria-hidden (QA-102).
    expect(t).toMatch(/aria-label="Keyboard shortcuts"[^>]*><span aria-hidden="true">\?<\/span><\/button>/)
    // One monochrome icon set (wave-B review): the shortcuts button is the
    // keyboard ICON, never the '⌨' glyph.
    expect(t).not.toContain('⌨')
    expect(t).toMatch(/aria-label="Customize keyboard shortcuts"[^>]*><svg[^>]*class="icon"/)
  })
})

// QA-012 / QA-026: Export stays the RIGHT-MOST pinned control even when the
// cluster also shows a finished export's "↓ MP4" link and an export error.
// Both used to render AFTER the Export button, so at every width the download
// link (or a 340 px error chip) sat to Export's right.
// The trigger's markup: its ▾ is decorative and hidden from assistive tech (QA-102).
const EXPORT_LABEL = 'Export <span aria-hidden="true">▾</span>'

describe('the pinned cluster with an export link and an export error', () => {
  const seeded = async () => {
    vi.resetModules()
    const { useStore } = await import('../store')
    // SSR reads the store's INITIAL state (zustand's server snapshot), so the
    // finished-export state is seeded there — the real component, the real store.
    Object.assign(useStore.getInitialState(), {
      sessionId: 'A', sessionName: 'A', edlHash: 'aaaaaaaaaaaaaaa1',
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
    expect(pinned).toContain('↓ MP4')
    expect(pinned).toContain('the encoder ran out of disk')
    expect(pinned.indexOf('↓ MP4')).toBeLessThan(exportAt)
    expect(pinned.indexOf('the encoder ran out of disk')).toBeLessThan(exportAt)
  })

  it('ends with the Export button: no control follows it', async () => {
    const pinned = await seeded()
    const tail = pinned.slice(pinned.indexOf(EXPORT_LABEL))
    expect(tail).not.toMatch(/<button|<a\b|⚠/)
  })
})
