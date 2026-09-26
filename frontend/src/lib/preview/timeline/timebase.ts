// The project timebase — a line-for-line port of `edl/timebase.py` (instant
// preview spec §6 R2). It is the ONE place the frontend turns seconds into
// frames and back; `lib/frameStep.ts` and `lib/overlayGate.ts` delegate here.
//
// WHY RATIONAL AND WHY BigInt. The server does this arithmetic on Python
// `Fraction`s of the EXACT binary value of each float: `frame_of(t)` is
// `round(Fraction(t) * rate)` with round-half-EVEN, and `rate_of(29.97…)` is a
// continued-fraction `limit_denominator(1001)`. `Math.round(t * fps)` differs
// from that on exact ties (0.75 s at 30 fps is frame 22 on the server, 23 in
// `Math.round`) and on products that float multiplication rounds across a
// half. One frame of disagreement is exactly the class of bug this port
// exists to remove, so every function below reproduces the Python result
// bit for bit (pinned by tests/goldens/timebase_cases.json).

export interface Rational { num: number; den: number }

/** A Rational whose numerator/denominator are exact integers (bigint). */
interface Q { n: bigint; d: bigint }

const Q_ZERO: Q = { n: 0n, d: 1n }

function gcd(a: bigint, b: bigint): bigint {
  a = a < 0n ? -a : a
  b = b < 0n ? -b : b
  while (b) [a, b] = [b, a % b]
  return a
}

function q(n: bigint, d: bigint = 1n): Q {
  if (d < 0n) { n = -n; d = -d }
  const g = gcd(n, d) || 1n
  return { n: n / g, d: d / g }
}

const qMul = (a: Q, b: Q): Q => q(a.n * b.n, a.d * b.d)
const qDiv = (a: Q, b: Q): Q => q(a.n * b.d, a.d * b.n)
const qAdd = (a: Q, b: Q): Q => q(a.n * b.d + b.n * a.d, a.d * b.d)
const qSub = (a: Q, b: Q): Q => q(a.n * b.d - b.n * a.d, a.d * b.d)
const qCmp = (a: Q, b: Q): number => {
  const l = a.n * b.d
  const r = b.n * a.d
  return l < r ? -1 : l > r ? 1 : 0
}
const qAbs = (a: Q): Q => (a.n < 0n ? { n: -a.n, d: a.d } : a)
const qToNumber = (a: Q): number => correctlyRoundedDiv(a.n, a.d)

/** floor(n / d) for bigint (d > 0). */
function floorDiv(n: bigint, d: bigint): bigint {
  const qq = n / d
  return (n % d !== 0n && (n < 0n) !== (d < 0n)) ? qq - 1n : qq
}

/** Python `round(Fraction)`: nearest integer, ties to EVEN. */
export function roundHalfEvenQ(a: Q): bigint {
  const fl = floorDiv(a.n, a.d)
  const rem2 = 2n * (a.n - fl * a.d)       // 2·(a − floor(a))·d, in [0, 2d)
  if (rem2 > a.d) return fl + 1n
  if (rem2 < a.d) return fl
  return fl % 2n === 0n ? fl : fl + 1n
}

/** libavutil AV_ROUND_NEAR_INF: nearest, ties away from zero. */
export function roundHalfAwayQ(a: Q): bigint {
  if (a.n >= 0n) return floorDiv(2n * a.n + a.d, 2n * a.d)
  return -floorDiv(-2n * a.n + a.d, 2n * a.d)
}

// ---------------------------------------------------------------- floats

const F64 = new Float64Array(1)
const U32 = new Uint32Array(F64.buffer)
const LITTLE = new Uint8Array(new Uint16Array([1]).buffer)[0] === 1

