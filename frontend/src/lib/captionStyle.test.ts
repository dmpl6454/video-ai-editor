import { describe, expect, it } from 'vitest'
import { CAPTION_PRESETS, captionLookOf } from './captionStyle'

describe('caption looks (QA-075)', () => {
  it('each preset sends the whole look, so presets replace rather than stack', () => {
    for (const p of CAPTION_PRESETS) {
      expect(Object.keys(p.args).sort()).toEqual(['background', 'color', 'font', 'shadow_on', 'size', 'stroke', 'stroke_w', 'upper'])
    }
    expect(CAPTION_PRESETS.find((p) => p.id === 'classic')!.args).toEqual(captionLookOf(null))
  })
  it('the current look is recognised as its preset', () => {
    const boxed = CAPTION_PRESETS.find((p) => p.id === 'boxed')!
    expect(boxed.matches(captionLookOf({ background: '#000000b3', stroke_w: 0, shadow_on: false }))).toBe(true)
    expect(boxed.matches(captionLookOf({ background: '#000000B3' }))).toBe(false)
    expect(CAPTION_PRESETS.find((p) => p.id === 'classic')!.matches(captionLookOf(undefined))).toBe(true)
  })
})
