// Speed CURVES — a line-for-line port of `edl/speed_curve.py` (read its
// docstring for the model). A clip's `speed` may be `{curve: [[x, r], ...]}`:
// x a position over the clip's OUTPUT (0..1), r the speed there (0.1-10x),
// piecewise linear. The server normalises every curve on validation, so the
// client only ever sees canonical points.
//
// EXACTNESS. The picture of a curve clip is `setpts` of the closed form
//   out(T) = t_i + 2·q / (r_i + sqrt(r_i² + k_i·q)),  q = T − s_i
// with every constant printed in `repr` (ffmpeg parses the exact double). The
// functions below repeat the SAME double operations in the SAME order, and
// `Math.sqrt` is correctly rounded like C's and Python's (IEEE 754), so the
// frame the export shows at output slot k is reproduced bit for bit — pinned
// by the `speed` group of tests/goldens/frame_map (decoded real renders).

/** A canonical curve: [x, speed] points, x from 0 to 1. */
export type CurvePoints = ReadonlyArray<readonly [number, number]>

/** `curve_points`: the points of a curve speed, or null (a number, null, or
 *  a dict without a curve). */
export function curvePoints(speed: unknown): CurvePoints | null {
  if (typeof speed !== 'object' || speed === null) return null
  const raw = (speed as { curve?: unknown }).curve
  if (!Array.isArray(raw) || raw.length < 2) return null
  return raw.map((p) => [Number((p as number[])[0]), Number((p as number[])[1])] as const)
}

export const isCurve = (speed: unknown): boolean => curvePoints(speed) !== null

/** `mean_speed`: Σ Δx · (r_a + r_b)/2 in point order. */
export function meanSpeed(points: CurvePoints): number {
  let m = 0
  for (let i = 0; i + 1 < points.length; i++) {
    const [x0, r0] = points[i]
    const [x1, r1] = points[i + 1]
    m = m + (x1 - x0) * (r0 + r1) / 2
  }
  return m
}

export interface CurveSeg { s0: number; s1: number; t0: number; t1: number; r: number; rr: number; k: number; r1: number }

export interface CurveMap { S: number; D: number; segs: CurveSeg[]; sEnd: number; rEnd: number }

/** `curve_map`: lay the points over `S` source seconds (null if S <= 1e-9). */
export function curveMap(points: CurvePoints, S: number): CurveMap | null {
  if (!points || points.length < 2 || !(S > 1e-9)) return null
  const m = meanSpeed(points)
  const D = S / m
  const segs: CurveSeg[] = []
  let s = 0
  for (let i = 0; i + 1 < points.length; i++) {
    const [x0, r0] = points[i]
    const [x1, r1] = points[i + 1]
    const t0 = D * x0
    const t1 = D * x1
    if (!(t1 > t0)) continue
    const s1 = s + (t1 - t0) * (r0 + r1) / 2
    const k = 2 * (r1 - r0) / (t1 - t0)
    segs.push({ s0: s, s1, t0, t1, r: r0, rr: r0 * r0, k, r1 })
    s = s1
  }
  return { S, D, segs, sEnd: s, rEnd: points[points.length - 1][1] }
}

/** `out_seconds`: output seconds at source seconds T (the setpts expression). */
export function outSeconds(cm: CurveMap, T: number): number {
  for (const g of cm.segs) {
    if (T < g.s1) {
      const q = T - g.s0
      return g.t0 + 2 * q / (g.r + Math.sqrt(g.rr + g.k * q))
    }
  }
  return cm.D + (T - cm.sEnd) / cm.rEnd
}

/** `source_seconds`: source seconds consumed after t output seconds. */
export function sourceSeconds(cm: CurveMap, t: number): number {
  if (t <= 0) return 0
  for (const g of cm.segs) {
    if (t < g.t1) {
      const tau = t - g.t0
      return g.s0 + g.r * tau + (g.k / 4) * tau * tau
    }
  }
  return cm.sEnd + (t - cm.D) * cm.rEnd
}

/** `speed_at`: the speed at output seconds t. */
export function speedAt(cm: CurveMap, t: number): number {
  for (const g of cm.segs) if (t < g.t1) return g.r + (g.k / 2) * (t - g.t0)
  return cm.rEnd
}

/** `frame_map.curve_retimer`: rebased input ticks → output ticks on a stream
 *  in time base `tb` (T = PTS·TB in double, the curve, /TB, truncated). */
export function curveRetimer(cm: CurveMap, tb: { num: number; den: number }): (x: number) => number {
  const TB = tb.num / tb.den
  return (x: number) => Math.trunc(outSeconds(cm, x * TB) / TB)
}