/** The EXACT value of a finite double as a rational (Python `Fraction(x)`). */
export function floatToQ(x: number): Q {
  if (!Number.isFinite(x)) throw new RangeError(`not a finite number: ${x}`)
  if (x === 0) return Q_ZERO
  if (Number.isInteger(x) && Number.isSafeInteger(x)) return { n: BigInt(x), d: 1n }
  F64[0] = x
  const hi = U32[LITTLE ? 1 : 0]
  const lo = U32[LITTLE ? 0 : 1]
  const sign = hi >>> 31 ? -1n : 1n
  const exp = (hi >>> 20) & 0x7ff
  let mant = (BigInt(hi & 0xfffff) << 32n) | BigInt(lo)
  let e: number
  if (exp === 0) e = -1074                 // subnormal
  else { mant |= 1n << 52n; e = exp - 1075 }
  return e >= 0 ? q(sign * (mant << BigInt(e))) : q(sign * mant, 1n << BigInt(-e))
}

const bitLength = (x: bigint): number => x.toString(2).length

/** n/d rounded to the nearest double (ties even), like Python's int/int. */
function correctlyRoundedDiv(n: bigint, d: bigint): number {
  if (d < 0n) { n = -n; d = -d }
  const neg = n < 0n
  if (neg) n = -n
  if (n === 0n) return 0
  // Enough quotient bits for 53 significant bits + a round bit, plus sticky.
  const s = Math.max(0, 55 - (bitLength(n) - bitLength(d)))
  const num = n << BigInt(s)
  const quo = num / d
  const sticky = num % d !== 0n
  const drop = bitLength(quo) - 53
  let m = quo >> BigInt(drop)
  const rem = quo & ((1n << BigInt(drop)) - 1n)
  const half = 1n << BigInt(drop - 1)
  if (rem > half || (rem === half && (sticky || (m & 1n) === 1n))) m += 1n
  const val = Number(m) * 2 ** (drop - s)
  return neg ? -val : val
}

/** Python `f"{x:.6f}"` parsed back as ffmpeg's microseconds (half-even on
 *  the exact binary value — `toFixed` rounds exact ties up and differs). */
export function ffmpegMicros(x: number): number {
  return Number(roundHalfEvenQ(qMul(floatToQ(x), { n: 1_000_000n, d: 1n })))
}

// ---------------------------------------------------------------- rates

/** The rates a real camera, phone or NLE produces (timebase.STANDARD_RATES). */
export const STANDARD_RATES: readonly Rational[] = [
  { num: 24000, den: 1001 }, { num: 24, den: 1 }, { num: 25, den: 1 },
  { num: 30000, den: 1001 }, { num: 30, den: 1 },
  { num: 48, den: 1 }, { num: 50, den: 1 },
  { num: 60000, den: 1001 }, { num: 60, den: 1 },
]

export const DEFAULT_RATE: Rational = { num: 30, den: 1 }
const SNAP_TOLERANCE = 0.001

const toQ = (r: Rational): Q => ({ n: BigInt(r.num), d: BigInt(r.den) })
const fromQ = (a: Q): Rational => ({ num: Number(a.n), den: Number(a.d) })

/** Python `Fraction.limit_denominator(maxDen)`. */
function limitDenominator(a: Q, maxDen: bigint): Q {
  if (a.d <= maxDen) return a
  let p0 = 0n, q0 = 1n, p1 = 1n, q1 = 0n
  let n = a.n, d = a.d
  for (;;) {
    const k = floorDiv(n, d)
    const q2 = q0 + k * q1
    if (q2 > maxDen) break
    ;[p0, q0, p1, q1] = [p1, q1, p0 + k * p1, q2]
    ;[n, d] = [d, n - k * d]
  }
  const k = floorDiv(maxDen - q0, q1)
  const bound1 = q(p0 + k * p1, q0 + k * q1)
  const bound2 = q(p1, q1)
  return qCmp(qAbs(qSub(bound2, a)), qAbs(qSub(bound1, a))) <= 0 ? bound2 : bound1
}

export type FpsLike = number | Rational | null | undefined

function isRational(x: unknown): x is Rational {
  return typeof x === 'object' && x !== null && 'num' in x && 'den' in x
}

/** `rate_of`: the exact frame rate a stored fps stands for — the nearest
 *  standard rate within 0.1 %, else `limit_denominator(1001)`; missing,
 *  non-finite or non-positive → 30. */
