// QA-025 / QA-027: export resolution labels and platform presets.
import { describe, expect, it } from 'vitest'
import {
  PLATFORM_SPECS, activePlatformPreset, defaultQuality, exportBody, exportDimensions,
  platformMenuCommand, platformPresetCommand, qualityOptions, resolutionOptions,
} from './exportOptions'

describe('exportDimensions mirrors compositor.export_dimensions', () => {
  // Same table as tests/test_a4_export_settings.py::test_named_resolution_is_the_short_side.
  it.each([
    [1080, 1920, 1080, [1080, 1920]],
    [1080, 1920, 720, [720, 1280]],
    [1080, 1920, 2160, [2160, 3840]],
    [1920, 1080, 1080, [1920, 1080]],
    [1920, 1080, 720, [1280, 720]],
    [1080, 1080, 720, [720, 720]],
    [1080, 1350, 1080, [1080, 1350]],
    [1080, 1920, 0, [1080, 1920]],
  ])('%ix%i @ %i', (w, h, s, want) => {
    expect(exportDimensions(w, h, s)).toEqual(want)
  })
})

describe('resolutionOptions', () => {
  it('labels every option with the size the file will measure on a vertical canvas', () => {
    const labels = resolutionOptions({ w: 1080, h: 1920 }).map((o) => o.label)
    expect(labels).toContain('1080p (1080×1920)')
    expect(labels).toContain('720p (720×1280)')
    expect(labels[0]).toBe('Source (1080×1920)')
    expect(labels.join()).not.toMatch(/608/)
  })
})

describe('platform presets', () => {
  it('dispatch the full preset, not a canvas-only set_canvas', () => {
    const shorts = PLATFORM_SPECS.find((p) => p.label === 'Shorts')!
    expect(platformPresetCommand(shorts)).toEqual({ tool: 'apply_export_preset', args: { name: 'shorts' } })
    expect(shorts.lufs).toBe(-14)
  })
  it('the active preset is derived from the whole spec on the canvas', () => {
    expect(activePlatformPreset({ w: 1080, h: 1920, bitrate_kbps: 8000, loudness_lufs: -14 })?.preset).toBe('shorts')
    expect(activePlatformPreset({ w: 1080, h: 1920, bitrate_kbps: null, loudness_lufs: -16 })).toBeNull()
  })
})

describe('export body', () => {
  const preset = { w: 1080, h: 1920, bitrate_kbps: 8000, loudness_lufs: -16 }
  it('defaults to the platform target when the project carries one', () => {
    expect(defaultQuality(preset)).toBe('platform')
    expect(qualityOptions(preset)[0].label).toBe('Platform target (8 Mbps)')
    expect(exportBody(preset, 1080, 'platform')).toEqual({ height: 1080 })
  })
  it('an explicit quality pick overrides the preset target with bitrate_kbps: 0', () => {
    expect(exportBody(preset, 0, 23)).toEqual({ crf: 23, bitrate_kbps: 0 })
  })
  it('without a preset it is the old crf body, and Source sends no height', () => {
    expect(defaultQuality({ w: 1920, h: 1080 })).toBe(18)
    expect(exportBody({ w: 1920, h: 1080 }, 0, 18)).toEqual({ crf: 18 })
  })
})

describe('platformMenuCommand (what the Ratio menu dispatches)', () => {
  it('maps every toolbar preset to apply_export_preset', () => {
    for (const [label, name] of [['Reels', 'reels'], ['Shorts', 'shorts'], ['TikTok', 'tiktok'],
                                 ['IG 1:1', 'ig_feed_1x1'], ['IG 4:5', 'ig_feed_4x5']]) {
      expect(platformMenuCommand({ label, w: 1080, h: 1920, fps: 30 }))
        .toEqual({ tool: 'apply_export_preset', args: { name } })
    }
  })
  it('keeps set_canvas for a size it does not know', () => {
    expect(platformMenuCommand({ label: 'Custom', w: 720, h: 720, fps: 25 }))
      .toEqual({ tool: 'set_canvas', args: { w: 720, h: 720, fps: 25 } })
  })
})
