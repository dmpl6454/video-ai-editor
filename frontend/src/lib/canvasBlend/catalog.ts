// CapCut Canvas backgrounds and overlay blend modes (wave E, lane F2).
//
// The table is the server's `edl/canvas_blend.py` — `GET /api/canvas-blend/
// presets` serves the same payload, and `blendModes.json` beside this file IS
// that payload, pinned byte for byte by tests/test_f2_canvas_blend.py (the
// renderer below needs the CSS operators synchronously inside a rAF paint
// loop, so it cannot wait on a fetch; the clip-animation table does the same,
// lib/anim/clipAnimTable.json). Nothing here restates a mode.

import TABLE from './blendModes.json'

export interface BlendMode {
  id: string
  label: string
  /** The CSS `mix-blend-mode` the live PiP layer composites with. */
  css: string
  /** Porter-Duff (plus-lighter / plus-darker) rather than W3C separable. */
  porter_duff: boolean
}

export interface BlurLevel { level: number; label: string; sigma_frac: number }

export type CanvasKind = 'color' | 'blur' | 'image'

export interface CanvasBg {
  type: CanvasKind
  color?: string
  blur?: number
  image?: string | null
}

export const BLENDS: readonly BlendMode[] = TABLE.blends
export const CANVAS_KINDS = TABLE.canvas.kinds as CanvasKind[]
export const BLUR_LEVELS: readonly BlurLevel[] = TABLE.canvas.blur_levels
export const BLUR_DEFAULT: number = TABLE.canvas.blur_default
export const BLUR_DOWNSCALE: number = TABLE.canvas.blur_downscale
export const SWATCHES: readonly string[] = TABLE.canvas.swatches
export const IMAGE_EXTS: readonly string[] = TABLE.canvas.image_exts

const BY_ID = new Map(BLENDS.map((b) => [b.id, b]))

/** A clip's blend id ('normal' when unset or unknown). */
export function blendOf(clip: unknown): string {
  const b = (clip as { blend?: unknown } | null)?.blend
  return typeof b === 'string' && BY_ID.has(b) ? b : 'normal'
}

export function blendLabel(id: string): string {
  return BY_ID.get(id)?.label ?? 'Normal'
}

/** The CSS `mix-blend-mode` keyword of blend `id`. */
export function blendCss(id: string): string {
  return BY_ID.get(id)?.css ?? 'normal'
}

const SUPPORT = new Map<string, boolean>()

/** Can THIS browser composite blend `id` live? Every W3C separable mode is
 *  universal; `plus-lighter` (Add) is in WebKit and Blink; `plus-darker`
 *  (Linear Burn) is WebKit only — WKWebView, the app's own engine, draws it,
 *  a Chromium dev browser does not (the Inspector says so; the export always
 *  does). */
export function blendSupported(id: string): boolean {
  const css = blendCss(id)
  if (css === 'normal') return true
  let ok = SUPPORT.get(css)
  if (ok === undefined) {
    ok = typeof CSS !== 'undefined' && typeof CSS.supports === 'function'
      ? CSS.supports('mix-blend-mode', css) : false
    SUPPORT.set(css, ok)
  }
  return ok
}

/** Tests: forget the cached `CSS.supports` answers. */
export function resetBlendSupport(): void {
  SUPPORT.clear()
}

/** Is this WebKit (the app's own engine, WKWebView)? `plus-darker` is a
 *  WebKit-only operator, so its support is the feature test. */
export function isWebKitCompositor(): boolean {
  return blendSupported('linear_burn')
}

/** Where the LIVE blend of this browser is not the export's, measured
 *  (tests/test_f2_blend_browser_parity.py, Playwright Chromium and WebKit,
 *  the export's own decoded frames on both sides; every other mode is
 *  ≥ 36.4 dB): WebKit draws Soft Light with its own curve (32.0 dB at full
 *  opacity) and does not clamp Color Dodge / Color Burn before the opacity
 *  mix (16-17 dB where they saturate, at 50 %; exact at 100 %). Chromium has
 *  no `plus-darker`, so Linear Burn previews as Normal there. */
export const LIVE_BLEND_FLAGS = {
  webkit: { soft_light: 'approx', color_dodge: 'partial-opacity', color_burn: 'partial-opacity' },
} as const

/** A one-line note when the live preview of `mode` (at opacity `opacity`,
 *  `null` = keyframed) is not the export's in THIS browser, else null. */
export function blendLiveNote(mode: string, opacity: number | null): string | null {
  const label = blendLabel(mode)
  if (!blendSupported(mode)) {
    return `This browser cannot preview ${label} live, so the preview shows it as Normal. The app window and the export blend it.`
  }
  if (!isWebKitCompositor()) return null
  const flag = (LIVE_BLEND_FLAGS.webkit as Record<string, string>)[mode]
  if (flag === 'approx') return `The preview approximates ${label}; the export blends it exactly.`
  if (flag === 'partial-opacity' && (opacity === null || opacity < 0.999)) {
    return `Below full opacity the preview approximates ${label} where it saturates; the export blends it exactly.`
  }
  return null
}

/** A clip's canvas background, or null (black bars). */
export function canvasBgOf(clip: unknown): CanvasBg | null {
  const bg = (clip as { canvas_bg?: unknown } | null)?.canvas_bg
  if (!bg || typeof bg !== 'object') return null
  const t = (bg as { type?: unknown }).type
  return t === 'color' || t === 'blur' || t === 'image' ? (bg as CanvasBg) : null
}

/** '#RRGGBB' → [r, g, b] in 0..255 (black for anything else). */
export function hexRgb(hex: string | undefined): [number, number, number] {
  const m = /^#?([0-9a-fA-F]{6})$/.exec(hex ?? '')
  if (!m) return [0, 0, 0]
  const n = parseInt(m[1], 16)
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255]
}

/** The blur level's label ("Medium"). */
export function blurLabel(level: number | undefined): string {
  return BLUR_LEVELS.find((b) => b.level === level)?.label ?? BLUR_LEVELS[BLUR_DEFAULT - 1]?.label ?? ''
}