export function rateOf(fps: FpsLike): Rational {
  if (fps === null || fps === undefined) return DEFAULT_RATE
  const key = isRational(fps) ? `${fps.num}/${fps.den}` : typeof fps === 'number' ? String(fps) : null
  if (key === null) return rateOfExact(fps)
  let r = RATE_CACHE.get(key)
  if (r === undefined) {
    r = rateOfExact(fps)
    if (RATE_CACHE.size >= RATE_CACHE_MAX) RATE_CACHE.clear()
    RATE_CACHE.set(key, r)
  }
  return r
}

// rateOf is on the per-frame path (overlay gates call frameOf/timeOf for
// every overlay on every rAF while playing), and a project has one or two
// distinct fps values: memoise the exact result per input. Bounded, and the
// cached value is exactly what rateOfExact returns, so nothing can drift.
const RATE_CACHE = new Map<string, Rational>()
const RATE_CACHE_MAX = 64

function rateOfExact(fps: FpsLike): Rational {
  if (fps === null || fps === undefined) return DEFAULT_RATE
  let value: Q
  if (isRational(fps)) {
    if (!fps.den) return DEFAULT_RATE
    value = q(BigInt(fps.num), BigInt(fps.den))
  } else {
    if (typeof fps !== 'number' || !Number.isFinite(fps)) return DEFAULT_RATE
    value = limitDenominator(floatToQ(fps), 1001n)
  }
  if (value.n <= 0n) return DEFAULT_RATE
  const v = qToNumber(value)
  let best: Rational | null = null
  let bestErr = Infinity
  for (const std of STANDARD_RATES) {
    const s = std.num / std.den
    const err = Math.abs(v - s)
    if (err <= s * SNAP_TOLERANCE && err < bestErr) { best = std; bestErr = err }
  }
  return best ?? fromQ(value)
}

const rateQ = (fps: FpsLike): Q => toQ(rateOf(fps))

export const sameRate = (a: Rational, b: Rational): boolean => a.num * b.den === b.num * a.den

/** `fps_float`: the rate as a float for storage/display. */
export function fpsFloat(fps: FpsLike): number {
  const r = rateOf(fps)
  return r.num / r.den
}

/** `ffmpeg_rate`: "30" or "30000/1001". */
export function ffmpegRate(fps: FpsLike): string {
  const r = rateOf(fps)
  return r.den === 1 ? String(r.num) : `${r.num}/${r.den}`
}

/** `frame_duration`: seconds per frame. */
export function frameDuration(fps: FpsLike): number {
  const r = rateOf(fps)
  return r.den / r.num
}

/** Distance from a half-integer below which frameOf takes the exact path.
 *  `t·num/den` in floats is off from the exact product by < 3 ulp; at any
 *  frame index a timeline reaches (< 2^31) that is < 1e-6, so a float result
 *  this far from a tie rounds to the same integer as the exact rational. */
const TIE_MARGIN = 1e-6
const FAST_LIMIT = 2 ** 31

/** `frame_of`: the frame index nearest to `t` (ties to even, on the exact
 *  value of `t`); t <= 0, null or non-finite → 0. */
export function frameOf(t: number | null | undefined, fps: FpsLike): number {
  if (t === null || t === undefined || !(t > 0) || !Number.isFinite(t)) return 0
  const r = rateOf(fps)
  const x = (t * r.num) / r.den
  if (x < FAST_LIMIT && Math.abs(x - Math.floor(x) - 0.5) > TIE_MARGIN) return Math.round(x)
  return frameOfExact(t, r)
}

function frameOfExact(t: number, r: Rational): number {
  return Number(roundHalfEvenQ(qMul(floatToQ(t), toQ(r))))
}

/** `time_of`: the exact start time of `frame` (correctly rounded). */
export function timeOf(frame: number, fps: FpsLike): number {
  if (frame <= 0) return 0
  const r = rateOf(fps)
  const n = frame * r.den
  // Both operands exact integers in a double: IEEE division IS the correctly
  // rounded quotient, bit-identical to the BigInt path.
  if (Number.isSafeInteger(n) && Number.isSafeInteger(frame)) return n / r.num
  return correctlyRoundedDiv(BigInt(frame) * BigInt(r.den), BigInt(r.num))
}

