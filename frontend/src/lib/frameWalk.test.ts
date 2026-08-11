import { describe, expect, it } from 'vitest'
import { frameChainFor, keyframeAtOrBefore, type WalkSample } from './frameWalk'

const F = 33333          // one frame at 30fps, microseconds

/** Samples in DECODE order. `order` lists each sample's DISPLAY index, so
 *  [0,3,1,2] is the I P B B pattern a real x264 GOP arrives in. */
function gop(order: number[], base = 0): WalkSample[] {
  return order.map((display, i) => ({
    cts: (base + display) * F,
    duration: F,
    is_sync: i === 0 && display === order[0] && base === 0 ? true : false,
  }))
}

/** A no-B-frame stream: decode order == display order. */
const SIMPLE: WalkSample[] = Array.from({ length: 10 }, (_, i) => ({
  cts: i * F, duration: F, is_sync: i === 0,
}))

/** Two GOPs of I P B B P B B, the shape libx264 emits by default (the preview
 *  really does carry these — ffprobe reports has_b_frames=1, pict_type B). */
const BFRAMES: WalkSample[] = [
  { cts: 0 * F, duration: F, is_sync: true },   // I  display 0
  { cts: 3 * F, duration: F, is_sync: false },  // P  display 3
  { cts: 1 * F, duration: F, is_sync: false },  // B  display 1
  { cts: 2 * F, duration: F, is_sync: false },  // B  display 2
  { cts: 6 * F, duration: F, is_sync: false },  // P  display 6
  { cts: 4 * F, duration: F, is_sync: false },  // B  display 4
  { cts: 5 * F, duration: F, is_sync: false },  // B  display 5
]
const B_KEYS = [0]

describe('keyframeAtOrBefore', () => {
  it('finds the latest keyframe at or before the target', () => {
    const keys = [0, 4, 8]
    expect(keyframeAtOrBefore(SIMPLE, keys, 0)).toBe(0)
    expect(keyframeAtOrBefore(SIMPLE, keys, 3.5 * F)).toBe(0)
    expect(keyframeAtOrBefore(SIMPLE, keys, 4 * F)).toBe(4)
    expect(keyframeAtOrBefore(SIMPLE, keys, 9 * F)).toBe(8)
  })
  it('never goes past the start for a target before the first frame', () => {
    expect(keyframeAtOrBefore(SIMPLE, [0, 4], -5)).toBe(0)
  })
})

describe('frameChainFor — no B-frames', () => {
  it('paints the frame COVERING the target, not the one after it', () => {
    // 3.5 frames in: <video>.currentTime = 3.5F displays frame 3, so the
    // canvas must too. Painting frame 4 is what made the picture jump back
    // when the canvas handed over.
    const { paintUs } = frameChainFor(SIMPLE, [0], 3.5 * F)
    expect(paintUs).toBe(3 * F)
  })

  it('paints exactly the frame whose boundary the target sits on', () => {
    expect(frameChainFor(SIMPLE, [0], 5 * F).paintUs).toBe(5 * F)
  })

  it('a single-frame step advances the painted frame by exactly one', () => {
    const a = frameChainFor(SIMPLE, [0], 5 * F).paintUs
    const b = frameChainFor(SIMPLE, [0], 6 * F).paintUs
    expect(b - a).toBe(F)
  })

  it('clamps a target past the end to the last frame', () => {
    const { paintUs, lastIdx, maxEndUs } = frameChainFor(SIMPLE, [0], 999 * F)
    expect(paintUs).toBe(9 * F)
    expect(lastIdx).toBe(9)
    expect(maxEndUs).toBe(10 * F)
  })

  it('handles an empty sample table without emitting a bogus chain', () => {
    expect(frameChainFor([], [], 1000)).toEqual(
      { startIdx: 0, lastIdx: -1, paintUs: -1, maxEndUs: 0 })
  })
})

describe('frameChainFor — B-frames (decode order != display order)', () => {
  it('paints the B-frame covering the target, not the P-frame ahead of it', () => {
    // Target inside display-frame 2, which is the LAST sample in decode order
    // (index 3). The old walk stopped at index 1 — the P at display 3 — and
    // painted it: two frames ahead, exactly the measured canvas-165/video-163
    // split.
    const { paintUs, lastIdx } = frameChainFor(BFRAMES, B_KEYS, 2.5 * F)
    expect(paintUs).toBe(2 * F)
    expect(lastIdx).toBe(3)
  })

  it('feeds the future reference the covering frame depends on', () => {
    // The P at display 3 (decode index 1) sits BEFORE the covering B frames in
    // decode order, so a chain that stops at the covering frame necessarily
    // includes it. Decoding it is required; painting it is the bug.
    const { startIdx, lastIdx } = frameChainFor(BFRAMES, B_KEYS, 1.5 * F)
    expect(startIdx).toBe(0)
    expect(lastIdx).toBe(2)          // the B at display 1
    expect(BFRAMES.slice(startIdx, lastIdx + 1).map((s) => s.cts / F))
      .toEqual([0, 3, 1])            // …and the P at display 3 is inside the span
  })

  it('walks into the second GOP for a later target', () => {
    const { paintUs, lastIdx } = frameChainFor(BFRAMES, B_KEYS, 5.9 * F)
    expect(paintUs).toBe(5 * F)
    expect(lastIdx).toBe(6)
  })

  it('a step across a reordered boundary still advances by one frame', () => {
    for (let d = 0; d < 6; d++) {
      const here = frameChainFor(BFRAMES, B_KEYS, d * F).paintUs
      const next = frameChainFor(BFRAMES, B_KEYS, (d + 1) * F).paintUs
      expect(next - here).toBe(F)
    }
  })

  it('never picks a frame that displays AFTER the target', () => {
    for (let i = 0; i <= 60; i++) {
      const target = (i / 10) * F
      const { paintUs } = frameChainFor(BFRAMES, B_KEYS, target)
      expect(paintUs).toBeLessThanOrEqual(target + 1)
    }
  })
})

describe('frameChainFor — scan bounds', () => {
  it('stops scanning shortly past the target instead of walking the file', () => {
    // 100k samples; a correct implementation looks at a handful. If this ever
    // becomes O(n) a paused drag turns into a per-move full-table scan.
    const many: WalkSample[] = Array.from({ length: 100_000 }, (_, i) => ({
      cts: i * F, duration: F, is_sync: i % 60 === 0,
    }))
    const keys = many.map((_, i) => i).filter((i) => i % 60 === 0)
    const t0 = performance.now()
    const { paintUs } = frameChainFor(many, keys, 100 * F)
    expect(paintUs).toBe(100 * F)
    expect(performance.now() - t0).toBeLessThan(50)
  })

  it('tolerates a zero duration without collapsing the reorder window', () => {
    const noDur: WalkSample[] = [
      { cts: 0, duration: 0, is_sync: true },
      { cts: 3 * F, duration: 0, is_sync: false },
      { cts: 1 * F, duration: 0, is_sync: false },
      { cts: 2 * F, duration: 0, is_sync: false },
    ]
    expect(frameChainFor(noDur, [0], 2 * F).paintUs).toBe(2 * F)
  })
})

// `gop` is exercised indirectly above; keep a direct check so the helper itself
// can't drift and silently weaken every case built on it.
describe('test helper', () => {
  it('builds decode-order samples with display-order cts', () => {
    expect(gop([0, 3, 1, 2]).map((s) => s.cts / F)).toEqual([0, 3, 1, 2])
  })
})
