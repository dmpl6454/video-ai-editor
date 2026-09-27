// Geometry PARAMETERS of one v1 clip at one output frame (instant preview
// spec §3.4 passes 1-4, 7, 8): a pure port of the pixel maths
// `render/compositor.py::_build_clip_video_chain` hands ffmpeg —
//
//   fit (contain: scale "decrease" + pad; cover: scale "increase" + crop,
//        or the oversized cover-pan crop) → rotate in place, black corners →
//   transform (static:    scale to (W·s, H·s) + pad by (dx, dy) + crop;
//              keyframed: the same per frame — scale to (W·s, H·s), centre
//              in a fixed pan frame, crop at centre − (x, y)) →
//   hflip / vflip (effects, on the canvas-sized frame) →
//   opacity (RGB × o) → video fades (RGB × fade factor)
//
// — including ffmpeg's INTEGER rules, which decide where a picture edge lands:
// scale's force_original_aspect_ratio rounds half to even (`lrint`), `pad`
// truncates its offset and rounds it down to the chroma grid (even), `crop`
// rounds its offset half to even, clamps it into the input, then rounds it
// down to even, and the extra cover zoom truncates. Every rule is pinned by
// tests/goldens/geometry_cases.json (marker positions, picture edges and gain
// measured from real compositor renders, tests/gen_geometry_goldens.py).
//
// The result is an INVERSE chain the shader walks per output pixel:
//   canvas px → F2 (pre-transform frame) → F1 (pre-rotation frame) → source uv
// with a bounds test at each stage (black outside), plus the RGB gain.
//
// Keyframe TIME. A clip with any keyframed transform/opacity runs its
// geometry AFTER the retime and the project-grid `fps` in the export
// (compositor `kf_clip`), so every keyframe — and that clip's video fades —
// is evaluated at the clip-local TIMELINE seconds of the OUTPUT frame,
// `(k − clipStart)/R`: exactly `playhead − clip.start`, where the UI
// samples keys (lib/overlay.ts). Before (review RD2) it was the displayed
// SOURCE frame's retimed time, which cannot animate the frames a slow clip
// repeats, a 25 fps source in a 30 fps project, or a freeze (Wave D3, E1a).

import { sampleKF, type KFNum } from '../../overlay'
import { effectiveDuration, type EdlClip } from '../timeline/framePlan'
import { KIND_GAP, type ProgramMap } from '../timeline/programMap'
import { ptsOf, type SourceInfo } from '../timeline/frameMap'

// ------------------------------------------------------------------ affine

/** 2-D affine map: x' = a·x + c·y + e, y' = b·x + d·y + f. */
export interface Affine { a: number; b: number; c: number; d: number; e: number; f: number }
/** Inclusive bounds [x0, x1] × [y0, y1]. */
export interface Bounds { x0: number; y0: number; x1: number; y1: number }

export const IDENTITY: Affine = { a: 1, b: 0, c: 0, d: 1, e: 0, f: 0 }

export function apply(m: Affine, x: number, y: number): [number, number] {
  return [m.a * x + m.c * y + m.e, m.b * x + m.d * y + m.f]
}

/** m ∘ n: apply n first, then m. */
export function compose(m: Affine, n: Affine): Affine {
  return {
    a: m.a * n.a + m.c * n.b, b: m.b * n.a + m.d * n.b,
    c: m.a * n.c + m.c * n.d, d: m.b * n.c + m.d * n.d,
    e: m.a * n.e + m.c * n.f + m.e, f: m.b * n.e + m.d * n.f + m.f,
  }
}

export function invert(m: Affine): Affine {
  const det = m.a * m.d - m.b * m.c
  if (!det) throw new Error('singular geometry map')
  const a = m.d / det, b = -m.b / det, c = -m.c / det, d = m.a / det
  return { a, b, c, d, e: -(a * m.e + c * m.f), f: -(b * m.e + d * m.f) }
}

const scaleT = (sx: number, sy: number, tx = 0, ty = 0): Affine => ({ a: sx, b: 0, c: 0, d: sy, e: tx, f: ty })

const inside = (b: Bounds, x: number, y: number) => x >= b.x0 && x <= b.x1 && y >= b.y0 && y <= b.y1

