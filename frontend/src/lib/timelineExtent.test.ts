import { describe, expect, it } from 'vitest'
import { defaultOverlayEnd, videoContentEnd, MIN_OVERLAY_S } from './timelineExtent'
import type { EDL } from '../types'

const edl = (clips: Array<{ start: number; in: number; out: number }>): EDL => ({
  version: 2,
  canvas: { w: 1920, h: 1080, fps: 30 },
  duration: 0,
  tracks: [
    { id: 'v1', type: 'video', z: 0, clips: clips.map((c, i) => ({ id: `c${i}`, src: '/x.mp4', ...c })) },
    // A text clip that overshoots — the thing that must NOT be counted.
    { id: 'text', type: 'text', z: 9, clips: [{ id: 't1', text: 'hi', start: 1.375, end: 4.375 }] },
  ],
} as unknown as EDL)

describe('videoContentEnd', () => {
  it('measures v1 only, ignoring an overshooting overlay', () => {
    // The reported session: v1 ends at 4.0 while a text clip runs to 4.375.
    expect(videoContentEnd(edl([{ start: 0, in: 0, out: 2 }, { start: 2, in: 2, out: 4 }])))
      .toBeCloseTo(4.0)
  })

  it('assembles rather than taking a geometric max', () => {
    // Two overlapping clips still PLAY one after the other, so the picture is
    // longer than the furthest end — matching recompute_duration's v1 cursor.
    expect(videoContentEnd(edl([{ start: 0, in: 0, out: 4 }, { start: 2, in: 0, out: 4 }])))
      .toBeCloseTo(8.0)
  })

  it('is 0 with no video and survives a null EDL', () => {
    expect(videoContentEnd(edl([]))).toBe(0)
    expect(videoContentEnd(null)).toBe(0)
  })
})

describe('defaultOverlayEnd', () => {
  it('clamps a default that would outlive the picture', () => {
    // The reported case exactly: playhead 1.375, 3s default, video ends 4.0.
    expect(defaultOverlayEnd(1.375, 3, 4.0)).toBeCloseTo(4.0)
  })

  it('leaves a default that fits alone', () => {
    expect(defaultOverlayEnd(0.5, 3, 10)).toBeCloseTo(3.5)
  })

  it('does not clamp when there is no video to clamp against', () => {
    expect(defaultOverlayEnd(1, 3, 0)).toBeCloseTo(4)
  })

  it('honours the full default when the playhead is already past the picture', () => {
    // Parked past the end is a deliberate placement; collapsing it to nothing
    // would make the overlay unusable instead of merely long.
    expect(defaultOverlayEnd(9, 3, 4)).toBeCloseTo(12)
    expect(defaultOverlayEnd(4, 3, 4)).toBeCloseTo(7)
  })

  it('still clamps when the playhead is merely NEAR the end', () => {
    // The bug this test exists for: a `start >= videoEnd - MIN` past-the-end
    // test read 0.575 inside a 1.0s picture as "past the end" and handed back
    // the full 3s, so the overlay ran to 3.575 and held the timeline open.
    // Measured live in the app before the fix.
    const e = defaultOverlayEnd(0.575, 3, 1.0)
    expect(e).toBeLessThan(3.575)
    expect(e).toBeCloseTo(1.075)          // floored to a usable 0.5s
    expect(e - 0.575).toBeGreaterThanOrEqual(MIN_OVERLAY_S - 1e-9)
  })

  it('never produces an unreadably short overlay', () => {
    // Just inside the end: clamping to the video would leave ~0.1s, which is a
    // few frames and invisible in practice (round-5 finding M-02).
    const e = defaultOverlayEnd(3.9, 3, 4.0)
    expect(e - 3.9).toBeGreaterThanOrEqual(MIN_OVERLAY_S - 1e-9)
  })
})
