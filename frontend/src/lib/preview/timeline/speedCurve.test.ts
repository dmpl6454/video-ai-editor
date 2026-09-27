// speedCurve.ts against edl/speed_curve.py (Wave D S1). The frame-exact
// proof is the `speed` group of the frame-map goldens (programMap.test.ts:
// decoded ffmpeg renders at five rates); this pins the arithmetic itself to
// the Python doubles and the helpers the engine and lane S2 use.
import { describe, expect, it } from 'vitest'
import {
  anchoredTicks, cRound, curveMap, curvePoints, curveRetimer, curveSeek, curveTimeBase, meanSpeed, outSeconds,
  sourceSeconds, speedAt,
} from './speedCurve'
import { clipSpeedFactor, effectiveDuration, clipFrames, sourceOffsetAt, speedFactor } from './framePlan'
import { freezeFrame, freezeInFor, forwardClipFrames, type SourceInfo } from './frameMap'
import { frameOf } from './timebase'

const BULLET = [[0, 3], [0.3, 3], [0.4, 0.3], [0.6, 0.3], [0.7, 3], [1, 3]] as const
const HERO = [[0, 1], [0.3, 1], [0.42, 0.25], [0.58, 0.25], [0.7, 1], [1, 1]] as const

describe('speed curves', () => {
  it('reproduce the Python doubles bit for bit', () => {
    const cm = curveMap(BULLET, 5.3)!
    // repr() of edl/speed_curve.py on the same inputs
    expect(cm.D).toBe(2.4200913242009126)
    expect(outSeconds(cm, 1.234567)).toBe(0.4115223333333333)
    expect(sourceSeconds(cm, 0.777)).toBe(2.3165064212845694)
    expect(outSeconds(cm, 9.0)).toBe(3.653424657534247)
    expect(effectiveDuration({ id: 'h', src: 'a', in: 1.25, out: 7.5, speed: { curve: HERO } }))
      .toBe(7.911392405063291)
  })

  it('out and source seconds are each other’s inverse; speed is the slope', () => {
    const cm = curveMap(HERO, 6)!
    for (const t of [0, 0.3, 1.7, 2.9, 4.4, cm.D - 1e-6]) {
      expect(outSeconds(cm, sourceSeconds(cm, t))).toBeCloseTo(t, 9)
      const h = 1e-6
      expect((sourceSeconds(cm, t + h) - sourceSeconds(cm, t)) / h).toBeCloseTo(speedAt(cm, t), 4)
    }
    expect(sourceSeconds(cm, cm.D)).toBeCloseTo(6, 9)
  })

  it('the footprint is the integral; a freeze is its hold; the factor is the mean', () => {
    const c = { id: 'c', src: 'a', in: 0, out: 6, speed: { curve: BULLET } }
    const m = meanSpeed(BULLET)
    expect(effectiveDuration(c)).toBe(6 / m)
    expect(speedFactor(c.speed)).toBe(m)
    expect(clipFrames(c, 30)).toBe(frameOf(6 / m, 30))
    const fz = { id: 'f', src: 'a', in: 2, out: 2 + 1 / 30, freeze: 1.5 }
    expect(effectiveDuration(fz)).toBe(1.5)
    expect(clipSpeedFactor(fz)).toBeCloseTo((1 / 30) / 1.5, 12)
    expect(curvePoints(2)).toBeNull()
    expect(curvePoints({ curve: [] })).toBeNull()
  })

  it('the retimer truncates like D2TS on the stream time base', () => {
    const cm = curveMap(BULLET, 4)!
    const tb = { num: 1, den: 15360 }
    const r = curveRetimer(cm, tb)
    for (let i = 0; i < 120; i++) {
      const x = i * 512
      expect(r(x)).toBe(Math.trunc(outSeconds(cm, x * (1 / 15360)) / (1 / 15360)))
    }
  })
})

