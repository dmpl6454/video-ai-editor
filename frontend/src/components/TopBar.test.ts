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
  // separators in .topbar-scroll are the ones before TextTool and before the
  // platform presets, and both still sit between real controls.
  it('leaves no trailing separator at the end of the scrolling section', () => {
    const html = boot()
    const scroll = html.slice(html.indexOf('topbar-scroll'), html.lastIndexOf('topbar-pinned'))
    // The last thing in the scrolling section is a button (⌨ shortcuts), not a
    // separator span left behind by a removed neighbour.
    expect(scroll.lastIndexOf('</button>')).toBeGreaterThan(scroll.lastIndexOf('width:1px'))
  })
})
