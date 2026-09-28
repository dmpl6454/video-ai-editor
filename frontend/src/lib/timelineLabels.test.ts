// QA-118: labels are fitted, never cut mid-glyph, and never print on each other.
import { describe, expect, it } from 'vitest'
import { BOWTIE_R, clearOfBowtie, collides, fitLabel, laneTooltipHead, markerChips, stickyLabelX } from './timelineLabels'

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

// Final QA (editor-ux): with a dissolve on every cut, the bowtie sits at the
// MIDDLE of the overlap — 9.4 px into the incoming clip at 37.5 px/s, 50 px at
// 200 px/s — and the name was only moved when the bowtie was within 9 px of
// the clip's edge, so it read "y.mp4", "rtical_street.mp4", "c◉.mp4".
describe('clearOfBowtie', () => {
  it('starts the name past a head bowtie, wherever in the overlap it sits', () => {
    // 37.5 px/s, 0.5 s overlap: bowtie 9.4 px in — the old 9 px test missed it.
    const a = clearOfBowtie(100 + 6, 100, 187, 109.4, null)
    expect(a.lx).toBeGreaterThanOrEqual(109.4 + BOWTIE_R)
    // Zoomed in: bowtie 50 px in, over what used to be the name's 2nd glyph.
    expect(clearOfBowtie(106, 100, 1000, 150, null).lx).toBeGreaterThanOrEqual(150 + BOWTIE_R)
  })
  it('ends the name before a tail bowtie', () => {
    const r = clearOfBowtie(106, 100, 187, null, 277.6)
    expect(r.maxRight).toBeLessThanOrEqual(277.6 - BOWTIE_R)
    expect(r.lx).toBe(106)
  })
  it('changes nothing without transitions, and never leaves the clip', () => {
    expect(clearOfBowtie(106, 100, 187, null, null)).toEqual({ lx: 106, maxRight: 100 + 187 - 8 })
    // A sticky title already right of the bowtie stays where it is.
    expect(clearOfBowtie(400, 100, 900, 109, null).lx).toBe(400)
    // A clip narrower than its bowtie: clamped to the clip.
    expect(clearOfBowtie(106, 100, 10, 105, null).lx).toBeLessThanOrEqual(110)
  })
})
