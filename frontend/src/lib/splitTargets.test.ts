import { describe, expect, it } from 'vitest'
import { splitTargets, splitTimeFor } from './splitTargets'
import type { EDL } from '../types'

/** Two 2 s v1 clips, one 0.5 s fade at 2.0, a caption at layout 3.0–4.0
 *  (on screen at render 2.5–3.5). */
function edl(withFade: boolean): EDL {
  return {
    version: 1, duration: withFade ? 3.5 : 4.0, canvas: { w: 320, h: 180, fps: 30 },
    tracks: [
      { id: 'v1', type: 'video', z: 0,
        transitions: withFade ? [{ at: 2, type: 'fade', duration: 0.5 }] : [],
        clips: [
          { id: 'a', track: 'v1', src: 'a.mp4', in: 0, out: 2, start: 0 },
          { id: 'b', track: 'v1', src: 'b.mp4', in: 0, out: 2, start: 2 },
        ] },
      { id: 'captions', type: 'captions', z: 13, clips: [
        { id: 'cue', track: 'captions', text: 'hi', start: 3.0, end: 4.0 },
      ] },
    ],
  } as unknown as EDL
}

describe('splitTargets (⌘B)', () => {
  it('cuts the selected caption where the playhead is ON SCREEN inside it', () => {
    // Playhead at render 2.7 = layout 3.2, inside the caption on screen. The
    // old layout test (3.0 <= 2.7?) said "not inside" and cut v1 at 2.7.
    expect(splitTargets(edl(true), new Set(['cue']), 2.7))
      .toEqual([{ track: 'captions', time: expect.closeTo(3.2, 9) }])
  })
  it('falls back to v1, decoded through v1\'s own inverse', () => {
    expect(splitTargets(edl(true), new Set(), 2.7))
      .toEqual([{ track: 'v1', time: expect.closeTo(3.2, 9) }])
    // Before the seam nothing moves.
    expect(splitTargets(edl(true), new Set(), 1.0)).toEqual([{ track: 'v1', time: 1.0 }])
  })
  it('splits the selected v1 clip at its layout time', () => {
    expect(splitTargets(edl(true), new Set(['b']), 2.7))
      .toEqual([{ track: 'v1', time: expect.closeTo(3.2, 9) }])
  })
  it('is unchanged without transitions', () => {
    expect(splitTargets(edl(false), new Set(['cue']), 3.2))
      .toEqual([{ track: 'captions', time: 3.2 }])
    expect(splitTargets(edl(false), new Set(), 2.7)).toEqual([{ track: 'v1', time: 2.7 }])
  })
  it('one split per track, even with several selected clips on it', () => {
    const e = edl(false)
    expect(splitTargets(e, new Set(['a', 'b', 'cue']), 3.2))
      .toEqual([{ track: 'v1', time: 3.2 }, { track: 'captions', time: 3.2 }])
  })
})

describe('splitTimeFor (timeline "Split at playhead")', () => {
  it('uses the lane\'s own inverse', () => {
    expect(splitTimeFor(edl(true), 'captions', 2.7)).toBeCloseTo(3.2, 9)
    expect(splitTimeFor(edl(true), 'v1', 2.7)).toBeCloseTo(3.2, 9)
    // Inside the crossfade window (render 1.5–2.0): overlay lanes snap to the
    // seam, v1 maps into clip B's head.
    expect(splitTimeFor(edl(true), 'captions', 1.75)).toBeCloseTo(2.0, 9)
    expect(splitTimeFor(edl(true), 'v1', 1.75)).toBeCloseTo(2.25, 9)
    expect(splitTimeFor(null, 'v1', 1.75)).toBe(1.75)
  })
})
