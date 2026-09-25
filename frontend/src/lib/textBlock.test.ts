// QA-078 / QA-075: rule 7 of the shared text layout model — alignment inside
// the block, the background box, line spacing, caption positions and the
// animation length — pinned to the SAME fixture the export side asserts
// (tests/test_b3_panels.py::test_text_block_rules_match_the_contract).
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import {
  ANIM_DUR, ANIM_DUR_RANGE, BG_PAD_X_RATIO, BG_PAD_Y_RATIO, BG_RADIUS_RATIO, animDuration, backgroundRect,
  captionAnchorY, lineCenters, lineX, offAxisSentinel, TEXT_AXIS_SENTINEL,
} from './textLayout'
import { animEnvelope } from './textAnim'

const B = (JSON.parse(readFileSync(fileURLToPath(
  new URL('./__fixtures__/text_layout_cases.json', import.meta.url)), 'utf-8')) as { block: {
  BG_PAD_X_RATIO: number; BG_PAD_Y_RATIO: number; BG_RADIUS_RATIO: number; ANIM_DUR: number; ANIM_DUR_RANGE: number[]
  line_x: { align: string; anchor_x: number; block_w: number; w: number; x: number }[]
  background: { anchor_x: number; centers: number[]; block_w: number; size: number; spacing: number; rect: number[] }[]
  spaced_lines: { anchor_y: number; n: number; size: number; spacing: number; centers: number[] }[]
  caption_positions: { position: string; w: number; h: number; y: number }[]
} }).block

describe('text block rules match the export (QA-078/075)', () => {
  it('constants', () => {
    expect([BG_PAD_X_RATIO, BG_PAD_Y_RATIO, BG_RADIUS_RATIO, ANIM_DUR]).toEqual([B.BG_PAD_X_RATIO, B.BG_PAD_Y_RATIO, B.BG_RADIUS_RATIO, B.ANIM_DUR])
    expect([...ANIM_DUR_RANGE]).toEqual(B.ANIM_DUR_RANGE)
  })
  it('alignment inside the block', () => {
    for (const c of B.line_x) expect(lineX(c.align, c.anchor_x, c.block_w, c.w), JSON.stringify(c)).toBeCloseTo(c.x, 9)
  })
  it('the background box', () => {
    for (const c of B.background) {
      const r = backgroundRect(c.anchor_x, c.centers, c.block_w, c.size, c.spacing)
      r.forEach((v, i) => expect(v).toBeCloseTo(c.rect[i], 9))
    }
  })
  it('line spacing', () => {
    for (const c of B.spaced_lines) {
      lineCenters(c.anchor_y, c.n, c.size, c.spacing).forEach((v, i) => expect(v).toBeCloseTo(c.centers[i], 9))
    }
  })
  it('caption positions', () => {
    for (const c of B.caption_positions) expect(captionAnchorY(c.position, c.w, c.h), c.position).toBeCloseTo(c.y, 9)
  })
  it('a clip animation length is honoured, clamped and capped at 40 % of the window', () => {
    expect(animDuration(null, 10)).toBe(0.35)
    expect(animDuration(1, 10)).toBe(1)
    expect(animDuration(9, 100)).toBe(3)
    expect(animDuration(1, 1)).toBeCloseTo(0.4, 9)
    // halfway through a 1 s fade-in the preview is at half opacity
    expect(animEnvelope({ anim_in: 'fade', anim_dur: 1 }, 0.5, 1080, { start: 0, end: 10 }).alpha).toBeCloseTo(0.5, 9)
  })
})

describe('typed coordinates are honoured (QA-076)', () => {
  it('only an axis\'s schema default is "unset", and a typed one moves a pixel off it', () => {
    expect(offAxisSentinel(918, TEXT_AXIS_SENTINEL.y)).toBe(918)
    expect(offAxisSentinel(1700, TEXT_AXIS_SENTINEL.y)).toBe(1701)
    expect(offAxisSentinel(540, TEXT_AXIS_SENTINEL.x)).toBe(541)
    expect(offAxisSentinel(960, TEXT_AXIS_SENTINEL.x)).toBe(960)
  })
})