/** `quantize`: `t` snapped to the nearest frame boundary. */
export function quantize(t: number, fps: FpsLike): number {
  return timeOf(frameOf(t, fps), fps)
}

/** `frames_between`: whole frames in [start, end); never negative. */
export function framesBetween(start: number, end: number, fps: FpsLike): number {
  return Math.max(0, frameOf(end, fps) - frameOf(start, fps))
}

/** `floor_to_frame`: the last boundary at or before `t` (1 µs tolerance). */
export function floorToFrame(t: number | null | undefined, fps: FpsLike): number {
  if (t === null || t === undefined || !(t > 0)) return 0
  const x = qAdd(qMul(floatToQ(t), rateQ(fps)), { n: 1n, d: 1_000_000n })
  return timeOf(Number(floorDiv(x.n, x.d)), fps)
}

/** `ceil_to_frame`: the first boundary at or after `t` (1 µs tolerance). */
export function ceilToFrame(t: number | null | undefined, fps: FpsLike): number {
  if (t === null || t === undefined || !(t > 0)) return 0
  const x = qSub(qMul(floatToQ(t), rateQ(fps)), { n: 1n, d: 1_000_000n })
  return timeOf(Number(-floorDiv(-x.n, x.d)), fps)
}

/** `samples_for_frames`: audio samples spanning exactly `frames` frames
 *  (nearest sample, ties even). */
export function samplesForFrames(frames: number, fps: FpsLike, sampleRate = 48000): number {
  if (frames <= 0) return 0
  const r = rateOf(fps)
  return Number(roundHalfEvenQ(q(BigInt(frames) * BigInt(sampleRate) * BigInt(r.den), BigInt(r.num))))
}

/** `seek_preroll`: half a frame before edit point `t` (never below 0). */
export function seekPreroll(t: number | null | undefined, fps: FpsLike): number {
  if (t === null || t === undefined || !(t > 0)) return 0
  return Math.min(t, frameDuration(fps) / 2)
}

/** `enable_window`: `[lo, hi)` gate bounds half a frame before each
 *  boundary frame. */
export function enableWindow(start: number, end: number, fps: FpsLike): [number, number] {
  const half = frameDuration(fps) / 2
  const lo = timeOf(frameOf(start, fps), fps) - half
  const hi = timeOf(frameOf(end, fps), fps) - half
  return [lo, Math.max(lo, hi)]
}

// ---------------------------------------------------------------- source_rate

function parseRate(x: number | string | Rational | null | undefined): Q | null {
  if (x === null || x === undefined) return null
  try {
    let v: Q
    if (typeof x === 'string') {
      const text = x.trim()
      if (text.includes('/')) {
        const [a, b] = text.split('/', 2)
        const bq = decimalToQ(b)
        if (bq.n === 0n) return null
        v = qDiv(decimalToQ(a), bq)
      } else v = decimalToQ(text)
    } else if (isRational(x)) {
      if (!x.den) return null
      v = q(BigInt(x.num), BigInt(x.den))
    } else {
      if (!Number.isFinite(x)) return null
      v = floatToQ(x)
    }
    return v.n > 0n ? v : null
  } catch {
    return null
  }
}

/** Python `Fraction("12.5")` for a decimal string (exact). */
function decimalToQ(text: string): Q {
  const m = /^\s*([+-]?)(\d*)(?:\.(\d*))?(?:[eE]([+-]?\d+))?\s*$/.exec(text)
  if (!m || (!m[2] && !m[3])) throw new SyntaxError(`invalid literal: ${text}`)
  const digits = (m[2] || '') + (m[3] || '')
  const exp = -(m[3] || '').length + (m[4] ? Number(m[4]) : 0)
  let n = BigInt(digits || '0')
  let d = 1n
  if (exp >= 0) n *= 10n ** BigInt(exp)
  else d = 10n ** BigInt(-exp)
  return q(m[1] === '-' ? -n : n, d)
}

function standardOrNull(v: Q | null): Rational | null {
  if (v === null) return null
  const snapped = rateOf(fromQSafe(v))
  return STANDARD_RATES.some((s) => sameRate(s, snapped)) ? snapped : null
}

