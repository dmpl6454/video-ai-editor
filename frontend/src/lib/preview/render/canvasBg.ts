// The CapCut Canvas background of a letterboxed main-track clip, as the
// engine's compositor draws it (wave E, lane F2) — a port of
// render/canvas_bg.py, the export's own rule:
//
//   color  the letterbox is the colour (vf_pad's colour, BT.709);
//   image  the letterbox is the picture cover-fitted to the canvas — the
//          SAME file the export uses, served by the canvas-bg route
//          (render/canvas_bg.image_file), sampled 1:1;
//   blur   the clip itself scaled to COVER a 1/4-size canvas (ffmpeg's size
//          and crop rules, `blurDims` / `fitDims` / `cropOffset`), a
//          Gaussian of the export's sigma there, and bilinear back up.
//
// Review RE: the background is a STILL, canvas-sized layer under the moving
// picture (render/canvas_bg.composite_block): the pan, zoom, rotation,
// flips, opacity and the clip animation move and fade the PICTURE only, the
// video fades (to / from black) the whole frame. `toBg` maps a CANVAS point
// to the background's uv (0..1). (Wave E first bound it to the fitted frame,
// so a Zoom In or a scale < 1 shrank it and black bars came back.)

import { BLUR_DOWNSCALE, BLUR_LEVELS, canvasBgOf, hexRgb } from '../../canvasBlend/catalog'
import type { EdlClip } from '../timeline/framePlan'
import { compose, cropOffset, fitDims, flipStage, lrint, type Affine, type Size } from './geometry'

export type BgDraw =
  | { mode: 'color'; rgb: [number, number, number]; toBg: Affine }
  | { mode: 'image'; url: string; toBg: Affine }
  | {
    mode: 'blur'; toBg: Affine
    /** The small cover frame the blur runs on (`blurDims`). */
    small: Size
    /** Gaussian sigma in SMALL pixels (canvas_bg.blur_sigma_small). */
    sigma: number
    /** Small-frame pixel (x, y) → source uv (0..1, y down): the cover crop
     *  (its `a` × the texture width = texels per small pixel). */
    coverToUv: Affine
  }

/** canvas_bg.blur_dims: 1/BLUR_DOWNSCALE of the canvas, even, ≥ 2. */
export function blurDims(W: number, H: number): [number, number] {
  const d = BLUR_DOWNSCALE
  return [Math.max(2, Math.floor(lrint(W / d) / 2) * 2), Math.max(2, Math.floor(lrint(H / d) / 2) * 2)]
}

/** canvas_blend.blur_level(): the level, clamped to the table. */
function levelOf(v: unknown): number {
  const n = Math.round(Number(v))
  return Math.max(1, Math.min(BLUR_LEVELS.length, Number.isFinite(n) ? n : 2))
}

/** canvas_bg.blur_sigma_small: the canvas sigma over the downscale. */
export function blurSigmaSmall(level: unknown, W: number, H: number): number {
  const [bw] = blurDims(W, H)
  return BLUR_LEVELS[levelOf(level) - 1].sigma_frac * Math.min(W, H) * (bw / W)
}

const scaleT = (sx: number, sy: number, tx = 0, ty = 0): Affine => ({ a: sx, b: 0, c: 0, d: sy, e: tx, f: ty })

/** The background of `clip` on `canvas` (null: black bars — no background,
 *  or a `cover` fit, which has no bars). `baseUrl` is the session's
 *  `/api/sessions/{sid}/canvas-bg`; without it an image background is null. */
export function canvasBgDraw(clip: EdlClip, canvas: Size, source: Size, baseUrl?: string | null): BgDraw | null {
  const bg = canvasBgOf(clip)
  if (!bg || (clip as { fit?: string }).fit === 'cover') return null
  const W = canvas.w
  const H = canvas.h
  const toBg = scaleT(1 / W, 1 / H)
  // a blur is a copy of the picture: mirrored with it (canvas_bg.background_chain)
  const tx = ((clip as { transform?: { flip_h?: unknown; flip_v?: unknown } }).transform ?? {})
  const blurToBg = tx.flip_h || tx.flip_v ? compose(toBg, flipStage(tx, W, H)) : toBg
  if (bg.type === 'color') {
    const [r, g, b] = hexRgb(bg.color)
    return { mode: 'color', rgb: [r, g, b], toBg }
  }
  if (bg.type === 'image') {
    if (!baseUrl || !bg.image) return null
    const id = encodeURIComponent(String((clip as { id?: unknown }).id ?? ''))
    // the picture's own file identity keys the URL, so a replaced picture is refetched
    const v = encodeURIComponent(bg.image)
    return { mode: 'image', url: `${baseUrl}/${id}.png?w=${W}&h=${H}&v=${v}`, toBg }
  }
  const [bw, bh] = blurDims(W, H)
  const sw = source.w > 0 ? source.w : W
  const sh = source.h > 0 ? source.h : H
  const [cw, ch] = fitDims(sw, sh, bw, bh, 'increase')
  const cx = cropOffset((cw - bw) / 2, cw, bw)
  const cy = cropOffset((ch - bh) / 2, ch, bh)
  return {
    mode: 'blur', toBg: blurToBg, small: { w: bw, h: bh }, sigma: blurSigmaSmall(bg.blur, W, H),
    coverToUv: scaleT(1 / cw, 1 / ch, cx / cw, cy / ch),
  }
}
