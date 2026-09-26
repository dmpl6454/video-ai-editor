// The Inspector's speed-curve editor, as pure maths (wave D, lane S2).
//
// A curve is `[[x, r], ...]`: x a position over the clip AS IT PLAYS (0..1),
// r the speed there (0.1-10x), linear between points (`edl/speed_curve.py`;
// the render and `lib/preview/timeline/speedCurve.ts` read the same shape).
// Everything the editor does to a curve is a function here, so it is unit
// tested against numbers instead of screenshots:
//
//  * the graph's axes: x linear, speed on a LOG axis with 1x in the middle
//    (0.1x and 10x are equally far from normal, as in CapCut's editor);
//  * dragging and keyboard-nudging a point: the ends stay at x = 0 and 1,
//    a point never crosses or touches its neighbours, speed is clamped;
//  * adding a point ON the curve (the shape does not change until it is
//    dragged), removing one (never an end, never below 2 points);
//  * the resulting timeline duration (`S / mean(r)`, the model's footprint).
//
// Values committed to the server are rounded (x to 0.001, r to 0.01): what
// the Inspector shows is exactly what is stored.

import { meanSpeed, type CurvePoints } from '../preview/timeline/speedCurve'

export type Point = readonly [number, number]
export type Curve = readonly Point[]

/** The curve's speed range and point budget. The server owns them
 *  (`speed_presets.CURVE_SPEED_RANGE`, `MAX_CURVE_POINTS`) and serves them in
 *  `GET /api/speed/presets` (`curve_range`, `max_points`); speedCatalog.ts
 *  installs them here when the catalog loads (review RD2: they were only
 *  hard-coded). Until then, the values the server serves today. */
export interface CurveLimits { readonly min: number; readonly max: number; readonly maxPoints: number }
export const DEFAULT_CURVE_LIMITS: CurveLimits = { min: 0.1, max: 10, maxPoints: 32 }
/** The defaults (tests, and the graph's axis labels). */
export const SPEED_MIN = DEFAULT_CURVE_LIMITS.min
export const SPEED_MAX = DEFAULT_CURVE_LIMITS.max
export const MAX_POINTS = DEFAULT_CURVE_LIMITS.maxPoints

let limits: CurveLimits = DEFAULT_CURVE_LIMITS

/** The limits in force (the server's once its catalog has loaded). */
export function curveLimits(): CurveLimits {
  return limits
}

/** The limits a catalog answer carries, or null when they are unusable. */
export function limitsFromCatalog(c: { curve_range?: unknown; max_points?: unknown }): CurveLimits | null {
  const r = c.curve_range
  if (!Array.isArray(r) || r.length !== 2) return null
  const [min, max] = r.map(Number)
  const maxPoints = Number(c.max_points)
  if (!(min > 0 && max > min && Number.isFinite(max)) || !(Number.isInteger(maxPoints) && maxPoints >= 2)) return null
  return { min, max, maxPoints }
}

/** Install the server's limits (null: back to the defaults). */
export function setCurveLimits(l: CurveLimits | null): void {
  limits = l ?? DEFAULT_CURVE_LIMITS
}
/** Closest two points may sit, as a fraction of the clip. */
export const MIN_GAP = 0.02
/** Keyboard steps: position (fraction of the clip) and speed (decades). */
export const NUDGE_X = 0.01
export const NUDGE_X_BIG = 0.1
export const NUDGE_LOG = 0.05
export const NUDGE_LOG_BIG = 0.25

const logMin = () => Math.log10(limits.min)
const logMax = () => Math.log10(limits.max)

/** Custom's starting shape: flat 1x with three handles to pull. */
export const CUSTOM_START: Curve = [[0, 1], [0.25, 1], [0.5, 1], [0.75, 1], [1, 1]]

export const clampSpeed = (r: number): number => Math.min(limits.max, Math.max(limits.min, r))

/** Speed → the graph's vertical position, 0 at the TOP (10x) to 1 at the
 *  bottom (0.1x); 1x is 0.5. */
export function speedToY(r: number): number {
  const l = Math.log10(clampSpeed(r))
  return (logMax() - l) / (logMax() - logMin())
}

/** The inverse of `speedToY` (clamped to the range). */
export function yToSpeed(y: number): number {
  const v = Math.min(1, Math.max(0, y))
  return clampSpeed(10 ** (logMax() - v * (logMax() - logMin())))
}

export const roundX = (x: number): number => Math.round(x * 1000) / 1000
export const roundSpeed = (r: number): number => Math.round(clampSpeed(r) * 100) / 100

/** A curve as it is committed: rounded, ends pinned at 0 and 1. */
export function normalizeForCommit(c: Curve): [number, number][] {
  const out = c.map(([x, r]) => [roundX(x), roundSpeed(r)] as [number, number])
  if (out.length) { out[0][0] = 0; out[out.length - 1][0] = 1 }
  return out
}

/** The speed ON the curve at position x (linear between points). */
export function speedAtX(c: Curve, x: number): number {
  if (!c.length) return 1
  if (x <= c[0][0]) return c[0][1]
  for (let i = 0; i + 1 < c.length; i++) {
    const [x0, r0] = c[i]
    const [x1, r1] = c[i + 1]
    if (x <= x1) return x1 > x0 ? r0 + (r1 - r0) * (x - x0) / (x1 - x0) : r1
  }
  return c[c.length - 1][1]
}

/** Where point `i` may sit horizontally: an end is pinned; an inner point
 *  stays MIN_GAP clear of both neighbours. */
export function xBounds(c: Curve, i: number): [number, number] {
  if (i === 0) return [0, 0]
  if (i === c.length - 1) return [1, 1]
  return [c[i - 1][0] + MIN_GAP, c[i + 1][0] - MIN_GAP]
}

