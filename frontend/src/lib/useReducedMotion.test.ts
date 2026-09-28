import { describe, expect, it, vi, afterEach } from 'vitest'
import { prefersReducedMotion } from './useReducedMotion'

// review RE: the setting must be read LIVE; the hook subscribes to the media
// query's change event (a Playwright test flips it with emulate_media and
// counts the running Web Animations in both browsers).
describe('prefersReducedMotion', () => {
  afterEach(() => { vi.unstubAllGlobals() })
  it('reads the media query each time it is asked', () => {
    let reduce = false
    vi.stubGlobal('window', { matchMedia: (q: string) => ({ matches: q.includes('reduce') && reduce }) })
    expect(prefersReducedMotion()).toBe(false)
    reduce = true
    expect(prefersReducedMotion()).toBe(true)
  })
  it('is false without matchMedia', () => {
    vi.stubGlobal('window', {})
    expect(prefersReducedMotion()).toBe(false)
  })
})
