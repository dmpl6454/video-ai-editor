import { describe, expect, it } from 'vitest'
import {
  CANVAS_BG_MODE, CANVAS_BG_MOVED_MODE, CANVAS_BG_ROTATED_MODE, MODE_APPROX, MODE_EXACT, PHASE_CAPS, canvasBgReason,
  clipFeatures,
} from './support'

// The CapCut Canvas background classes (wave E, F2), measured by
// tests/wk/test_canvas_bg_parity.py (which asserts this table).
describe('canvas background fidelity class', () => {
  const clip = (bg: unknown, fit = 'contain', rotation: unknown = 0) =>
    ({ src: 'a', in: 0, out: 1, start: 0, fit, canvas_bg: bg, transform: { rotation } }) as never

  it('adds no cause for no background, a cover fit, or the EXACT kinds', () => {
    expect(canvasBgReason(clip(null))).toBeNull()
    expect(canvasBgReason(clip({ type: 'blur', blur: 2 }, 'cover'))).toBeNull()
    expect(CANVAS_BG_MODE).toEqual({ color: MODE_EXACT, image: MODE_EXACT, blur: MODE_EXACT })
    for (const t of ['color', 'image', 'blur']) expect(canvasBgReason(clip({ type: t }))).toBeNull()
  })

  it('is APPROX on a rotated clip (static or keyed): the black corners meet the background', () => {
    expect(CANVAS_BG_ROTATED_MODE).toBe(MODE_APPROX)
    expect(canvasBgReason(clip({ type: 'color', color: '#FFFFFF' }, 'contain', 6)))
      .toEqual([MODE_APPROX, 'canvas:color:rotated'])
    expect(canvasBgReason(clip({ type: 'blur' }, 'contain', { keyframes: [[0, 0], [1, 10]] })))
      .toEqual([MODE_APPROX, 'canvas:blur:rotated'])
    expect(clipFeatures(clip({ type: 'image' }, 'contain', -8), PHASE_CAPS[1]))
      .toContainEqual([MODE_APPROX, 'canvas:image:rotated'])
    expect(canvasBgReason(clip({ type: 'image' }, 'cover', 6))).toBeNull()
  })

  it('is APPROX where a scaled-down, keyed or animated picture moves over the still background (review RE)', () => {
    expect(CANVAS_BG_MOVED_MODE).toBe(MODE_APPROX)
    const moved = (tx: Record<string, unknown>, extra: Record<string, unknown> = {}) =>
      ({ src: 'a', in: 0, out: 2, start: 0, canvas_bg: { type: 'blur', blur: 3 }, transform: tx, ...extra }) as never
    expect(canvasBgReason(moved({ scale: 0.7 }))).toEqual([MODE_APPROX, 'canvas:blur:moved'])
    expect(canvasBgReason(moved({ x: { keyframes: [[0, 0], [1, 40]] } }))).toEqual([MODE_APPROX, 'canvas:blur:moved'])
    expect(canvasBgReason(moved({}, { anim_in: 'zoom_in' }))).toEqual([MODE_APPROX, 'canvas:blur:moved'])
    // a static pan (measured 38.7-42.0 dB) and a fade-only animation stay EXACT
    expect(canvasBgReason(moved({ x: 60, y: -30 }))).toBeNull()
    expect(canvasBgReason(moved({}, { anim_in: 'fade_in' }))).toBeNull()
  })
})
