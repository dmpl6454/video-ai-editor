import { describe, expect, it } from 'vitest'
import { edlTimeFromOutput, outStart, v1Layout, type LayoutClip } from './timelineLayout'

const clip = (id: string, start: number, duration: number): LayoutClip =>
  ({ id, start, duration })

/** The four 2s clips from the real report (session s_2cfa632c89). */
const FOUR = [clip('a', 0, 2), clip('b', 2, 2), clip('c', 4, 2), clip('d', 6, 2)]

describe('v1Layout', () => {
  it('shifts nothing when there are no transitions', () => {
    const { shift } = v1Layout(FOUR, [])
    for (const c of FOUR) expect(outStart(c, shift)).toBe(c.start)
  })

  it('reproduces the reported session exactly', () => {
    // 8s split at 2/4/6, transitions at 2.0 and 4.0 only -> renders 7.0s.
    const { shift } = v1Layout(FOUR, [
      { at: 2, duration: 0.5 }, { at: 4, duration: 0.5 },
    ])
    expect(outStart(FOUR[0], shift)).toBeCloseTo(0.0, 6)
    expect(outStart(FOUR[1], shift)).toBeCloseTo(1.5, 6)
    expect(outStart(FOUR[2], shift)).toBeCloseTo(3.0, 6)
    expect(outStart(FOUR[3], shift)).toBeCloseTo(5.0, 6)
    // The last clip must END exactly at the duration the backend reports.
    expect(outStart(FOUR[3], shift) + FOUR[3].duration).toBeCloseTo(7.0, 6)
  })

  it('matches the three-transition case the backend renders as 6.5s', () => {
    const { shift } = v1Layout(FOUR, [
      { at: 2, duration: 0.5 }, { at: 4, duration: 0.5 }, { at: 6, duration: 0.5 },
    ])
    expect(outStart(FOUR[3], shift) + FOUR[3].duration).toBeCloseTo(6.5, 6)
  })

  it('does not charge a transition at a real GAP (the renderer keeps the cut)', () => {
    // clip b starts 1s late: filler goes in, so the transition never applies.
    const gapped = [clip('a', 0, 2), clip('b', 3, 2)]
    const { shift, seams } = v1Layout(gapped, [{ at: 2, duration: 0.5 }])
    expect(outStart(gapped[1], shift)).toBe(3)
    expect(seams[0].overlap).toBe(0)
  })

  it('still charges an OVERLAPPING pair (the renderer packs those)', () => {
    // Testing abs(nxt.start - boundary) would wrongly skip this.
    const over = [clip('a', 0, 2), clip('b', 1.5, 2)]
    const { seams } = v1Layout(over, [{ at: 2, duration: 0.5 }])
    expect(seams[0].overlap).toBeCloseTo(0.5, 6)
  })

  it('never overlaps further than the shorter clip is long', () => {
    const short = [clip('a', 0, 2), clip('b', 2, 0.2)]
    const { shift } = v1Layout(short, [{ at: 2, duration: 1.5 }])
    expect(shift.get('b')).toBeCloseTo(0.2, 6)
  })

  it('matches a transition within 0.05s of the boundary, not beyond', () => {
    const near = v1Layout(FOUR, [{ at: 2.04, duration: 0.5 }])
    expect(near.shift.get('b')).toBeCloseTo(0.5, 6)
    const far = v1Layout(FOUR, [{ at: 2.06, duration: 0.5 }])
    expect(far.shift.get('b')).toBe(0)
  })

  it('accumulates across seams, carrying past an uncharged one', () => {
    // Transition at 2.0 applies; the 4.0 seam has a gap after it, so clip d
    // is still pulled left by the FIRST transition only.
    const withGap = [clip('a', 0, 2), clip('b', 2, 2), clip('c', 5, 2)]
    const { shift } = v1Layout(withGap, [{ at: 2, duration: 0.5 }])
    expect(shift.get('b')).toBeCloseTo(0.5, 6)
    expect(shift.get('c')).toBeCloseTo(0.5, 6)
  })

  it('places the seam affordance at the middle of the overlap', () => {
    const { seams } = v1Layout(FOUR, [{ at: 2, duration: 0.5 }])
    // clip a ends at 2.0 output, clip b starts at 1.5 output -> middle 1.75.
    expect(seams[0].outAt).toBeCloseTo(1.75, 6)
  })

  it('places a hard cut exactly on the boundary', () => {
    const { seams } = v1Layout(FOUR, [])
    expect(seams[0].outAt).toBeCloseTo(2.0, 6)
    expect(seams[1].outAt).toBeCloseTo(4.0, 6)
  })

  it('sorts by start rather than trusting array order', () => {
    const shuffled = [FOUR[2], FOUR[0], FOUR[3], FOUR[1]]
    const { shift } = v1Layout(shuffled, [{ at: 2, duration: 0.5 }])
    expect(shift.get('a')).toBe(0)
    expect(shift.get('b')).toBeCloseTo(0.5, 6)
  })

  it('handles an empty or single-clip track', () => {
    expect(v1Layout([], []).shift.size).toBe(0)
    const one = v1Layout([clip('a', 0, 2)], [{ at: 2, duration: 0.5 }])
    expect(one.shift.get('a')).toBe(0)
    expect(one.seams).toEqual([])
  })

  it('round-trips output time back to EDL time', () => {
    const { shift } = v1Layout(FOUR, [
      { at: 2, duration: 0.5 }, { at: 4, duration: 0.5 },
    ])
    // Each clip's own start must survive the round trip exactly.
    for (const c of FOUR) {
      expect(edlTimeFromOutput(outStart(c, shift), FOUR, shift)).toBeCloseTo(c.start, 6)
    }
    // A point inside clip 4 (output 5.0-7.0) maps back into EDL 6.0-8.0.
    expect(edlTimeFromOutput(6.0, FOUR, shift)).toBeCloseTo(7.0, 6)
    // Past the end, the full accumulated overlap applies.
    expect(edlTimeFromOutput(7.0, FOUR, shift)).toBeCloseTo(8.0, 6)
  })

  it('is the identity when there are no transitions', () => {
    const { shift } = v1Layout(FOUR, [])
    for (const t of [0, 1.3, 5.5, 8]) {
      expect(edlTimeFromOutput(t, FOUR, shift)).toBeCloseTo(t, 6)
    }
  })

  it('respects speed-adjusted durations (effective, not raw out-in)', () => {
    // A 2x clip occupies 1s of timeline; the seam is at 1.0, not 2.0.
    const fast = [clip('a', 0, 1), clip('b', 1, 2)]
    const { shift } = v1Layout(fast, [{ at: 1, duration: 0.5 }])
    expect(shift.get('b')).toBeCloseTo(0.5, 6)
  })
})