// ------------------------------------------------------- ffmpeg's integers

/** C `lrint` in the default rounding mode: nearest, ties to even. */
export function lrint(x: number): number {
  const f = Math.floor(x)
  const r = x - f
  if (r > 0.5) return f + 1
  if (r < 0.5) return f
  return f % 2 === 0 ? f : f + 1
}

/** Python `round()` of a float: nearest, ties to even (same as lrint). */
export const pyRound = lrint

/** Round down to the yuv420p chroma grid (`x &= ~1`, `ff_draw_round_to_sub`). */
export const evenDown = (x: number): number => x - (((x % 2) + 2) % 2)

/** C cast of a double to int: toward zero. */
export const trunc = (x: number): number => Math.trunc(x)

/** `%.Nf` then parse: the value ffmpeg reads back from a printed option. */
export const printed = (x: number, digits: number): number => Number(x.toFixed(digits))

/** `av_rescale` (a·b/c, nearest, ties away from zero) for positive values. */
export const rescaleNear = (a: number, b: number, c: number): number => Math.floor((a * b) / c + 0.5)

/** scale=W:H:force_original_aspect_ratio=decrease|increase → output size. */
export function fitDims(srcW: number, srcH: number, W: number, H: number, mode: 'decrease' | 'increase'): [number, number] {
  const tmpW = rescaleNear(H, srcW, srcH)
  const tmpH = rescaleNear(W, srcH, srcW)
  return mode === 'decrease' ? [Math.min(tmpW, W), Math.min(tmpH, H)] : [Math.max(tmpW, W), Math.max(tmpH, H)]
}

/** vf_crop's offset: lrint, clamp into the input, round down to even. */
export function cropOffset(expr: number, inSize: number, outSize: number): number {
  let x = lrint(expr)
  if (x < 0) x = 0
  if (x + outSize > inSize) x = inSize - outSize
  return evenDown(x)
}

/** vf_pad's offset: truncate, round down to even. */
export const padOffset = (expr: number): number => evenDown(trunc(expr))

// -------------------------------------------------------------- keyframes

/** `keyframes.is_keyframed`: ≥ 2 keys. A single-key value is NOT sampled by
 *  the server — it falls back to the property's default. */
export function isKeyframed(v: unknown): v is { keyframes: [number, number][]; interp?: string } {
  if (v === null || typeof v !== 'object') return false
  const k = (v as { keyframes?: unknown }).keyframes
  return Array.isArray(k) && k.length >= 2
}

/** The value the server uses for a transform property at keyframe time t. */
export function propValue(v: unknown, t: number, fallback: number): number {
  if (typeof v === 'number') return v
  if (isKeyframed(v)) return sampleKF(v as KFNum, t, fallback)
  return fallback
}

/** Whether the export runs this clip's geometry on the output grid
 *  (compositor `kf_clip`): any keyframed transform or opacity. */
export function hasKeyframes(clip: EdlClip): boolean {
  const tx = (clip.transform ?? {}) as Transform
  return [tx.x, tx.y, tx.scale, tx.rotation, tx.opacity].some(isKeyframed)
}

/** compositor `kf_pan_frame`: the fixed frame a keyframed scale/pan is
 *  centred in — the largest keyed size plus the largest pan on both sides,
 *  even. Same EDL values, same double operations. */
export function kfPanFrame(tx: Transform, W: number, H: number): [number, number] {
  const peak = (v: unknown, stat: number): number =>
    isKeyframed(v) ? Math.max(...v.keyframes.map((p) => Math.abs(p[1]))) : Math.abs(stat)
  const num = (v: unknown, d: number): number => (typeof v === 'number' ? v : d)
  const sMax = peak(tx.scale, printed(num(tx.scale, 1), 4))
  const side = (canvas: number, pan: number): number => {
    const scaled = Math.max(2, Math.trunc((canvas * sMax) / 2) * 2)
    const n = Math.max(canvas, scaled) + 2 * Math.ceil(pan) + 2
    return n + (n % 2)
  }
  return [side(W, peak(tx.x, printed(num(tx.x, 0), 2))), side(H, peak(tx.y, printed(num(tx.y, 0), 2)))]
}

