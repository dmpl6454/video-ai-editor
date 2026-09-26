// QA-118: labels are fitted, never cut mid-glyph, and never print on each other.
import { describe, expect, it } from 'vitest'
import { collides, fitLabel, laneTooltipHead, markerChips, stickyLabelX } from './timelineLabels'

const m = (s: string) => s.length * 6   // 6 px per character

describe('fitLabel', () => {
  it('keeps text that fits and ellipsizes text that does not', () => {
    expect(fitLabel('interview.mp4', 200, m)).toBe('interview.mp4')
    expect(fitLabel('interview.mp4', 42, m)).toBe('interv…')
    expect(m(fitLabel('interview_final_v2.mp4', 60, m))).toBeLessThanOrEqual(60)
  })
  it('returns nothing when not even one character fits', () => {
    expect(fitLabel('abc', 8, m)).toBe('')
    expect(fitLabel('abc', 0, m)).toBe('')
  })
})

describe('stickyLabelX', () => {
  it('pins a scrolled-off clip title to the visible lane start', () => {
    expect(stickyLabelX(100, 80)).toBe(106)
    expect(stickyLabelX(-400, 380)).toBe(386)
  })
})

describe('marker chips and ruler collisions', () => {
  it('turns marker labels into chips and drops one that would overlap its neighbour', () => {
    const chips = markerChips([{ x: 400, label: 'marker' }, { x: 420, label: 'B' }, { x: 600, label: '' }], m)
    expect(chips.map((c) => c.text)).toEqual(['marker', 'Marker'])
    expect(chips[0]).toEqual({ x: 405, w: 6 * 6 + 8, text: 'marker' })
  })
  it('a ruler label under a chip collides; one beside it does not', () => {
    const chips = [{ x: 405, w: 44 }]
    expect(collides(403, 30, chips)).toBe(true)
    expect(collides(450, 30, chips)).toBe(false)
  })
})

describe('laneTooltipHead', () => {
  it('does not repeat the lane name', () => {
    expect(laneTooltipHead('Main video', 'Main video — drop footage here')).toBe('Main video — drop footage here')
    expect(laneTooltipHead('Hook', 'Hook text')).toBe('Hook — Hook text')
    expect(laneTooltipHead('Main audio', 'Main audio')).toBe('Main audio')
  })
})

describe('clip name plate (QA-118 remainder)', () => {
  it('wraps the measured text with padding and centres on the row', async () => {
    const { labelPlate, LABEL_PLATE_ALPHA } = await import('./timelineLabels')
    const p = labelPlate(106, 80, 40, 36)
    expect(p).toEqual({ x: 102, y: 50, w: 88, h: 16 })
    expect(LABEL_PLATE_ALPHA).toBeGreaterThanOrEqual(0.6)
  })
})
