// CapCut clip animations (wave E, F1) — the browser's evaluator of the ONE
// preset table `edl/clip_animations.py`. `clipAnimTable.json` is that table,
// dumped by tests/gen_clip_anim_table.py and pinned equal to it by
// tests/test_clip_animations.py; `clipAnimCases.json` holds the Python
// plan's own numbers at many times, which clipAnim.test.ts asserts this file
// reproduces. The engine's geometry (v1), pipDraw and StickerLayer (overlays
// and stickers, in both preview modes) all read `planOf` / `animAt`.
//
// An animation composes ON TOP of the clip's own pose, on the keyframe clock
// (clip-local timeline seconds, `playhead − clip.start`):
//   scale ×, x / y + (a share of the canvas W / H), rotation + (degrees,
//   clockwise), opacity × (linear ramps, ffmpeg `fade`), blur (a blurred
//   copy mixed in with weight 1→0 In, 0→1 Out).

import TABLE from './clipAnimTable.json'
import { sampleKF, type KFNum } from '../overlay'

export type AnimKind = 'in' | 'out' | 'combo'
export type AnimChannel = 'scale' | 'x' | 'y' | 'rotation'
export const ANIM_CHANNELS: readonly AnimChannel[] = ['scale', 'x', 'y', 'rotation']

export interface AnimPresetJson {
  id: string
  kind: AnimKind
  label: string
  hint: string
  icon: string
  channels: Record<string, { keys: number[][]; interp: string }>
  waves: Record<string, { bias: number; terms: number[][] }>
}

export interface AnimTable {
  in: AnimPresetJson[]
  out: AnimPresetJson[]
  combo: AnimPresetJson[]
  dur_default: number
  dur_range: number[]
  share: number
  blur_sigma_frac: number
}

export const ANIM_TABLE = TABLE as unknown as AnimTable

/** The clip / sticker fields an animation lives in (schema `_ClipAnimFields`). */
export interface AnimFields {
  anim_in?: string | null
  anim_out?: string | null
  anim_combo?: string | null
  anim_dur?: number | null
  anim_out_dur?: number | null
}

const BY_KIND: Record<AnimKind, Map<string, AnimPresetJson>> = {
  in: new Map(ANIM_TABLE.in.map((p) => [p.id, p])),
  out: new Map(ANIM_TABLE.out.map((p) => [p.id, p])),
  combo: new Map(ANIM_TABLE.combo.map((p) => [p.id, p])),
}

export function animPreset(kind: AnimKind, id: string | null | undefined): AnimPresetJson | null {
  return (id && BY_KIND[kind].get(id)) || null
}

export function hasAnimation(c: AnimFields | null | undefined): boolean {
  return !!c && !!(c.anim_in || c.anim_out || c.anim_combo)
}

/** `clip_animations.duration_of`: the side's own length or the default, in
 *  dur_range, never more than `share` of the window (never under 0.1 s). */
export function durationOf(want: number | null | undefined, window: number): number {
  const [lo, hi] = ANIM_TABLE.dur_range
  const base = typeof want === 'number' && Number.isFinite(want) ? Math.min(hi, Math.max(lo, want)) : ANIM_TABLE.dur_default
  return Math.min(base, Math.max(0.1, window * ANIM_TABLE.share))
}

interface KF { keyframes: [number, number][]; interp: string }
interface Wave { bias: number; terms: number[][] }

export interface AnimPlan {
  window: number
  dIn: number
  dOut: number
  keyed: Partial<Record<AnimChannel, KF[]>>
  waves: Partial<Record<AnimChannel, Wave>>
  fadeIn: [number, number] | null
  fadeOut: [number, number] | null
  blurIn: [number, number] | null
  blurOut: [number, number] | null
}

/** `clip_animations.plan`: the clip's animation over `window` seconds. */
export function planOf(c: AnimFields | null | undefined, window: number): AnimPlan | null {
  if (!c) return null
  const combo = animPreset('combo', c.anim_combo)
  const pin = combo ? null : animPreset('in', c.anim_in)
  const pout = combo ? null : animPreset('out', c.anim_out)
  if (!pin && !pout && !combo) return null
  const w = Math.max(0, window)
  const dIn = pin ? durationOf(c.anim_dur, w) : 0
  const dOut = pout ? durationOf(c.anim_out_dur, w) : 0
  const plan: AnimPlan = { window: w, dIn, dOut, keyed: {}, waves: {}, fadeIn: null, fadeOut: null, blurIn: null, blurOut: null }
  const add = (name: string, kf: KF) => {
    const ch = name as AnimChannel
    ;(plan.keyed[ch] ??= []).push(kf)
  }
  if (pin) {
    for (const [name, ch] of Object.entries(pin.channels)) {
      if (name === 'opacity') plan.fadeIn = [0, dIn]
      else if (name === 'blur') plan.blurIn = [0, dIn]
      else add(name, { keyframes: ch.keys.map(([p, v]) => [p * dIn, v] as [number, number]), interp: ch.interp })
    }
  }
  if (pout) {
    const st = Math.max(0, w - dOut)
    for (const [name, ch] of Object.entries(pout.channels)) {
      if (name === 'opacity') plan.fadeOut = [st, dOut]
      else if (name === 'blur') plan.blurOut = [st, dOut]
      else add(name, { keyframes: ch.keys.map(([p, v]) => [st + p * dOut, v] as [number, number]), interp: ch.interp })
    }
  }
  if (combo) {
    for (const [name, wv] of Object.entries(combo.waves)) plan.waves[name as AnimChannel] = wv
  }
  return plan
}