describe('freeze frames', () => {
  const src: SourceInfo = { rate: { num: 25, den: 1 }, tb: { num: 1, den: 12800 }, frames: 400, startTicks: 0, w: 320, h: 180 }
  it('hold the first frame a 1x chain at `in` shows, and freezeInFor inverts it', () => {
    for (const fps of [30, 25, 60000 / 1001] as const) {
      for (const f of [1, 2, 3, 57, 200, 399]) {
        const t = freezeInFor(src, f, fps)
        expect(freezeFrame(src, t, fps)).toBe(f)
        expect(forwardClipFrames(src, { in: t, out: t + 1 }, 1, fps)[0]).toBe(f)
      }
    }
  })
})

// Wave D3 (E1b): the v1 chain's in-anchored clock — `edl/speed_curve.py`
// "the v1 chain's clock". Values are the Python doubles (repr).
describe('the in-anchored curve clock', () => {
  it('reproduces the Python helpers bit for bit', () => {
    expect([0.5, -0.5, 2.5, -2.5, 0.49999999999999994, 1.4999999999999998].map(cRound))
      .toEqual([1, -1, 3, -3, 0, 1])
    expect([0, 0.7, 0.71, 12.34].map(curveSeek)).toEqual([0, 0, 0.2, 11.8])
    expect(curveTimeBase({ num: 1, den: 15360 }, 30000)).toEqual({ num: 1, den: 1920000 })
    expect(curveTimeBase({ num: 1, den: 90000 }, 60000)).toEqual({ num: 1, den: 180000 })
    const cm = curveMap(HERO, 8)!
    const inS = 0.5428571428571428
    expect([0, 7680, 12345, 77777, -300].map((p) => anchoredTicks(p, 1 / 76800, inS, cm)))
      .toEqual([-41691, -34011, -29346, 36085, -41991])
    expect(outSeconds(cm, -0.01)).toBe(-0.01)
  })

  it('sourceOffsetAt is Clip.source_offset_at (a curve\'s integral, speed, a freeze)', () => {
    const c = { id: 'a', src: 'a', in: 0, out: 10, speed: { curve: HERO } }
    expect(effectiveDuration(c)).toBe(12.658227848101266)
    expect([0, 3, 6, 9.5, 12].map((t) => sourceOffsetAt(c, t)))
      .toEqual([0, 3, 4.917721518987341, 6.841772151898734, 9.341772151898734])
    expect(sourceOffsetAt({ id: 'b', src: 'a', in: 0, out: 10, speed: 2 }, 1.5)).toBe(3)
    expect(sourceOffsetAt({ id: 'f', src: 'a', in: 2, out: 2.04, freeze: 1 }, 0.5)).toBe(0)
  })

  it('a split piece continues its parent curve frame for frame', () => {
    // dispatch split_at of a Hero clip (in 0.4 + 1/7, 8 s, 25p) at frame 101:
    // the two pieces the Python op wrote.
    const src: SourceInfo = { rate: { num: 30, den: 1 }, tb: { num: 1, den: 15360 }, frames: 600, startTicks: 0, w: 320, h: 180 }
    const whole = { id: 'w', src: 'a', in: 0.4 + 1 / 7, out: 8.4 + 1 / 7, speed: { curve: HERO } }
    const left = { id: 'l', src: 'a', in: 0.5428571428571429, out: 4.273012127034358,
      speed: { curve: [[0.0, 1.0], [0.7519739315703722, 1.0], [1.0, 0.3815624999999997]] as [number, number][] } }
    const right = { id: 'r', src: 'a', in: 4.273012127034358, out: 8.542857142857143,
      speed: { curve: [[0.0, 0.3815624999999997], [0.03502204475501199, 0.25], [0.30122285999500864, 0.25],
        [0.5008734714250062, 1.0], [1.0, 1.0]] as [number, number][] } }
    const n = clipFrames(whole, 25)
    expect(clipFrames(left, 25)).toBe(101)
    expect(clipFrames(left, 25) + clipFrames(right, 25)).toBe(n)
    const all = Array.from(forwardClipFrames(src, whole, n, 25))
    const pieces = [...forwardClipFrames(src, left, 101, 25), ...forwardClipFrames(src, right, n - 101, 25)]
    expect(pieces).toEqual(all)
  })
})
