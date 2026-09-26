// QA-012: the Ratio menu checks the canvas' current aspect / platform preset.
// The old nine buttons carried no selected state at all.
import { describe, expect, it } from 'vitest'
import { PLATFORM_PRESETS, activeAspect, presetActive, ratioFacts, ratioLabel, ratioTriggerName, ratioValue } from './ratioMenu'

const preset = (label: string) => PLATFORM_PRESETS.find((p) => p.label === label)!

describe('activeAspect', () => {
  it('names the aspect a canvas is in, tolerating even-pixel rounding', () => {
    expect(activeAspect({ w: 1080, h: 1920, fps: 30 })).toBe('9:16')
    expect(activeAspect({ w: 640, h: 360, fps: 30 })).toBe('16:9')
    expect(activeAspect({ w: 1080, h: 1080, fps: 30 })).toBe('1:1')
    expect(activeAspect({ w: 1080, h: 1350, fps: 30 })).toBe('4:5')
    expect(activeAspect({ w: 1080, h: 1352, fps: 30 })).toBe('4:5')
  })

  it('is null for a custom size or no canvas', () => {
    expect(activeAspect({ w: 1000, h: 700, fps: 30 })).toBeNull()
    expect(activeAspect(null)).toBeNull()
    expect(activeAspect({ w: 0, h: 0, fps: 30 })).toBeNull()
  })
})

describe('presetActive', () => {
  // apply_export_preset sets size + bitrate + loudness and deliberately keeps
  // the project's frame rate (QA-009), so "which preset is in effect" is read
  // from those three — never from fps, and never from size alone.
  const applied = (w: number, h: number, bitrate_kbps: number, loudness_lufs: number, fps = 30) =>
    ({ w, h, fps, bitrate_kbps, loudness_lufs })

  it('checks the preset whose whole spec the canvas carries, at any project frame rate', () => {
    const reels25 = applied(1080, 1920, 8000, -16, 25)
    expect(PLATFORM_PRESETS.filter((p) => presetActive(p, reels25)).map((p) => p.label)).toEqual(['Reels', 'TikTok'])
    const shorts = applied(1080, 1920, 8000, -14, 29.97)
    expect(PLATFORM_PRESETS.filter((p) => presetActive(p, shorts)).map((p) => p.label)).toEqual(['Shorts'])
    expect(presetActive(preset('IG 4:5'), applied(1080, 1350, 6000, -16))).toBe(true)
  })

  it('checks no preset for a canvas that only has the size (an aspect switch, not a preset)', () => {
    const plain = { w: 1080, h: 1920, fps: 30, bitrate_kbps: null, loudness_lufs: -16 }
    expect(PLATFORM_PRESETS.filter((p) => presetActive(p, plain))).toEqual([])
    expect(presetActive(preset('Reels'), { w: 1080, h: 1920, fps: 30 })).toBe(false)
  })

  it('does not check a preset at another size', () => {
    expect(presetActive(preset('Reels'), applied(720, 1280, 8000, -16))).toBe(false)
    expect(presetActive(preset('IG 1:1'), applied(1080, 1920, 6000, -16))).toBe(false)
  })
})

describe('ratioLabel', () => {
  it('labels the trigger with the aspect, or the raw size', () => {
    expect(ratioLabel({ w: 1920, h: 1080, fps: 30 })).toBe('16:9')
    expect(ratioLabel({ w: 1000, h: 700, fps: 30 })).toBe('1000×700')
    expect(ratioLabel(null)).toBe('Ratio')
  })
})

// R3 (LEFT_RAIL_SPEC §2.10): the trigger shows the value, and its NAME always
// carries the facts the trigger shows only at density 0.
describe('the Ratio trigger (R3)', () => {
  const applied = (w: number, h: number, bitrate_kbps: number, loudness_lufs: number, fps = 30) =>
    ({ w, h, fps, bitrate_kbps, loudness_lufs })

  it('names the canvas with its value and facts', () => {
    expect(ratioTriggerName({ w: 1080, h: 1920, fps: 30 })).toBe('Canvas ratio: 9:16, 1080 by 1920, 30 fps')
    expect(ratioTriggerName({ w: 1920, h: 1080, fps: 29.97 })).toBe('Canvas ratio: 16:9, 1920 by 1080, 29.97 fps')
    expect(ratioTriggerName(null)).toBe('Canvas ratio')
  })

  it('shows the preset in effect when exactly one is', () => {
    expect(ratioValue(applied(1080, 1920, 8000, -14))).toBe('Shorts')
    expect(ratioValue(applied(1080, 1350, 6000, -16))).toBe('IG 4:5')
    expect(ratioTriggerName(applied(1080, 1350, 6000, -16, 25))).toBe('Canvas ratio: IG 4:5, 1080 by 1350, 25 fps')
  })

  it('falls back to the aspect when the spec is shared (Reels = TikTok) or no preset is in effect', () => {
    expect(ratioValue(applied(1080, 1920, 8000, -16))).toBe('9:16')
    expect(ratioValue({ w: 1080, h: 1920, fps: 30 })).toBe('9:16')
    expect(ratioValue({ w: 1000, h: 700, fps: 30 })).toBe('1000×700')
  })

  it('has the short facts line for density 0', () => {
    expect(ratioFacts({ w: 1080, h: 1920, fps: 30 })).toBe('1080×1920 · 30 fps')
    expect(ratioFacts({ w: 1920, h: 1080, fps: 23.976 })).toBe('1920×1080 · 23.976 fps')
  })
})
