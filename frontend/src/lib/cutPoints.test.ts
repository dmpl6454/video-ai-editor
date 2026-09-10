// The v1 cut list and the target rule, on EDLs shaped like the backend's:
// effective ends honour `speed`, a gap is not a cut, the last transition on
// a seam wins, and the target follows selection first, playhead second.
import { describe, expect, it } from 'vitest'
import { formatCutTime, targetCut, v1CutPoints } from './cutPoints'
import type { EDL } from '../types'

const media = (id: string, start: number, len: number, extra: Record<string, unknown> = {}) =>
  ({ id, src: `/m/${id}.mp4`, in: 0, out: len, start, ...extra })

function edl(clips: unknown[], transitions: unknown[] = []): EDL {
  const v1 = { id: 'v1', type: 'video', z: 0, clips, transitions } as unknown as EDL['tracks'][number]
  const text = { id: 'text', type: 'text', z: 5, clips: [{ id: 't1', text: 'hi', start: 0, end: 2 }] }
  return { version: 2, duration: 30, canvas: { w: 1920, h: 1080, fps: 30, bg: '#000' }, tracks: [v1, text] }
}

describe('v1CutPoints', () => {
  it('lists each adjacent pair once, in order, ignoring text clips and gaps', () => {
    const e = edl([media('c', 20, 5), media('a', 0, 10), media('b', 10, 10), media('d', 26, 4)])
    const cuts = v1CutPoints(e)
    expect(cuts.map((c) => [c.before.id, c.after.id, c.at])).toEqual([['a', 'b', 10], ['b', 'c', 20]])
    // d starts 1 s after c ends: a gap the renderer fills with black, not a seam.
    expect(cuts.some((c) => c.after.id === 'd')).toBe(false)
  })

  it('uses the EFFECTIVE end of a retimed clip', () => {
    const e = edl([media('a', 0, 10, { speed: 2 }), media('b', 5, 10)])
    expect(v1CutPoints(e).map((c) => c.at)).toEqual([5])
  })

  it('attaches the transition on the seam, last match winning, within 0.05 s', () => {
    const e = edl([media('a', 0, 10), media('b', 10, 10)], [
      { at: 10.0, type: 'fade', duration: 0.5 },
      { at: 10.03, type: 'glitch', duration: 0.25 },
      { at: 10.2, type: 'wipeleft', duration: 0.4 },
    ])
    const [cut] = v1CutPoints(e)
    expect(cut.tr?.type).toBe('glitch')
  })

  it('is empty without a v1 track, with one clip, or with no EDL', () => {
    expect(v1CutPoints(null)).toEqual([])
    expect(v1CutPoints(edl([media('a', 0, 10)]))).toEqual([])
    expect(v1CutPoints({ ...edl([]), tracks: [] })).toEqual([])
  })
})

describe('targetCut', () => {
  const cuts = v1CutPoints(edl([media('a', 0, 10), media('b', 10, 10), media('c', 20, 10)]))

  it('a selected clip targets its leading cut; the first clip its trailing cut', () => {
    expect(targetCut(cuts, 'c', 0)).toMatchObject({ index: 1, reason: 'selection' })
    expect(targetCut(cuts, 'b', 25)).toMatchObject({ index: 0, reason: 'selection' })
    expect(targetCut(cuts, 'a', 25)).toMatchObject({ index: 0, reason: 'selection' })
  })

  it('a selection that is not a v1 media clip falls through to the playhead', () => {
    expect(targetCut(cuts, 't1', 19)).toMatchObject({ index: 1, reason: 'playhead' })
    expect(targetCut(cuts, null, 3)).toMatchObject({ index: 0, reason: 'playhead' })
    expect(targetCut(cuts, null, 15)).toMatchObject({ index: 0, reason: 'playhead' })   // ties go to the earlier cut
    expect(targetCut(cuts, null, 15.01)).toMatchObject({ index: 1, reason: 'playhead' })
  })

  it('is null with no cuts', () => {
    expect(targetCut([], 'a', 0)).toBeNull()
  })

  it('formats a cut time to centiseconds', () => {
    expect(formatCutTime(10)).toBe('10.00 s')
    expect(formatCutTime(12.345)).toBe('12.35 s')
  })
})