/** A Q as a Rational when it fits in doubles, for rateOf's float path. */
function fromQSafe(v: Q): Rational | number {
  if (v.n <= BigInt(Number.MAX_SAFE_INTEGER) && v.d <= BigInt(Number.MAX_SAFE_INTEGER)) return fromQ(v)
  return qToNumber(v)
}

/** `source_rate`: the CFR rate a SOURCE is normalised to (see timebase.py). */
export function sourceRate(avgFps: number | string | Rational | null | undefined,
                           nominalFps: number | string | Rational | null | undefined = null): Rational {
  let avg = parseRate(avgFps)
  const nominal = parseRate(nominalFps)
  if (avg === null && nominal === null) return DEFAULT_RATE
  if (avg === null) avg = nominal!
  const avgStd = standardOrNull(avg)
  const nomStd = standardOrNull(nominal)
  const f = (r: Rational) => r.num / r.den
  if (avgStd && nomStd && Math.abs(f(avgStd) - f(nomStd)) <= f(nomStd) * 0.002) return nomStd
  if (avgStd) return avgStd
  const avgF = qToNumber(avg)
  if (nominal !== null && avgF >= 1 && avgF <= 240) {
    const nomF = qToNumber(nominal)
    if (Math.abs(avgF - nomF) <= nomF * SNAP_TOLERANCE) return rateOf(fromQSafe(avg))
  }
  if (nomStd && avgF <= f(nomStd) * (1 + SNAP_TOLERANCE)) return nomStd
  let best = STANDARD_RATES[0]
  for (const s of STANDARD_RATES) if (Math.abs(f(s) - avgF) < Math.abs(f(best) - avgF)) best = s
  return best
}

// ---------------------------------------------------------------- MSE grid (R1)

/** The media timescale of every init and fragment the client writes (R1). */
export const MSE_TIMESCALE = 240000

/** Ticks per frame at an EXACT `rate` in MSE_TIMESCALE, or null when it is
 *  not an integer — the R1 refusal: such a timeline stays in server mode.
 *  The one implementation; media/fmp4Writer and timeline/frameMap use it. */
export function ticksPerFrameExact(rate: Rational): number | null {
  const { num, den } = rate
  if (!Number.isInteger(num) || !Number.isInteger(den) || num <= 0 || den <= 0) return null
  const scaled = MSE_TIMESCALE * den
  return scaled % num === 0 ? scaled / num : null
}

/** `ticksPerFrameExact` of the project rate a stored fps stands for. */
export function ticksPerFrame(fps: FpsLike): number | null {
  return ticksPerFrameExact(rateOf(fps))
}

// ---------------------------------------------------------------- exact helpers
// Exported for frameMap.ts: integer rescaling with libavutil rounding.

/** round(a·b / c), ties away from zero, exact for any integers. */
export function mulDivRoundAway(a: number, b: number, c: number): number {
  const p = a * b
  if (Number.isSafeInteger(p) && Number.isSafeInteger(c) && c > 0) {
    const neg = p < 0
    const ap = neg ? -p : p
    let qq = Math.floor(ap / c)
    let r = ap - qq * c
    while (r < 0) { qq -= 1; r += c }
    while (r >= c) { qq += 1; r -= c }
    if (2 * r >= c) qq += 1
    return neg ? -qq : qq
  }
  return Number(roundHalfAwayQ(q(BigInt(a) * BigInt(b), BigInt(c))))
}

/** `av_rescale_q(a, from, to)` (AV_ROUND_NEAR_INF). */
export function rescale(a: number, from: Rational, to: Rational): number {
  return mulDivRoundAway(a, from.num * to.den, from.den * to.num)
}

export const _internal = {
  floatToQ, correctlyRoundedDiv, limitDenominator, decimalToQ, rateOfExact,
  /** The BigInt-only frame_of, for the fast path's parity test. */
  frameOfExact: (t: number, fps: FpsLike) =>
    (!(t > 0) || !Number.isFinite(t) ? 0 : frameOfExact(t, rateOfExact(fps))),
}
