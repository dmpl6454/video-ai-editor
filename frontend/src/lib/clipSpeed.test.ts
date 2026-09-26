// One implementation of Clip.speed_factor / effective_duration on the client
// (review RD2): types.ts delegates to the golden-pinned framePlan port, and
// the Inspector's Normal slider starts a curve clip at its MEAN speed.
import { describe, expect, it } from 'vitest'
import { clipDuration, clipSpeedFactor, normalSpeedOf, type AnyClip } from '../types'
import { clipSpeedFactor as planFactor, effectiveDuration, type EdlClip } from './preview/timeline/framePlan'

const base = { id: 'c', src: 's.mp4', in: 1, out: 11, start: 0, effects: [], transform: {} }
const CASES: Array<Record<string, unknown>> = [
  { ...base },
  { ...base, speed: 2 },
  { ...base, speed: 0.5 },
  { ...base, speed: 0 },
  { ...base, speed: -3 },
  { ...base, speed: { curve: [[0, 1], [0.759, 1], [1, 0.406]] } },
  { ...base, speed: { curve: [[0, 0.1], [0.5, 10], [1, 0.1]], name: 'custom' } },
  { ...base, freeze: 3 },
  { ...base, in: 4, out: 4, freeze: 2 },
]

describe('clip speed on the client is one implementation', () => {
  it('types.clipSpeedFactor / clipDuration equal framePlan (golden-pinned) for every kind of clip', () => {
    for (const c of CASES) {
      expect(clipSpeedFactor(c as unknown as AnyClip), JSON.stringify(c)).toBe(planFactor(c as EdlClip))
      expect(clipDuration(c as unknown as AnyClip), JSON.stringify(c)).toBe(effectiveDuration(c as EdlClip))
    }
  })
})

describe('normalSpeedOf: where the Normal slider starts', () => {
  it('is the curve\'s mean speed on a curve clip (review RD2: it read 1.00x)', () => {
    const c = CASES[5] as unknown as AnyClip
    const mean = planFactor(CASES[5] as EdlClip)
    expect(mean).toBeGreaterThan(0.9)
    expect(mean).toBeLessThan(0.95)
    expect(normalSpeedOf(c)).toBeCloseTo(mean, 12)
    // the clip's length is kept at the first step from there
    expect((10 / normalSpeedOf(c))).toBeCloseTo(clipDuration(c), 9)
  })
  it('is the scalar speed, 1 when unset, and within the slider range', () => {
    expect(normalSpeedOf(CASES[1] as unknown as AnyClip)).toBe(2)
    expect(normalSpeedOf(CASES[0] as unknown as AnyClip)).toBe(1)
    expect(normalSpeedOf({ ...base, speed: { curve: [[0, 10], [1, 10]] } } as unknown as AnyClip)).toBe(4)
  })
})
