// QA-015: the preview's text layout is pinned to the SAME contract fixture the
// export is (tests/test_text_layout_contract.py). If either side drifts, one
// of the two suites fails.
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import {
  EMOJI_BOX_RATIO, EMOJI_INK_RATIO, LINE_HEIGHT_RATIO, SHADOW_ALPHA, SHADOW_OFFSET,
  TEXT_ROLES, WRAP_WIDTH_RATIO, baselineFor, isAnimated, lineCenters, outlineLineWidth,
  pickScript, roleAnchorY, scaleRotationAt, staticValue,
} from './textLayout'

const CASES = JSON.parse(readFileSync(fileURLToPath(
  new URL('./__fixtures__/text_layout_cases.json', import.meta.url)), 'utf-8')) as {
  LINE_HEIGHT_RATIO: number; SHADOW_OFFSET: number[]; SHADOW_ALPHA: number
  WRAP_WIDTH_RATIO: number; EMOJI_BOX_RATIO: number; EMOJI_INK_RATIO: number
  roles: Record<string, { size: number; stroke_w: number; fill: number[]; stroke: number[];
    shadow: boolean; upper: boolean; script_weight: number }>
  anchors: { role: string; w: number; h: number; y: number }[]
  lines: { anchor_y: number; n: number; size: number; centers: number[] }[]
}

describe('shared text layout contract (QA-015)', () => {
  it('constants match the export', () => {
    expect(LINE_HEIGHT_RATIO).toBe(CASES.LINE_HEIGHT_RATIO)
    expect([...SHADOW_OFFSET]).toEqual(CASES.SHADOW_OFFSET)
    expect(SHADOW_ALPHA).toBe(CASES.SHADOW_ALPHA)
    expect(WRAP_WIDTH_RATIO).toBe(CASES.WRAP_WIDTH_RATIO)
    expect(EMOJI_BOX_RATIO).toBe(CASES.EMOJI_BOX_RATIO)
    expect(EMOJI_INK_RATIO).toBe(CASES.EMOJI_INK_RATIO)
  })

  it('role table matches the export role table', () => {
    expect(Object.keys(TEXT_ROLES).sort()).toEqual(Object.keys(CASES.roles).sort())
    for (const [role, want] of Object.entries(CASES.roles)) {
      const got = TEXT_ROLES[role]
      expect(got.size).toBe(want.size)
      expect(got.strokeW).toBe(want.stroke_w)
      expect([...got.fill]).toEqual(want.fill)
      expect([...got.stroke]).toEqual(want.stroke)
      expect(got.shadow).toBe(want.shadow)
      expect(got.upper).toBe(want.upper)
      expect(got.scriptWeight).toBe(want.script_weight)
    }
  })

  it('role anchors are the export anchors', () => {
    for (const a of CASES.anchors) expect(roleAnchorY(a.role, a.h, a.w)).toBeCloseTo(a.y, 6)
  })

  it('line centres are the export line centres', () => {
    for (const l of CASES.lines) {
      const got = lineCenters(l.anchor_y, l.n, l.size)
      got.forEach((v, i) => expect(v).toBeCloseTo(l.centers[i], 6))
    }
  })

  it('baseline centres the cap band; outline is 2x so stroke_w shows outside', () => {
    // 'H' ink spans [baseline - asc, baseline + desc]; its middle must be `center`.
    const b = baselineFor(540, 88, 0)
    expect((b - 88 + b + 0) / 2).toBeCloseTo(540, 9)
    expect(outlineLineWidth(5)).toBe(10)
  })

  it('picks the same script fallback as _pick_script_font', () => {
    expect(pickScript('नमस्ते दुनिया')).toBe('deva')
    expect(pickScript('مرحبا بالعالم')).toBe('arab')
    expect(pickScript('HELLO नमस्ते')).toBe('deva')      // 6 Devanagari codepoints vs 5 letters
    expect(pickScript('HELLO WORLD नम')).toBe(null)      // Latin-dominant
    expect(pickScript('नमस्ते hi')).toBe('deva')
    expect(pickScript('HELLO')).toBe(null)
  })

  it('text transforms: static, 1-key, animated, and captions ignore them', () => {
    expect(scaleRotationAt({ scale: 1.5, rotation: 30 }, 'default', 0)).toEqual({ scale: 1.5, rotation: 30 })
    expect(scaleRotationAt({ scale: 1.5, rotation: 30 }, 'caption', 0)).toEqual({ scale: 1, rotation: 0 })
    const kf = { keyframes: [[0, 0.5], [2, 1.5]] as [number, number][] }
    expect(scaleRotationAt({ scale: kf }, 'default', 1).scale).toBeCloseTo(1.0, 9)
    expect(isAnimated(kf)).toBe(true)
    expect(staticValue({ keyframes: [[0, 300]] })).toBe(300)
    expect(staticValue(kf)).toBe(null)
  })
})
