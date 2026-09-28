// clipAnim.ts reproduces the Python plan (edl/clip_animations.py) to the
// last digit that matters: clipAnimCases.json is the Python AnimPlan's own
// channel values, fade gains and blur weights (tests/gen_clip_anim_table.py;
// tests/test_clip_animations.py keeps it current).
import { describe, expect, it } from 'vitest'
import cases from './clipAnimCases.json'
import {
  ANIM_CHANNELS, ANIM_TABLE, animAt, animPeak, animValue, blurSigma, blurWeight, durationOf, fadeGain,
  hasAnimation, planOf, REST_POSE, type AnimFields,
} from './clipAnim'

interface Case {
  spec: AnimFields
  window: number
  d_in: number
  d_out: number
  peaks: Record<string, number>
  samples: number[][]
}
const all = (cases as unknown as { channels: string[]; cases: Case[] })

describe('clip animation plan parity with the Python table', () => {
  it('covers every preset of every kind', () => {
    const seen = new Set<string>()
    for (const c of all.cases) {
      if (c.spec.anim_in) seen.add(`in:${c.spec.anim_in}`)
      if (c.spec.anim_out) seen.add(`out:${c.spec.anim_out}`)
      if (c.spec.anim_combo) seen.add(`combo:${c.spec.anim_combo}`)
    }
    const want = [...ANIM_TABLE.in.map((p) => `in:${p.id}`), ...ANIM_TABLE.out.map((p) => `out:${p.id}`),
      ...ANIM_TABLE.combo.map((p) => `combo:${p.id}`)]
    expect([...seen].sort()).toEqual(want.sort())
    expect(all.channels).toEqual([...ANIM_CHANNELS])
  })

  it('durations, peaks, channels, fades and blur match at every sample', () => {
    let n = 0
    for (const c of all.cases) {
      const pl = planOf(c.spec, c.window)!
      expect(pl.dIn).toBeCloseTo(c.d_in, 12)
      expect(pl.dOut).toBeCloseTo(c.d_out, 12)
      for (const ch of ANIM_CHANNELS) expect(animPeak(pl, ch)).toBeCloseTo(c.peaks[ch], 7)
      for (const [t, scale, x, y, rot, gain, blur] of c.samples) {
        expect(animValue(pl, 'scale', t)).toBeCloseTo(scale, 7)
        expect(animValue(pl, 'x', t)).toBeCloseTo(x, 7)
        expect(animValue(pl, 'y', t)).toBeCloseTo(y, 7)
        expect(animValue(pl, 'rotation', t)).toBeCloseTo(rot, 6)
        expect(fadeGain(pl, t)).toBeCloseTo(gain, 7)
        expect(blurWeight(pl, t)).toBeCloseTo(blur, 7)
        n++
      }
    }
    expect(n).toBeGreaterThan(2000)
  })
})

describe('clip animation basics', () => {
  it('a clip without an animation is the rest pose', () => {
    expect(hasAnimation({})).toBe(false)
    expect(planOf({}, 2)).toBeNull()
    expect(animAt({ anim_in: null }, 0.3, 2)).toEqual(REST_POSE)
  })

  it('an unknown name animates nothing (the model drops it too)', () => {
    expect(planOf({ anim_in: 'wobble' }, 2)).toBeNull()
  })

  it('a combo excludes in and out', () => {
    const pl = planOf({ anim_in: 'zoom_in', anim_out: 'fade_out', anim_combo: 'rock' }, 2)!
    expect(pl.dIn).toBe(0)
    expect(pl.fadeOut).toBeNull()
    expect(pl.waves.rotation).toBeTruthy()
  })

  it('caps each side at 40 % of the clip, so in and out never overlap', () => {
    expect(durationOf(3, 1)).toBeCloseTo(0.4, 12)
    expect(durationOf(null, 10)).toBe(ANIM_TABLE.dur_default)
    const pl = planOf({ anim_in: 'fade_in', anim_out: 'fade_out', anim_dur: 3, anim_out_dur: 3 }, 1)!
    expect(pl.fadeIn![0] + pl.fadeIn![1]).toBeLessThanOrEqual(pl.fadeOut![0] + 1e-12)
  })

  it('slides travel a whole canvas and end at rest', () => {
    const pl = planOf({ anim_in: 'slide_left', anim_out: 'slide_down' }, 2)!
    expect(animValue(pl, 'x', 0)).toBe(1)
    expect(animValue(pl, 'x', 0.5)).toBe(0)
    expect(animValue(pl, 'y', 2)).toBe(1)
  })

  it('blur sigma is 2 % of the shorter side', () => {
    expect(blurSigma(640, 360)).toBeCloseTo(7.2, 9)
    expect(blurSigma(1080, 1920)).toBeCloseTo(21.6, 9)
  })
})
