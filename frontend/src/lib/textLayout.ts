// THE shared text layout model — the client half of render/text_overlay.py's
// "SHARED TEXT LAYOUT MODEL" block (QA-015). Read that comment for the why;
// this file is its mirror, and `__fixtures__/text_layout_cases.json` pins
// both sides to the same numbers (textLayout.test.ts here,
// tests/test_text_layout_contract.py there).
//
// Before this, TextLayer had its own role anchors (label at the TOP, 'lower'
// at 0.78 h), centred an em box with textBaseline 'middle' at 1.15 x size,
// stroked at a height-fraction lineWidth that showed only HALF outside the
// fill, and blurred its shadow — so preview text sat 20-50 px from where the
// export put it and the export outline looked twice as heavy.
//
// Pure functions only: TextLayer measures its own font metrics and feeds them
// in, exactly as the server measures Pillow's.

import { sampleKF, type KFNum } from './overlay'

export const LINE_HEIGHT_RATIO = 1.15
export const SHADOW_OFFSET: readonly [number, number] = [4, 6]
export const SHADOW_ALPHA = 140
export const WRAP_WIDTH_RATIO = 0.86
export const EMOJI_BOX_RATIO = 1.0
export const EMOJI_INK_RATIO = 0.92

export type RGBA = readonly [number, number, number, number]

export interface TextRoleStyle {
  /** CSS family declared in fonts.css for the role's bundled TTF. */
  font: string
  /** CSS weight of that TTF — the face's real weight, never a synthetic bold. */
  weight: string
  /** Font size in EDL-canvas px (ROLE_STYLES["size"]). */
  size: number
  /** Outline width in EDL-canvas px OUTSIDE the glyph (ROLE_STYLES["stroke_w"]). */
  strokeW: number
  fill: RGBA
  stroke: RGBA
  shadow: boolean
  upper: boolean
  /** `wght` for the Noto script fallbacks (SCRIPT_FONT_WEIGHT). */
  scriptWeight: number
}

const WHITE: RGBA = [255, 255, 255, 255]
const BLACK: RGBA = [0, 0, 0, 255]

/** Mirror of ROLE_STYLES + SCRIPT_FONT_WEIGHT in render/text_overlay.py. */
export const TEXT_ROLES: Record<string, TextRoleStyle> = {
  super:       { font: 'Anton',      weight: '400', size: 140, strokeW: 6, fill: WHITE, stroke: BLACK, shadow: true,  upper: true,  scriptWeight: 700 },
  hook:        { font: 'Bebas Neue', weight: '400', size: 170, strokeW: 7, fill: WHITE, stroke: BLACK, shadow: true,  upper: true,  scriptWeight: 700 },
  lower_third: { font: 'Montserrat', weight: '700', size: 56,  strokeW: 3, fill: WHITE, stroke: BLACK, shadow: true,  upper: false, scriptWeight: 700 },
  caption:     { font: 'Inter',      weight: '900', size: 64,  strokeW: 5, fill: WHITE, stroke: BLACK, shadow: true,  upper: false, scriptWeight: 900 },
  label:       { font: 'Inter',      weight: '700', size: 48,  strokeW: 3, fill: WHITE, stroke: BLACK, shadow: true,  upper: false, scriptWeight: 700 },
  watermark:   { font: 'Inter',      weight: '700', size: 32,  strokeW: 2, fill: [255, 255, 255, 200], stroke: [0, 0, 0, 140], shadow: false, upper: false, scriptWeight: 700 },
  default:     { font: 'Inter',      weight: '700', size: 64,  strokeW: 4, fill: WHITE, stroke: BLACK, shadow: true,  upper: false, scriptWeight: 700 },
}

export function roleStyle(role: string): TextRoleStyle {
  return TEXT_ROLES[role] ?? TEXT_ROLES.default
}

/** Rule 1: the role's own anchor y — `_y_for_role` with no override. */
export function roleAnchorY(role: string, canvasH: number, canvasW?: number): number {
  if (role === 'watermark') return canvasH - canvasH * 0.04
  if (role === 'hook') return canvasH * 0.5
  if (role === 'caption') return canvasW != null && canvasH > canvasW ? canvasH * 0.76 : canvasH - canvasH * 0.16
  if (role === 'lower_third') return canvasH - canvasH * 0.2
  return canvasH * 0.75
}