// --------------------------------------------------------------- the chain

export interface Size { w: number; h: number }

export interface GeometryInput {
  /** EDL canvas size (the compositor's canvas_w/h). */
  canvas: Size
  /** The master's display size (SourceInfo w/h). */
  source: Size
  clip: EdlClip
  /** Source-local seconds of the displayed frame (pre-speed, from the
   *  clip's first kept frame). */
  tSrc: number
  /** Clip-local TIMELINE seconds of the output frame, `(k − clipStart)/R`:
   *  the clock of every keyframe and, on a keyframed clip, of the fades.
   *  Default tSrc / scalar speed. */
  tClip?: number
  /** A non-keyframed clip's fade clock: the retimed source frame's time
   *  (the fades run before the grid there); default tSrc / scalar speed. */
  tOut?: number
}

export interface ClipGeometry {
  /** canvas px → F2 px; F2 bounds (black outside). */
  toF2: Affine
  f2Bounds: Bounds
  /** F2 → F1 (rotation inverse); F1 bounds. With a rotation the bounds
   *  reach past the frame (vf_rotate interpolates up to one pixel outside,
   *  clamping to the edge pixel), and `f1Clamp` pulls F1 back inside. */
  toF1: Affine
  f1Bounds: Bounds
  f1Clamp: boolean
  /** F1 → normalized source uv (0..1, y down); the picture's uv bounds. */
  toUv: Affine
  uvBounds: Bounds
  /** RGB multiplier: opacity × video fades. */
  gain: number
  /** How many source pixels one canvas pixel covers (≥ 1: minification). */
  minification: number
}

const UV_BOUNDS: Bounds = { x0: 0, y0: 0, x1: 1, y1: 1 }

/** vf_rotate paints output pixels whose input position (pixel-index space)
 *  has floor(x) in [−1, inw]: continuous [−0.5, W + 1.5). */
const ROTATE_REACH = (W: number, H: number): Bounds => ({ x0: -0.5, y0: -0.5, x1: W + 1.5, y1: H + 1.5 })

interface Transform { x?: unknown; y?: unknown; scale?: unknown; rotation?: unknown; opacity?: unknown }

function fitStage(c: EdlClip, W: number, H: number, sw: number, sh: number, tx: Transform) {
  const fit = (c as { fit?: string }).fit === 'cover' ? 'cover' : 'contain'
  const animated = isKeyframed(tx.scale) || isKeyframed(tx.x) || isKeyframed(tx.y)
  const xs = typeof tx.x === 'number' && !isKeyframed(tx.x) ? tx.x : 0
  const ys = typeof tx.y === 'number' && !isKeyframed(tx.y) ? tx.y : 0
  const sc = typeof tx.scale === 'number' ? tx.scale : 1
  const coverPan = fit === 'cover' && !animated && (xs !== 0 || ys !== 0)
  if (fit === 'contain') {
    const [pw, ph] = fitDims(sw, sh, W, H, 'decrease')
    const ox = padOffset((W - pw) / 2)
    const oy = padOffset((H - ph) / 2)
    // vf_pad copies its input rounded DOWN to the chroma grid: an odd-sized
    // scaled picture (720x1280 into 640x360 is 203 wide) loses its last
    // column, so the picture is 202 px of a 203 px scale.
    const uvBounds: Bounds = { x0: 0, y0: 0, x1: evenDown(pw) / pw, y1: evenDown(ph) / ph }
    // F1 (x, y) → uv = ((x − ox)/pw, (y − oy)/ph); black outside the picture.
    return { toUv: scaleT(1 / pw, 1 / ph, -ox / pw, -oy / ph), uvBounds, coverPan, scaleSrc: pw / sw }
  }
  const [cw, ch] = fitDims(sw, sh, W, H, 'increase')
  if (!coverPan) {
    const cx = cropOffset((cw - W) / 2, cw, W)
    const cy = cropOffset((ch - H) / 2, ch, H)
    return { toUv: scaleT(1 / cw, 1 / ch, cx / cw, cy / ch), uvBounds: UV_BOUNDS, coverPan, scaleSrc: cw / sw }
  }
  // The oversized cover-pan crop: an extra zoom (≥ 1, printed %.4f) widens
  // the margin, then ONE crop picks the window at centre − (x, y).
  const z = Math.max(1, sc)
  let cw2 = cw
  let ch2 = ch
  if (z > 1.001) {
    const zp = printed(z, 4)
    cw2 = trunc(cw * zp)
    ch2 = trunc(ch * zp)
  }
  const cx = cropOffset((cw2 - W) / 2 - printed(xs, 2), cw2, W)
  const cy = cropOffset((ch2 - H) / 2 - printed(ys, 2), ch2, H)
  return { toUv: scaleT(1 / cw2, 1 / ch2, cx / cw2, cy / ch2), uvBounds: UV_BOUNDS, coverPan, scaleSrc: cw2 / sw }
}

