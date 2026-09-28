import { describe, expect, it } from 'vitest'
import TABLE from './blendModes.json'
import {
  BLENDS, BLUR_DEFAULT, BLUR_LEVELS, LIVE_BLEND_FLAGS, SWATCHES, blendCss, blendLabel, blendLiveNote, blendOf,
  canvasBgOf, hexRgb, isWebKitCompositor, resetBlendSupport,
} from './catalog'

// The table is edl/canvas_blend.py's payload (pinned equal by
// tests/test_f2_canvas_blend.py::test_the_browser_fixture_matches_the_table).
describe('canvas / blend catalog', () => {
  it("names CapCut's 14 blend modes in its order", () => {
    expect(BLENDS.map((b) => b.label)).toEqual([
      'Normal', 'Darken', 'Multiply', 'Color Burn', 'Linear Burn', 'Lighten', 'Screen', 'Color Dodge',
      'Add', 'Overlay', 'Soft Light', 'Hard Light', 'Difference', 'Exclusion'])
    expect(BLENDS).toBe(TABLE.blends)
  })

  it('composites every mode with its CSS operator (Add and Linear Burn are Porter-Duff)', () => {
    expect(blendCss('multiply')).toBe('multiply')
    expect(blendCss('color_burn')).toBe('color-burn')
    expect(blendCss('soft_light')).toBe('soft-light')
    expect(blendCss('add')).toBe('plus-lighter')
    expect(blendCss('linear_burn')).toBe('plus-darker')
    expect(BLENDS.filter((b) => b.porter_duff).map((b) => b.id)).toEqual(['linear_burn', 'add'])
    expect(blendCss('nonsense')).toBe('normal')
  })

  it('reads a clip\'s blend and background defensively', () => {
    expect(blendOf({ blend: 'screen' })).toBe('screen')
    expect(blendOf({})).toBe('normal')
    expect(blendOf({ blend: 'sparkle' })).toBe('normal')
    expect(blendLabel('color_dodge')).toBe('Color Dodge')
    expect(canvasBgOf({ canvas_bg: { type: 'blur', blur: 3 } })).toEqual({ type: 'blur', blur: 3 })
    expect(canvasBgOf({ canvas_bg: null })).toBeNull()
    expect(canvasBgOf({ canvas_bg: { type: 'glitter' } })).toBeNull()
  })

  it('parses colours and knows the four blur strengths', () => {
    expect(hexRgb('#E53935')).toEqual([229, 57, 53])
    expect(hexRgb('00ff00')).toEqual([0, 255, 0])
    expect(hexRgb('red')).toEqual([0, 0, 0])
    expect(BLUR_LEVELS.map((b) => b.level)).toEqual([1, 2, 3, 4])
    expect(BLUR_DEFAULT).toBe(2)
    expect(SWATCHES[0]).toBe('#000000')
  })
})

describe('live blend notes (measured flags: tests/test_f2_blend_browser_parity.py)', () => {
  const withCss = (supported: (css: string) => boolean, fn: () => void) => {
    const g = globalThis as { CSS?: unknown }
    const old = g.CSS
    g.CSS = { supports: (_p: string, v: string) => supported(v) }
    resetBlendSupport()
    try { fn() } finally { g.CSS = old; resetBlendSupport() }
  }

  it('says so where Chromium cannot draw a mode (no plus-darker)', () => {
    withCss((v) => v !== 'plus-darker', () => {
      expect(blendLiveNote('linear_burn', 1)).toMatch(/cannot preview Linear Burn live/)
      expect(blendLiveNote('soft_light', 1)).toBeNull()
      expect(blendLiveNote('color_dodge', 0.5)).toBeNull()
      expect(isWebKitCompositor()).toBe(false)
    })
  })

  it("flags WebKit's Soft Light, and Dodge / Burn below full opacity", () => {
    withCss(() => true, () => {
      expect(isWebKitCompositor()).toBe(true)
      expect(blendLiveNote('linear_burn', 1)).toBeNull()
      expect(blendLiveNote('soft_light', 1)).toMatch(/approximates Soft Light/)
      expect(blendLiveNote('color_dodge', 1)).toBeNull()
      expect(blendLiveNote('color_dodge', 0.5)).toMatch(/Below full opacity/)
      expect(blendLiveNote('color_burn', null)).toMatch(/Below full opacity/)
      expect(blendLiveNote('multiply', 0.4)).toBeNull()
      expect(Object.keys(LIVE_BLEND_FLAGS.webkit).sort()).toEqual(['color_burn', 'color_dodge', 'soft_light'])
    })
  })
})
