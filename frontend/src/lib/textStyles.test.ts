import { describe, expect, it } from 'vitest'
import { TEXT_STYLE_PRESETS, textStyleArgs } from './textStyles'

describe('text style gallery (QA-078)', () => {
  it('offers more than the four old presets, each ONE add_text that never replaces an overlay', () => {
    expect(TEXT_STYLE_PRESETS.length).toBeGreaterThanOrEqual(6)
    for (const p of TEXT_STYLE_PRESETS) {
      const a = textStyleArgs(p, '', 1, 4)
      expect(a).toMatchObject({ text: 'Your text', start: 1, end: 4, allow_stack: true })
      expect(a).not.toHaveProperty('x')
      expect(a).not.toHaveProperty('y')
    }
  })
  it('uses the new block style (box, alignment, spacing, shadow, animation length)', () => {
    const keys = new Set(TEXT_STYLE_PRESETS.flatMap((p) => Object.keys(p.args)))
    for (const k of ['background', 'align', 'line_spacing', 'shadow_on', 'anim_dur']) expect(keys.has(k), k).toBe(true)
  })
})
