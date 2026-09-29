import { describe, expect, it } from 'vitest'
import { soundClipUnder, splitTargets, splitTimeFor } from './splitTargets'
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

/** Three 5 s v1 clips with 0.5 s dissolves at 5 and 10, and a voiceover laid
 *  at 0–15 that plays WHOLE from render 0 (`soundSpan`) — final sweep 2. */
function vlog(): EDL {
  return {
    version: 1, duration: 14, canvas: { w: 320, h: 180, fps: 30 },
    tracks: [
      { id: 'v1', type: 'video', z: 0,
        transitions: [{ at: 5, type: 'fade', duration: 0.5 }, { at: 10, type: 'fade', duration: 0.5 }],
        clips: [
          { id: 'a', track: 'v1', src: 'a.mp4', in: 0, out: 5, start: 0 },
          { id: 'b', track: 'v1', src: 'b.mp4', in: 0, out: 5, start: 5 },
          { id: 'c', track: 'v1', src: 'c.mp4', in: 0, out: 5, start: 10 },
        ] },
      { id: 'vo', type: 'audio', z: 30, clips: [
        { id: 'n', track: 'vo', src: 'n.wav', in: 0, out: 15, start: 0 },
      ] },
    ],
  } as unknown as EDL
}

describe('splitTargets on a SOUND lane (plays whole, not through the overlay inverse)', () => {
  it('cuts the voiceover at the playhead, not one second after it', () => {
    // The overlay inverse added the 1.0 s of upstream overlap: split_at 11.
    expect(splitTargets(vlog(), new Set(['n']), 10)).toEqual([{ track: 'vo', time: 10 }])
    expect(splitTimeFor(vlog(), 'vo', 10, vlog().tracks[1].clips[0])).toBe(10)
  })
  it('a voiceover laid after a seam is pulled by its own run\'s overlap', () => {
    const e = vlog()
    e.tracks[1].clips[0].start = 6        // plays from render 5.5, whole
    expect(splitTargets(e, new Set(['n']), 8)).toEqual([{ track: 'vo', time: 8.5 }])
  })
  it('the timeline\'s "Split at playhead" finds the sound clip under the playhead', () => {
    expect(soundClipUnder(vlog(), 'vo', 10)?.id).toBe('n')
    expect(soundClipUnder(vlog(), 'vo', 14.5)).toBeUndefined()   // past its drawn end (cut at the picture end)
    expect(soundClipUnder(vlog(), 'v1', 10)).toBeUndefined()
  })
})