export function animates(pl: AnimPlan | null, ch: AnimChannel): boolean {
  return !!pl && (!!pl.keyed[ch]?.length || !!pl.waves[ch])
}

/** Any of scale / x / y moves (the v1 chain's keyed-transform branch). */
export function animatesGeometry(pl: AnimPlan | null): boolean {
  return animates(pl, 'scale') || animates(pl, 'x') || animates(pl, 'y')
}

const TAU = 2 * Math.PI

/** `AnimPlan.value`: the channel at clip-local t (1 for scale, else 0, at rest). */
export function animValue(pl: AnimPlan | null, ch: AnimChannel, t: number): number {
  const mul = ch === 'scale'
  let v = mul ? 1 : 0
  if (!pl) return v
  for (const kf of pl.keyed[ch] ?? []) {
    const s = sampleKF(kf as unknown as KFNum, t, mul ? 1 : 0)
    v = mul ? v * s : v + s
  }
  const w = pl.waves[ch]
  if (w) {
    let s = w.bias
    for (const [a, hz, ph] of w.terms) s += a * Math.sin(TAU * hz * t + ph)
    v = mul ? v * s : v + s
  }
  return v
}

/** `AnimPlan.peak`: the largest |value| the channel takes. */
export function animPeak(pl: AnimPlan | null, ch: AnimChannel): number {
  const mul = ch === 'scale'
  let v = mul ? 1 : 0
  if (!pl) return v
  for (const kf of pl.keyed[ch] ?? []) {
    const m = Math.max(...kf.keyframes.map((p) => Math.abs(p[1])))
    v = mul ? v * m : v + m
  }
  const w = pl.waves[ch]
  if (w) {
    const m = Math.abs(w.bias) + w.terms.reduce((s, [a]) => s + Math.abs(a), 0)
    v = mul ? v * m : v + m
  }
  return v
}

/** `vf_fade`'s 16-bit factor over a printed (%.3f) ramp. */
function ramp16(t: number, st0: number, d0: number, rising: boolean): number {
  const st = Number(st0.toFixed(3))
  const d = Number(d0.toFixed(3))
  let f: number
  if (t < st) f = 0
  else if (t >= st + d) f = 65535
  else f = Math.min(65535, Math.max(0, Math.trunc(((t - st) * 65535) / d)))
  if (!rising) f = 65535 - f
  return f / 65535
}

/** `clip_animations.fade_gain`: the opacity ramps at clip-local t. */
export function fadeGain(pl: AnimPlan | null, t: number): number {
  if (!pl) return 1
  let g = 1
  if (pl.fadeIn) g *= ramp16(t, pl.fadeIn[0], pl.fadeIn[1], true)
  if (pl.fadeOut) g *= ramp16(t, pl.fadeOut[0], pl.fadeOut[1], false)
  return g
}

/** `clip_animations.blur_weight`: the blurred copy's weight at clip-local t. */
export function blurWeight(pl: AnimPlan | null, t: number): number {
  if (!pl) return 0
  let w = 0
  if (pl.blurIn) w = Math.max(w, ramp16(t, pl.blurIn[0], pl.blurIn[1], false))
  if (pl.blurOut) w = Math.max(w, ramp16(t, pl.blurOut[0], pl.blurOut[1], true))
  return w
}

/** Gaussian sigma of a full blur for an output of w × h px. */
export function blurSigma(w: number, h: number): number {
  return Math.round(ANIM_TABLE.blur_sigma_frac * Math.min(w, h) * 1000) / 1000
}

/** Everything a 2-D draw needs at clip-local t: travel as a share of the
 *  canvas, the zoom multiplier, the extra rotation (degrees), the opacity
 *  multiplier and the blur weight. Identity when the clip has none. */
export interface AnimPose { dx: number; dy: number; scale: number; rotation: number; alpha: number; blur: number }

export const REST_POSE: AnimPose = { dx: 0, dy: 0, scale: 1, rotation: 0, alpha: 1, blur: 0 }

export function poseAt(pl: AnimPlan | null, t: number): AnimPose {
  if (!pl) return REST_POSE
  return {
    dx: animValue(pl, 'x', t),
    dy: animValue(pl, 'y', t),
    scale: animValue(pl, 'scale', t),
    rotation: animValue(pl, 'rotation', t),
    alpha: fadeGain(pl, t),
    blur: blurWeight(pl, t),
  }
}

/** `poseAt(planOf(c, window), t)` — the common one-shot. */
export function animAt(c: AnimFields | null | undefined, t: number, window: number): AnimPose {
  return hasAnimation(c) ? poseAt(planOf(c, window), t) : REST_POSE
}