/** Rotation in place (`rotate=a:c=black`, ow = iw): positive = clockwise on
 *  screen. Returns F2 → F1. */
function rotationInverse(tx: Transform, t: number, W: number, H: number): Affine | null {
  let rad: number
  if (isKeyframed(tx.rotation)) {
    rad = (propValue(tx.rotation, t, 0) * Math.PI) / 180
  } else {
    const r = typeof tx.rotation === 'number' ? tx.rotation : 0
    if (!(Math.abs(r) > 0.001)) return null
    rad = (r * 3.14159265) / 180.0
  }
  const cos = Math.cos(rad)
  const sin = Math.sin(rad)
  // forward: p2 = c + Rot(θ)(p1 − c); inverse: p1 = c + Rot(−θ)(p2 − c)
  const cx = W / 2
  const cy = H / 2
  return { a: cos, b: -sin, c: sin, d: cos, e: cx - (cos * cx + sin * cy), f: cy - (-sin * cx + cos * cy) }
}

/** The transform stage: F3 (canvas-sized) → F2, and the zoom it applies. */
function transformInverse(tx: Transform, t: number, W: number, H: number, coverPan: boolean): { m: Affine; zoom: number } {
  const animated = isKeyframed(tx.scale) || isKeyframed(tx.x) || isKeyframed(tx.y)
  const scStatic = typeof tx.scale === 'number' ? tx.scale : 1
  const xStatic = isKeyframed(tx.x) ? 0 : typeof tx.x === 'number' ? tx.x : 0
  const yStatic = isKeyframed(tx.y) ? 0 : typeof tx.y === 'number' ? tx.y : 0
  if (animated) {
    // scale=w='max(2,trunc(W·s/2)·2)' (eval=frame) → pad to the fixed pan
    // frame at its centre (eval=frame: re-centred per frame size, truncated
    // and snapped to the chroma grid) → crop W×H at centre − (x, y).
    const s = isKeyframed(tx.scale) ? propValue(tx.scale, t, 1) : printed(scStatic, 4)
    const sw = Math.max(2, trunc((W * s) / 2) * 2)
    const sh = Math.max(2, trunc((H * s) / 2) * 2)
    const [fw, fh] = kfPanFrame(tx, W, H)
    const xv = isKeyframed(tx.x) ? propValue(tx.x, t, 0) : printed(xStatic, 2)
    const yv = isKeyframed(tx.y) ? propValue(tx.y, t, 0) : printed(yStatic, 2)
    const px = padOffset((fw - sw) / 2)
    const py = padOffset((fh - sh) / 2)
    const cx = cropOffset(Math.trunc((fw - W) / 2) - xv, fw, W)
    const cy = cropOffset(Math.trunc((fh - H) / 2) - yv, fh, H)
    // F3 (x, y) → pan frame (x + cx, y + cy) → scaled (… − px, … − py) → F2 · (W/sw, H/sh)
    return { m: scaleT(W / sw, H / sh, ((cx - px) * W) / sw, ((cy - py) * H) / sh), zoom: sw / W }
  }
  if (coverPan) return { m: IDENTITY, zoom: 1 }
  const changed = (Math.abs(scStatic - 1.0) > 0.001 && scStatic > 0) || xStatic !== 0 || yStatic !== 0
  if (!changed) return { m: IDENTITY, zoom: 1 }
  const sw = Math.max(2, Math.floor(trunc(W * scStatic) / 2) * 2)
  const sh = Math.max(2, Math.floor(trunc(H * scStatic) / 2) * 2)
  const dx = trunc(pyRound(xStatic))
  const dy = trunc(pyRound(yStatic))
  const pw = Math.floor((Math.max(sw, W) + 2 * Math.abs(dx) + 1) / 2) * 2
  const ph = Math.floor((Math.max(sh, H) + 2 * Math.abs(dy) + 1) / 2) * 2
  let px = 0
  let py = 0
  let inW = sw
  let inH = sh
  if (pw > sw || ph > sh) {
    px = padOffset((pw - sw) / 2 + dx)
    py = padOffset((ph - sh) / 2 + dy)
    inW = pw
    inH = ph
  }
  const cx = cropOffset((pw - W) / 2, inW, W)
  const cy = cropOffset((ph - H) / 2, inH, H)
  // F3 (x, y) → padded (x + cx, y + cy) → scaled (… − px, … − py) → F2 · (W/sw, H/sh)
  return { m: scaleT(W / sw, H / sh, ((cx - px) * W) / sw, ((cy - py) * H) / sh), zoom: sw / W }
}