/** Rule 2: each line's cap-band centre, top to bottom. `spacing` is the
 *  clip's style.line_spacing (QA-078), a multiplier. */
export function lineCenters(anchorY: number, nLines: number, size: number, spacing = 1): number[] {
  const lh = size * LINE_HEIGHT_RATIO * spacing
  return Array.from({ length: nLines }, (_, i) => anchorY + (i - (nLines - 1) / 2) * lh)
}

// ---- rule 7 (QA-078 / QA-075): alignment, background box, caption position.
// Mirror of the block in render/text_overlay.py; `block` in the fixture pins both.
export const BG_PAD_X_RATIO = 0.3
export const BG_PAD_Y_RATIO = 0.08
export const BG_RADIUS_RATIO = 0.18
export const ANIM_DUR = 0.35
export const ANIM_DUR_RANGE: readonly [number, number] = [0.1, 3.0]

export type TextAlign = 'left' | 'center' | 'right'

/** Left edge of a line of width `w` inside a block of width `blockW` that is
 *  centred on `anchorX`. */
export function lineX(align: TextAlign | string | null | undefined, anchorX: number, blockW: number, w: number): number {
  if (align === 'left') return anchorX - blockW / 2
  if (align === 'right') return anchorX + blockW / 2 - w
  return anchorX - w / 2
}

/** [left, top, right, bottom, radius] of the background box. */
export function backgroundRect(anchorX: number, centers: number[], blockW: number, size: number, spacing = 1):
  [number, number, number, number, number] {
  const lh = size * LINE_HEIGHT_RATIO * spacing
  const px = BG_PAD_X_RATIO * size
  const py = BG_PAD_Y_RATIO * size
  return [anchorX - blockW / 2 - px, centers[0] - lh / 2 - py,
          anchorX + blockW / 2 + px, centers[centers.length - 1] + lh / 2 + py, BG_RADIUS_RATIO * size]
}

/** A caption cue's anchor y for its captions track's `config.position`
 *  (text_overlay.caption_position_y + block_anchor_y). */
export function captionAnchorY(position: string | null | undefined, canvasW: number, canvasH: number): number {
  if (position === 'center') return canvasH * 0.5
  if (position === 'top') return canvasH * (canvasH > canvasW ? 0.14 : 0.12)
  return roleAnchorY('caption', canvasH, canvasW)
}

/** Each animation's length: the clip's anim_dur (clamped) or ANIM_DUR, never
 *  more than 40 % of the on-screen window, never under 0.1 s. */
export function animDuration(animDur: number | null | undefined, windowLen: number): number {
  const base = typeof animDur === 'number' && Number.isFinite(animDur)
    ? Math.min(ANIM_DUR_RANGE[1], Math.max(ANIM_DUR_RANGE[0], animDur)) : ANIM_DUR
  return Math.min(base, Math.max(0.1, windowLen * 0.4))
}

/** Rule 3: the alphabetic baseline that centres the 'H' ink (ascent `asc`
 *  above, descent `desc` below the baseline) on `center`. */
export function baselineFor(center: number, asc: number, desc: number): number {
  return center + (asc - desc) / 2
}

/** Rule 4: canvas strokes are centred on the path and the fill covers the
 *  inner half, so a `strokeW`-px outline needs a 2 x strokeW line. */
export function outlineLineWidth(strokeW: number): number {
  return 2 * strokeW
}

export function rgbaCss([r, g, b, a]: RGBA): string {
  return `rgba(${r},${g},${b},${a / 255})`
}

