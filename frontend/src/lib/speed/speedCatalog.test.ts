// The curve editor's range and point budget come from the server's catalog
// (GET /api/speed/presets), not a frontend copy (review RD2).
import { afterEach, describe, expect, it, vi } from 'vitest'
import { loadSpeedCatalog, resetSpeedCatalogForTests } from './speedCatalog'
import { DEFAULT_CURVE_LIMITS, addPoint, clampSpeed, curveLimits, limitsFromCatalog, setCurveLimits, yToSpeed } from './curveMath'

const CATALOG = {
  presets: [{ id: 'hero', label: 'Hero', hint: '', menu: true, points: [[0, 1], [1, 1]] }],
  custom: 'custom', curve_range: [0.2, 5], constant_range: [0.1, 100], max_points: 4,
  freeze_default: 3, freeze_range: [0.1, 60],
}

afterEach(() => {
  setCurveLimits(null)
  resetSpeedCatalogForTests()
  vi.unstubAllGlobals()
})

describe('curve limits from the catalog', () => {
  it('a loaded catalog installs its curve_range and max_points', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200, json: async () => CATALOG })))
    await loadSpeedCatalog()
    expect(curveLimits()).toEqual({ min: 0.2, max: 5, maxPoints: 4 })
    expect(clampSpeed(10)).toBe(5)
    expect(clampSpeed(0.1)).toBe(0.2)
    expect(yToSpeed(-1)).toBe(5)
    const four: Array<readonly [number, number]> = [[0, 1], [0.3, 1], [0.6, 1], [1, 1]]
    expect(addPoint(four, 0.45)).toBeNull()                 // the server's budget: 4 points
    expect(addPoint(four.slice(1), 0.45)).not.toBeNull()
  })

  it('an unusable answer keeps the defaults', () => {
    expect(limitsFromCatalog({ curve_range: [5, 1], max_points: 4 })).toBeNull()
    expect(limitsFromCatalog({ curve_range: [0.1, 10], max_points: 1 })).toBeNull()
    expect(limitsFromCatalog({})).toBeNull()
    setCurveLimits(limitsFromCatalog({}))
    expect(curveLimits()).toBe(DEFAULT_CURVE_LIMITS)
  })
})
