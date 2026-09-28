// Final QA: the live region was position:absolute with no top/left, so it sat
// at its static position just past the bottom of the fixed-viewport shell —
// the page became 1 px taller and any scrollIntoView scrolled the app by 1 px.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { announce } from './announce'

interface FakeEl { style: { cssText: string }; dataset: Record<string, string>; isConnected: boolean; textContent: string; setAttribute(k: string, v: string): void }

describe('announce', () => {
  afterEach(() => { vi.unstubAllGlobals() })

  it('pins its live region inside the viewport so it can never extend the page', () => {
    const made: FakeEl[] = []
    vi.stubGlobal('document', {
      createElement: () => {
        const el: FakeEl = { style: { cssText: '' }, dataset: {}, isConnected: true, textContent: '', setAttribute: () => {} }
        made.push(el)
        return el
      },
      body: { appendChild: () => {} },
    })
    announce('Selected intro.mp4')
    expect(made).toHaveLength(1)
    const css = made[0].style.cssText.replace(/\s+/g, '')
    expect(css).toContain('position:fixed')
    expect(css).toContain('top:0')
    expect(css).toContain('left:0')
    expect(made[0].textContent).toBe('Selected intro.mp4')
  })
})