/** hflip/vflip on the canvas-sized frame, in effect order (self-inverse). */
function flipMap(c: EdlClip, W: number, H: number): Affine {
  let m = IDENTITY
  for (const e of (c.effects as Array<{ type?: string }> | undefined) ?? []) {
    if (e?.type === 'hflip') m = compose(scaleT(-1, 1, W, 0), m)
    else if (e?.type === 'vflip') m = compose(scaleT(1, -1, 0, H), m)
  }
  return m
}

/** vf_fade's time-based factor (st/d printed %.3f), 16-bit like ffmpeg. */
export function fadeFactor(tOut: number, st: number, d: number, out: boolean): number {
  let f: number
  if (tOut < st) f = 0
  else if (tOut >= st + d) f = 65535
  else f = Math.min(65535, Math.max(0, Math.trunc(((tOut - st) * 65535) / d)))
  if (out) f = 65535 - f
  return f / 65535
}

/** Opacity × video fades: opacity keyframes at `tKf`, the fades at `tOut`
 *  (both clip-local timeline seconds; see GeometryInput). */
export function clipGain(c: EdlClip, tx: Transform, tKf: number, tOut: number): number {
  let g = 1
  if (isKeyframed(tx.opacity)) g = propValue(tx.opacity, tKf, 1)
  else {
    const o = typeof tx.opacity === 'number' ? tx.opacity : 1
    if (o < 0.999) g = printed(Math.max(0, Math.min(1, o)), 4)
  }
  // `Clip.effective_duration` (scalar speed, a speed curve's mean, a freeze)
  const eff = effectiveDuration(c)
  const vfi = Math.min(Number((c as { video_fade_in?: number }).video_fade_in ?? 0) || 0, eff)
  const vfo = Math.min(Number((c as { video_fade_out?: number }).video_fade_out ?? 0) || 0, eff)
  if (vfi > 0.001) g *= fadeFactor(tOut, 0, printed(vfi, 3), false)
  if (vfo > 0.001) g *= fadeFactor(tOut, printed(Math.max(0, eff - vfo), 3), printed(vfo, 3), true)
  return g
}

/** The geometry of one clip at one displayed frame. */
export function computeGeometry(input: GeometryInput): ClipGeometry {
  const { canvas, clip } = input
  const W = canvas.w
  const H = canvas.h
  const sw = input.source.w > 0 ? input.source.w : W
  const sh = input.source.h > 0 ? input.source.h : H
  const tx = (clip.transform ?? {}) as Transform
  const sp = typeof clip.speed === 'number' && clip.speed > 0 ? clip.speed : 1
  const tKf = input.tClip ?? input.tSrc / sp
  const fit = fitStage(clip, W, H, sw, sh, tx)
  const rot = rotationInverse(tx, tKf, W, H)
  const tr = transformInverse(tx, tKf, W, H, fit.coverPan)
  const toF2 = compose(tr.m, flipMap(clip, W, H))
  const tOut = hasKeyframes(clip) ? tKf : input.tOut ?? input.tSrc / sp
  const frame: Bounds = { x0: 0, y0: 0, x1: W, y1: H }
  return {
    toF2, f2Bounds: frame,
    toF1: rot ?? IDENTITY, f1Bounds: rot ? ROTATE_REACH(W, H) : frame, f1Clamp: rot !== null,
    toUv: fit.toUv, uvBounds: fit.uvBounds,
    gain: clipGain(clip, tx, tKf, tOut),
    minification: 1 / Math.max(1e-6, fit.scaleSrc * tr.zoom),
  }
}

