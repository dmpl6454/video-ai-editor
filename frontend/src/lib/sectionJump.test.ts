// Final QA: the Inspector jump list used scrollIntoView({block:'start'}),
// which parked each section's header UNDER the sticky jump bar (97 px hidden
// at 1440×900 with a three-row bar) and also scrolled the document by 1 px.
import { describe, expect, it } from 'vitest'
import { sectionScrollTop } from './sectionJump'

describe('sectionScrollTop', () => {
  // The measurements from the report: the scroll container's box starts at
  // 79 px on screen, the stuck bar is 97 px tall (79 → 176).
  it('puts the section top just below the stuck bar, not under it', () => {
    const top = sectionScrollTop({ containerTop: 79, clientTop: 0, scrollTop: 0, sectionTop: 1279, barHeight: 97 })
    // After scrolling by `top`, the section sits at 1279 − top on screen.
    expect(1279 - top).toBeGreaterThanOrEqual(176)
    expect(1279 - top).toBeLessThanOrEqual(176 + 8)
  })

  it('accounts for the current scroll offset and the container border', () => {
    const a = sectionScrollTop({ containerTop: 100, clientTop: 1, scrollTop: 400, sectionTop: 600, barHeight: 50 })
    // section is 600 − 101 = 499 px into the scrollport; content offset = 899
    expect(a).toBe(400 + 499 - 50 - 4)
  })

  it('never asks for a negative scroll', () => {
    expect(sectionScrollTop({ containerTop: 0, clientTop: 0, scrollTop: 0, sectionTop: 10, barHeight: 90 })).toBe(0)
  })
})
