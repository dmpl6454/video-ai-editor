import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  SAFE_ZONES, SAFE_ZONE_MODES, NOT_916_HINT,
  isSafeZoneMode, isVertical916, zonesFor, toPx, readStoredMode, writeStoredMode,
  type SafePlatform,
} from './safeZones'

const PLATFORMS = Object.keys(SAFE_ZONES) as SafePlatform[]
const V916 = { w: 1080, h: 1920 }
const H169 = { w: 1920, h: 1080 }

describe('SAFE_ZONES data', () => {
  it('describes three zones per platform, each with a unique id', () => {
    for (const p of PLATFORMS) {
      const zones = SAFE_ZONES[p].zones
      expect(zones).toHaveLength(3)
      expect(new Set(zones.map((z) => z.id)).size).toBe(3)
      expect(SAFE_ZONES[p].id).toBe(p)
    }
  })

  it('keeps every rect inside the unit square', () => {
    for (const p of PLATFORMS) {
      for (const { rect } of SAFE_ZONES[p].zones) {
        expect(rect.x).toBeGreaterThanOrEqual(0)
        expect(rect.y).toBeGreaterThanOrEqual(0)
        expect(rect.w).toBeGreaterThan(0)
        expect(rect.h).toBeGreaterThan(0)
        expect(rect.x + rect.w).toBeLessThanOrEqual(1 + 1e-9)
        expect(rect.y + rect.h).toBeLessThanOrEqual(1 + 1e-9)
      }
    }
  })

  it('cites a source URL and an ISO check date per platform', () => {
    for (const p of PLATFORMS) {
      expect(SAFE_ZONES[p].source).toMatch(/^https:\/\//)
      expect(SAFE_ZONES[p].fetched).toMatch(/^\d{4}-\d{2}-\d{2}$/)
    }
  })

  it('lists off first, then every platform, in the toggle order', () => {
    expect(SAFE_ZONE_MODES).toEqual(['off', 'tiktok', 'reels', 'shorts'])
    for (const m of SAFE_ZONE_MODES) expect(isSafeZoneMode(m)).toBe(true)
    expect(isSafeZoneMode('instagram')).toBe(false)
    expect(isSafeZoneMode(null)).toBe(false)
  })
})

describe('isVertical916', () => {
  it('accepts the 9:16 canvases the platform presets produce', () => {
    expect(isVertical916(V916)).toBe(true)
    expect(isVertical916({ w: 720, h: 1280 })).toBe(true)
  })

  it('rejects landscape, square and 4:5', () => {
    expect(isVertical916(H169)).toBe(false)
    expect(isVertical916({ w: 1080, h: 1080 })).toBe(false)
    expect(isVertical916({ w: 1080, h: 1350 })).toBe(false)
  })

  it('never divides by a zero-height canvas', () => {
    expect(isVertical916({ w: 0, h: 0 })).toBe(false)
  })
})

describe('zonesFor', () => {
  it('draws nothing when off, with no hint', () => {
    expect(zonesFor('off', V916)).toEqual({ zones: [], hint: null })
    expect(zonesFor('off', H169)).toEqual({ zones: [], hint: null })
  })

  it('returns the platform zones on a 9:16 canvas', () => {
    const { zones, hint } = zonesFor('tiktok', V916)
    expect(zones).toBe(SAFE_ZONES.tiktok.zones)
    expect(hint).toBeNull()
  })

  it('returns no zones but a 9:16 hint on a landscape canvas', () => {
    const { zones, hint } = zonesFor('tiktok', H169)
    expect(zones).toEqual([])
    expect(hint).toContain('9:16')
    expect(hint).toBe(NOT_916_HINT)
  })
})

describe('toPx', () => {
  it('scales a normalised rect into the preview box', () => {
    const px = toPx({ x: 0.86, y: 0.42, w: 0.14, h: 0.40 }, 405, 720)
    expect(px.left).toBeCloseTo(348.3, 6)
    expect(px.top).toBeCloseTo(302.4, 6)
    expect(px.width).toBeCloseTo(56.7, 6)
    expect(px.height).toBeCloseTo(288, 6)
  })

  it('maps the full frame to the full box', () => {
    expect(toPx({ x: 0, y: 0, w: 1, h: 1 }, 405, 720)).toEqual({ left: 0, top: 0, width: 405, height: 720 })
  })
})

describe('stored mode', () => {
  afterEach(() => { vi.unstubAllGlobals() })

  it('defaults to off when there is no localStorage at all', () => {
    // Newer Node versions ship a `localStorage` global of their own, so the
    // "no storage" case is stubbed rather than assumed from the runtime.
    vi.stubGlobal('localStorage', undefined)
    expect(readStoredMode()).toBe('off')
  })

  it('defaults to off on whatever storage this runtime provides', () => {
    expect(readStoredMode()).toBe('off')
  })

  it('defaults to off on a garbage or foreign value', () => {
    vi.stubGlobal('localStorage', { getItem: () => 'instagram' })
    expect(readStoredMode()).toBe('off')
    vi.stubGlobal('localStorage', { getItem: () => null })
    expect(readStoredMode()).toBe('off')
  })

  it('defaults to off when storage throws', () => {
    vi.stubGlobal('localStorage', { getItem: () => { throw new Error('SecurityError') } })
    expect(readStoredMode()).toBe('off')
  })

  it('round-trips a valid mode through the vai.safeZones key', () => {
    const bag = new Map<string, string>()
    vi.stubGlobal('localStorage', {
      getItem: (k: string) => bag.get(k) ?? null,
      setItem: (k: string, v: string) => { bag.set(k, v) },
    })
    writeStoredMode('reels')
    expect(bag.get('vai.safeZones')).toBe('reels')
    expect(readStoredMode()).toBe('reels')
  })

  it('swallows a write failure instead of breaking the toggle', () => {
    vi.stubGlobal('localStorage', { setItem: () => { throw new Error('QuotaExceededError') } })
    expect(() => writeStoredMode('shorts')).not.toThrow()
  })
})