/** Source uv of canvas point (x, y), or null where the chain paints black. */
export function sourceUvAt(g: ClipGeometry, x: number, y: number): [number, number] | null {
  const [x2, y2] = apply(g.toF2, x, y)
  if (!inside(g.f2Bounds, x2, y2)) return null
  let [x1, y1] = apply(g.toF1, x2, y2)
  if (!inside(g.f1Bounds, x1, y1)) return null
  if (g.f1Clamp) {
    x1 = Math.min(g.f2Bounds.x1, Math.max(0, x1))
    y1 = Math.min(g.f2Bounds.y1, Math.max(0, y1))
  }
  const [u, v] = apply(g.toUv, x1, y1)
  if (!inside(g.uvBounds, u, v)) return null
  return [u, v]
}

/** Canvas point showing source uv (u, v) — the forward map (tests, bar
 *  reading); null when that point is cut away by a crop or a bound. */
export function canvasPointOf(g: ClipGeometry, u: number, v: number): [number, number] | null {
  const m = invert(compose(g.toUv, compose(g.toF1, g.toF2)))
  const [x, y] = apply(m, u, v)
  const back = sourceUvAt(g, x, y)
  if (!back || Math.abs(back[0] - u) > 1e-6 || Math.abs(back[1] - v) > 1e-6) return null
  return [x, y]
}

// ---------------------------------------------------- per output frame k

export interface FrameGeometry {
  clip: EdlClip
  srcIndex: number
  srcFrame: number
  geometry: ClipGeometry
}

/** Seconds of source frame `i` after the clip's first kept frame `f0`. */
export function sourceSeconds(info: SourceInfo | null, i: number, f0: number): number {
  if (!info) return 0
  const ticks = ptsOf(info, i) - ptsOf(info, f0)
  return (ticks * info.tb.num) / info.tb.den
}

/** Geometry of output frame k of `pm` (null for a gap: draw black). */
export function frameGeometry(
  pm: ProgramMap, k: number, canvas: Size, infoOf: (src: string) => SourceInfo | null,
): FrameGeometry | null {
  if (k < 0 || k >= pm.total || pm.kind[k] === KIND_GAP) return null
  const ci = pm.clip[k]
  const clip = pm.clips[ci]
  const info = infoOf(clip.src)
  const i = pm.srcFrame[k]
  let tSrc: number
  if (clip.reverse) {
    // The reversed intermediate runs on the project grid; its per-frame
    // index is not in the map, so t is the clip-local output time × speed.
    const sp = typeof clip.speed === 'number' && clip.speed > 0 ? clip.speed : 1
    tSrc = ((k - pm.clipStart[ci]) * pm.R.den * sp) / pm.R.num
  } else {
    const s0 = pm.clipStart[ci]
    const f0 = s0 >= 0 && s0 < pm.total ? (pm.clip[s0] === ci ? pm.srcFrame[s0] : pm.bSrcFrame[s0]) : i
    tSrc = sourceSeconds(info, i, f0 >= 0 ? f0 : i)
  }
  const source = info ? { w: info.w, h: info.h } : canvas
  // A speed curve or a freeze retimes by more than a scalar: the fades'
  // clock is then the clip-local output time (the retimed pts on the grid).
  const scalar = clip.speed === null || clip.speed === undefined || typeof clip.speed === 'number'
  const frozen = typeof (clip as { freeze?: unknown }).freeze === 'number'
  const tClip = ((k - pm.clipStart[ci]) * pm.R.den) / pm.R.num
  const tOut = scalar && !frozen ? undefined : tClip
  return { clip, srcIndex: pm.srcKey[k], srcFrame: i, geometry: computeGeometry({ canvas, source, clip, tSrc, tClip, tOut }) }
}
