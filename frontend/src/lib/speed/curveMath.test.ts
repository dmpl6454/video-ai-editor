import { describe, expect, it } from 'vitest'
import {
  MAX_POINTS, MIN_GAP, SPEED_MAX, SPEED_MIN, addPoint, canRemove, curveDuration, curvePath, formatSpeed,
  insertIndex, matchPreset, movePoint, normalizeForCommit, nudgePoint, pointLabel, removePoint, speedAtX,
  speedToY, widestGapMid, xBounds, yToSpeed, type Curve,
} from './curveMath'

const HERO: Curve = [[0, 1], [0.3, 1], [0.42, 0.25], [0.58, 0.25], [0.7, 1], [1, 1]]

describe('graph axes', () => {
  it('puts 1x in the middle of a log speed axis, 10x on top, 0.1x at the bottom', () => {
    expect(speedToY(1)).toBeCloseTo(0.5, 12)
    expect(speedToY(10)).toBeCloseTo(0, 12)
    expect(speedToY(0.1)).toBeCloseTo(1, 12)
    // 2x and 0.5x are equally far from normal.
    expect(0.5 - speedToY(2)).toBeCloseTo(speedToY(0.5) - 0.5, 12)
  })

  it('inverts and clamps', () => {
    for (const r of [0.1, 0.25, 1, 3.3, 10]) expect(yToSpeed(speedToY(r))).toBeCloseTo(r, 9)
    expect(yToSpeed(-1)).toBe(SPEED_MAX)
    expect(yToSpeed(2)).toBe(SPEED_MIN)
    expect(speedToY(50)).toBe(0)
  })
})

describe('moving a point', () => {
  it('keeps the ends pinned to the clip edges', () => {
    expect(xBounds(HERO, 0)).toEqual([0, 0])
    expect(movePoint(HERO, 0, 0.4, 3)[0]).toEqual([0, 3])
    expect(movePoint(HERO, 5, 0.2, 0.5)[5]).toEqual([1, 0.5])
  })

  it('never lets an inner point cross or touch a neighbour, and clamps speed', () => {
    const m = movePoint(HERO, 2, 0.9, 50)
    expect(m[2][0]).toBeCloseTo(0.58 - MIN_GAP, 12)
    expect(m[2][1]).toBe(SPEED_MAX)
    expect(movePoint(HERO, 2, 0.0, 0)[2]).toEqual([0.3 + MIN_GAP, SPEED_MIN])
  })

  it('returns a new curve and leaves the input alone', () => {
    const before = JSON.stringify(HERO)
    const m = movePoint(HERO, 1, 0.2, 2)
    expect(m).not.toBe(HERO)
    expect(JSON.stringify(HERO)).toBe(before)
  })
})

describe('keyboard', () => {
  it('moves by 1% (10% with Shift) of the clip', () => {
    expect(nudgePoint(HERO, 2, 'ArrowRight')[2][0]).toBeCloseTo(0.43, 12)
    expect(nudgePoint(HERO, 2, 'ArrowLeft', true)[2][0]).toBeCloseTo(0.32, 12) // clamped at 0.3 + gap
  })

  it('steps speed in log space and lands on shown values', () => {
    const up = nudgePoint(HERO, 1, 'ArrowUp')[1][1]
    expect(up).toBe(1.12)
    const down = nudgePoint(HERO, 1, 'ArrowDown', true)[1][1]
    expect(down).toBe(0.56)
    // At the floor a step still moves (0.1 → 0.11), and never below 0.1.
    const low: Curve = [[0, 0.1], [1, 0.1]]
    expect(nudgePoint(low, 0, 'ArrowUp')[0][1]).toBe(0.11)
    expect(nudgePoint(low, 0, 'ArrowDown')[0][1]).toBe(SPEED_MIN)
    const high: Curve = [[0, 10], [1, 10]]
    expect(nudgePoint(high, 1, 'ArrowUp')[1][1]).toBe(SPEED_MAX)
  })

  it('an end point does not move sideways', () => {
    expect(nudgePoint(HERO, 0, 'ArrowRight')[0]).toEqual([0, 1])
  })
})

describe('adding and removing', () => {
  it('adds ON the curve, so the shape does not change', () => {
    const r = addPoint(HERO, 0.5)!
    expect(r.index).toBe(3)
    expect(r.curve[3]).toEqual([0.5, 0.25])
    const r2 = addPoint(HERO, 0.36)!
    expect(r2.curve[r2.index][1]).toBe(Math.round(speedAtX(HERO, 0.36) * 100) / 100)
    expect(speedAtX(HERO, 0.36)).toBeCloseTo(0.625, 12)
  })

  it('refuses a point on top of another, outside the clip, or past the limit', () => {
    expect(insertIndex(HERO, 0.3 + MIN_GAP / 2)).toBe(-1)
    expect(insertIndex(HERO, 0)).toBe(-1)
    expect(insertIndex(HERO, 1.2)).toBe(-1)
    const full: Curve = Array.from({ length: MAX_POINTS }, (_, i) => [i / (MAX_POINTS - 1), 1] as const)
    expect(addPoint(full, 0.5)).toBeNull()
  })

  it('splits the widest gap when no position is given', () => {
    expect(widestGapMid([[0, 1], [1, 1]])).toBe(0.5)
    expect(widestGapMid(HERO)).toBe(0.85)
  })

  it('never removes an end or goes below two points', () => {
    expect(canRemove(HERO, 0)).toBe(false)
    expect(canRemove(HERO, 5)).toBe(false)
    expect(removePoint(HERO, 2)).toHaveLength(5)
    expect(removePoint([[0, 1], [1, 2]], 1)).toHaveLength(2)
  })
})

describe('duration, presets, output', () => {
  it('fills S / mean(speed) of timeline, the model footprint', () => {
    expect(curveDuration([[0, 1], [1, 1]], 6)).toBe(6)
    expect(curveDuration([[0, 1], [1, 2]], 6)).toBeCloseTo(4, 12)
    // Hero: mean = 0.3·1 + 0.12·0.625 + 0.16·0.25 + 0.12·0.625 + 0.3·1 = 0.79
    expect(curveDuration(HERO, 7.9)).toBeCloseTo(10, 9)
  })

  it('names a curve by an exact point match only', () => {
    const presets = [{ id: 'hero', points: HERO.map((p) => [...p]) }]
    expect(matchPreset(HERO, presets)?.id).toBe('hero')
    expect(matchPreset(movePoint(HERO, 2, 0.42, 0.3), presets)).toBeNull()
  })

  it('commits rounded values with the ends pinned', () => {
    expect(normalizeForCommit([[0.0004, 1.2345], [0.5004, 0.333], [0.9999, 10.4]]))
      .toEqual([[0, 1.23], [0.5, 0.33], [1, 10]])
  })

  it('draws straight segments on the log axis', () => {
    expect(curvePath([[0, 1], [1, 10]], 100, 50)).toBe('M0.00 25.00 L100.00 0.00')
  })

  it('reads points for assistive tech', () => {
    expect(formatSpeed(0.25)).toBe('0.25×')
    expect(formatSpeed(2)).toBe('2×')
    expect(formatSpeed(1.5)).toBe('1.5×')
    expect(pointLabel(HERO, 0)).toBe('Speed point 1 of 6, start, 1×')
    expect(pointLabel(HERO, 2)).toBe('Speed point 3 of 6, 42% through the clip, 0.25×')
  })
})