/** Mirror of `_pick_script_font`: the dominant non-Latin script, or null. */
export function pickScript(text: string): 'deva' | 'arab' | 'cjk' | null {
  const counts = { deva: 0, arab: 0, cjk: 0, latin: 0 }
  for (const ch of text) {
    const cp = ch.codePointAt(0) ?? 0
    if (cp >= 0x0900 && cp <= 0x097F) counts.deva++
    else if ((cp >= 0x0600 && cp <= 0x06FF) || (cp >= 0x0750 && cp <= 0x077F)) counts.arab++
    else if ((cp >= 0x4E00 && cp <= 0x9FFF) || (cp >= 0x3000 && cp <= 0x30FF) || (cp >= 0x3400 && cp <= 0x4DBF)) counts.cjk++
    else if (/\p{L}/u.test(ch)) counts.latin++
  }
  // Python's max() keeps the FIRST of equal counts, in this insertion order.
  let best: keyof typeof counts = 'deva'
  for (const k of ['arab', 'cjk', 'latin'] as const) if (counts[k] > counts[best]) best = k
  if (counts[best] === 0 || best === 'latin') return null
  return best
}

/** CSS families for the bundled Noto fallbacks (fonts.css). CJK has none in
 *  the browser (the 17 MB SC face is not shipped to it), so it keeps the
 *  role family and the system CJK font — a known, documented gap. */
export const SCRIPT_FAMILY: Partial<Record<'deva' | 'arab' | 'cjk', string>> = {
  deva: 'Noto Sans Devanagari',
  arab: 'Noto Sans Arabic',
}

/** Per-RUN fallback families, in the canvas font list after the clip's own
 *  family — the export's `_SCRIPT_FALLBACK_FONTS` (text_overlay.py): a run
 *  the clip's font does not cover is drawn in the bundled Noto face, not a
 *  system Devanagari/Arabic font (a Latin-dominant caption's Hindi line). */
export const SCRIPT_FALLBACK_FAMILIES: readonly string[] = ['Noto Sans Devanagari', 'Noto Sans Arabic']

/** The canvas `font` family list for a clip whose primary family is `family`. */
export function fontFamilyList(family: string): string {
  const rest = SCRIPT_FALLBACK_FAMILIES.filter((f) => f !== family).map((f) => `"${f}"`)
  return [`"${family}"`, ...rest, 'system-ui', 'sans-serif'].join(', ')
}

type TxLike = { x?: KFNum; y?: KFNum; scale?: KFNum; rotation?: KFNum } | undefined

/** Rule 6 inputs at clip-local time `localT`: the transform scale and
 *  rotation (degrees, clockwise) — `resolve_scale_rotation` for a static
 *  value, the keyframe curve for an animated one. Captions: (1, 0). */
export function scaleRotationAt(tx: TxLike, role: string, localT: number): { scale: number; rotation: number } {
  if (role === 'caption' || !tx) return { scale: 1, rotation: 0 }
  return {
    scale: Math.max(0.01, sampleKF(tx.scale, localT, 1)),
    rotation: sampleKF(tx.rotation, localT, 0),
  }
}

/** Is a transform property ANIMATED (>= 2 keys) — the server's is_keyframed. */
export function isAnimated(v: unknown): boolean {
  if (!v || typeof v !== 'object') return false
  const k = (v as { keyframes?: unknown[] }).keyframes
  return Array.isArray(k) && k.length >= 2
}

/** A 1-key list is a constant (resolve_anchor_overrides honours it); a
 *  scalar is itself; anything else has no static value. */
export function staticValue(v: unknown): number | null {
  if (typeof v === 'number' && Number.isFinite(v)) return v
  if (v && typeof v === 'object') {
    const k = (v as { keyframes?: [number, number][] }).keyframes
    if (Array.isArray(k) && k.length === 1) return k[0][1]
  }
  return null
}

/** A TextClip's schema-default transform (edl/schema.py): on each axis it
 *  means "never positioned — role layout" (text_overlay.resolve_anchor_overrides). */
export const TEXT_AXIS_SENTINEL = { x: 540, y: 1700 } as const

/** A typed/dragged coordinate that lands on an axis's "unset" value moves one
 *  canvas pixel, so it is honoured instead of snapping to the role layout. */
export function offAxisSentinel(v: number, sentinel: number): number {
  return Math.abs(v - sentinel) < 0.5 ? sentinel + 1 : v
}