/** Point `i` moved to (x, r), both clamped. A new array; the input is
 *  never mutated. */
export function movePoint(c: Curve, i: number, x: number, r: number): Point[] {
  if (i < 0 || i >= c.length) return [...c]
  const [lo, hi] = xBounds(c, i)
  const nx = lo > hi ? c[i][0] : Math.min(hi, Math.max(lo, x))
  return c.map((p, j) => (j === i ? [nx, clampSpeed(r)] as const : p))
}

export type NudgeKey = 'ArrowLeft' | 'ArrowRight' | 'ArrowUp' | 'ArrowDown'

/** The arrow-key move of point `i`: left/right by NUDGE_X of the clip,
 *  up/down by NUDGE_LOG decades of speed (the big steps with Shift). The
 *  speed is snapped to 0.01 so repeated presses land on shown values. */
export function nudgePoint(c: Curve, i: number, key: NudgeKey, big = false): Point[] {
  const [x, r] = c[i]
  switch (key) {
    case 'ArrowLeft': return movePoint(c, i, x - (big ? NUDGE_X_BIG : NUDGE_X), r)
    case 'ArrowRight': return movePoint(c, i, x + (big ? NUDGE_X_BIG : NUDGE_X), r)
    case 'ArrowUp':
    case 'ArrowDown': {
      const step = (big ? NUDGE_LOG_BIG : NUDGE_LOG) * (key === 'ArrowUp' ? 1 : -1)
      let nr = roundSpeed(10 ** (Math.log10(r) + step))
      // At 0.1-0.2x a 0.05-decade step rounds back onto the same 0.01.
      if (nr === roundSpeed(r) && nr > limits.min && nr < limits.max) nr = roundSpeed(r + (step > 0 ? 0.01 : -0.01))
      return movePoint(c, i, x, nr)
    }
  }
}

/** Index a point at `x` would take, or -1 when it cannot be added there
 *  (too close to a point, outside 0..1, or the curve is full). */
export function insertIndex(c: Curve, x: number): number {
  if (c.length >= limits.maxPoints || !(x > 0 && x < 1)) return -1
  for (let i = 0; i + 1 < c.length; i++) {
    if (x > c[i][0] && x < c[i + 1][0]) {
      if (x - c[i][0] < MIN_GAP || c[i + 1][0] - x < MIN_GAP) return -1
      return i + 1
    }
  }
  return -1
}

/** A point added at `x`, ON the curve (its speed is the curve's there, so
 *  the shape is unchanged until it is dragged). Returns the new curve and
 *  the new point's index, or null when it cannot be added. */
export function addPoint(c: Curve, x: number): { curve: Point[]; index: number } | null {
  const i = insertIndex(c, x)
  if (i < 0) return null
  const r = roundSpeed(speedAtX(c, x))
  const curve = [...c.slice(0, i), [roundX(x), r] as const, ...c.slice(i)]
  return { curve, index: i }
}

/** Where "Add point" puts one when no position is given: the middle of the
 *  widest gap (so repeated presses keep splitting the curve evenly). */
export function widestGapMid(c: Curve): number {
  let best = -1
  let mid = 0.5
  for (let i = 0; i + 1 < c.length; i++) {
    const g = c[i + 1][0] - c[i][0]
    if (g > best) { best = g; mid = (c[i][0] + c[i + 1][0]) / 2 }
  }
  return roundX(mid)
}

/** Point `i` removed: never an end, never below two points. */
export function canRemove(c: Curve, i: number): boolean {
  return i > 0 && i < c.length - 1 && c.length > 2
}

export function removePoint(c: Curve, i: number): Point[] {
  return canRemove(c, i) ? c.filter((_, j) => j !== i) : [...c]
}

/** Timeline seconds a clip of `sourceSeconds` fills at this curve: the
 *  model's footprint `S / mean(r)` (`Clip.effective_duration`). */
export function curveDuration(c: Curve, sourceSeconds: number): number {
  const m = meanSpeed(c as CurvePoints)
  return m > 0 ? Math.max(0, sourceSeconds) / m : Math.max(0, sourceSeconds)
}

/** The preset whose points are exactly `c`, else null (the server names a
 *  stored curve the same way, `speed_presets.curve_name`). */
export function matchPreset<P extends { id: string; points: readonly (readonly number[])[] }>(
  c: Curve, presets: readonly P[]): P | null {
  return presets.find((p) => p.points.length === c.length
    && p.points.every((q, i) => q[0] === c[i][0] && q[1] === c[i][1])) ?? null
}

/** The SVG path of the curve in a `w` × `h` box (points joined by straight
 *  lines — the speed IS linear between points). */
export function curvePath(c: Curve, w: number, h: number): string {
  return c.map(([x, r], i) => `${i ? 'L' : 'M'}${(x * w).toFixed(2)} ${(speedToY(r) * h).toFixed(2)}`).join(' ')
}

/** "1.5×", "0.25×", "10×": how a speed reads next to a point. */
export function formatSpeed(r: number): string {
  const v = roundSpeed(r)
  return `${Number.isInteger(v) ? v.toFixed(0) : v.toFixed(2).replace(/0$/, '')}×`
}

/** The accessible name of point `i` (read by VoiceOver while it moves). */
export function pointLabel(c: Curve, i: number): string {
  const [x, r] = c[i]
  const where = i === 0 ? 'start' : i === c.length - 1 ? 'end' : `${Math.round(x * 100)}% through the clip`
  return `Speed point ${i + 1} of ${c.length}, ${where}, ${formatSpeed(r)}`
}
