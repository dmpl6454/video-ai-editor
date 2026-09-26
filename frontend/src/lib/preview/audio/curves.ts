// Gain SHAPES of the export's sound, sample for sample (instant preview spec
// §6 R10): ffmpeg's `afade` curves, `acrossfade`'s default overlap, the
// `volume=…:eval=frame` expression a clip's gain envelope becomes
// (`audio_mix.gain_env_filter` → `edl/keyframes.to_ffmpeg_expr`), and the
// `volume=<g>dB` clip gain — including how each number is FORMATTED into the
// filter text (`%.3f`, `%.4f`, `%.6f`, `%.2f`), because ffmpeg only ever sees
// the text. Pinned by tests/goldens/audio_curve_cases.json, which is rendered
// by real ffmpeg (tests/gen_audio_curve_goldens.py).

import { floatToQ, rescale, roundHalfEvenQ, type Rational } from '../timeline/timebase'

export const SAMPLE_RATE = 48000

/** `av_q2d(1/48000)`: ffmpeg's `t` of sample `i` is `i · (1/48000)` (a
 *  product, not `i / 48000`; they differ in the last bit, which decides a
 *  `lt(t, 0.4)` at sample 19200). */
export const SAMPLE_PERIOD = 1 / SAMPLE_RATE
export const sampleTime = (i: number): number => i * SAMPLE_PERIOD

const SR_TB: Rational = { num: 1, den: SAMPLE_RATE }
const US_TB: Rational = { num: 1, den: 1_000_000 }

/** Every `afade` curve ffmpeg 8 has (af_afade.c). The export emits only `tri`. */
export type FadeCurve =
  | 'tri' | 'qsin' | 'esin' | 'hsin' | 'log' | 'ipar' | 'qua' | 'cub' | 'squ' | 'cbr' | 'par' | 'exp'
  | 'iqsin' | 'ihsin' | 'dese' | 'desi' | 'losi' | 'sinc' | 'isinc' | 'quat' | 'quatr' | 'qsin2'
  | 'hsin2' | 'nofade'

export const FADE_CURVES: readonly FadeCurve[] = [
  'tri', 'qsin', 'esin', 'hsin', 'log', 'ipar', 'qua', 'cub', 'squ', 'cbr', 'par', 'exp', 'iqsin',
  'ihsin', 'dese', 'desi', 'losi', 'sinc', 'isinc', 'quat', 'quatr', 'qsin2', 'hsin2', 'nofade',
]

const cube = (a: number) => a * a * a
const LOSI_A = 1 / (1 - 0.787) - 1

/** af_afade.c `fade_gain(curve, index, range, silence=0, unity=1)`. */
export function fadeGain(curve: FadeCurve, index: number, range: number): number {
  let g = range > 0 ? Math.min(1, Math.max(0, index / range)) : 1
  switch (curve) {
    case 'qsin': g = Math.sin(g * Math.PI / 2); break
    case 'iqsin': g = 0.6366197723675814 * Math.asin(g); break
    case 'esin': g = 1 - Math.cos(Math.PI / 4 * (cube(2 * g - 1) + 1)); break
    case 'hsin': g = (1 - Math.cos(g * Math.PI)) / 2; break
    case 'ihsin': g = 0.3183098861837907 * Math.acos(1 - 2 * g); break
    case 'exp': g = Math.exp(-11.512925464970227 * (1 - g)); break
    case 'log': g = Math.min(1, Math.max(0, 1 + 0.2 * Math.log10(g))); break
    case 'par': g = 1 - Math.sqrt(1 - g); break
    case 'ipar': g = 1 - (1 - g) * (1 - g); break
    case 'qua': g = g * g; break
    case 'cub': g = cube(g); break
    case 'squ': g = Math.sqrt(g); break
    case 'cbr': g = Math.cbrt(g); break
    case 'dese': g = g <= 0.5 ? Math.cbrt(2 * g) / 2 : 1 - Math.cbrt(2 * (1 - g)) / 2; break
    case 'desi': g = g <= 0.5 ? cube(2 * g) / 2 : 1 - cube(2 * (1 - g)) / 2; break
    case 'losi': {
      const A = 1 / (1 + Math.exp(-((g - 0.5) * LOSI_A * 2)))
      const B = 1 / (1 + Math.exp(LOSI_A))
      const C = 1 / (1 + Math.exp(-LOSI_A))
      g = (A - B) / (C - B)
      break
    }
    case 'sinc': g = g >= 1 ? 1 : Math.sin(Math.PI * (1 - g)) / (Math.PI * (1 - g)); break
    case 'isinc': g = g <= 0 ? 0 : 1 - Math.sin(Math.PI * g) / (Math.PI * g); break
    case 'quat': g = g * g * g * g; break
    case 'quatr': g = Math.pow(g, 0.25); break
    case 'qsin2': g = Math.sin(g * Math.PI / 2) * Math.sin(g * Math.PI / 2); break
    case 'hsin2': g = Math.pow((1 - Math.cos(g * Math.PI)) / 2, 2); break
    case 'nofade': g = 1; break
    default: break                                   // tri
  }
  return g
}

// ---------------------------------------------------------------- number text

/** Python `f"{x:.<digits>f}"` as the exact decimal it denotes, as integer
 *  units of 10^-digits (half-even on the exact binary value; `toFixed`
 *  rounds exact ties up: 0.0625 → Python "0.062", JS "0.063"). */
export function pyFixedUnits(x: number, digits: number): number {
  const q = floatToQ(x)
  const scale = 10n ** BigInt(digits)
  return Number(roundHalfEvenQ({ n: q.n * scale, d: q.d }))
}

/** Python `f"{x:.<digits>f}"`. */
export function pyFixed(x: number, digits: number): string {
  const u = pyFixedUnits(x, digits)
  const neg = u < 0 || Object.is(u, -0) || (u === 0 && x < 0)
  const a = Math.abs(u)
  const s = String(a).padStart(digits + 1, '0')
  const body = digits ? `${s.slice(0, s.length - digits)}.${s.slice(s.length - digits)}` : s
  return neg ? `-${body}` : body
}

/** The double ffmpeg parses from Python `f"{x:.<digits>f}"`. */
export const pyFixedValue = (x: number, digits: number): number => Number(pyFixed(x, digits))

/** Python's `round(x)` on a float: nearest integer, exact ties to even. */
export const pyRound = (x: number): number => Number(roundHalfEvenQ(floatToQ(x)))

/** Output samples of an ffmpeg time option given as µs (`av_rescale`,
 *  nearest, ties away). */
export const microsToSamples = (us: number): number => rescale(us, US_TB, SR_TB)

// ---------------------------------------------------------------- afade

export interface FadeWindow {
  type: 'in' | 'out'
  curve: FadeCurve
  /** First sample of the ramp, on the stream's own sample clock. */
  start: number
  /** Ramp length in samples (`nb_samples`). */
  range: number
}

/** `afade=t=<type>:st=<st>:d=<d>` with `st`/`d` as Python formats them at
 *  3 decimals: `start_sample = av_rescale(st_us, sr, 1e6)`, likewise `range`. */
export function afadeWindow(type: 'in' | 'out', st: number, d: number, curve: FadeCurve = 'tri'): FadeWindow {
  return {
    type, curve,
    start: microsToSamples(pyFixedUnits(st, 3) * 1000),
    range: microsToSamples(pyFixedUnits(d, 3) * 1000),
  }
}

/** The gain `afade` applies at `sample` (same clock as `w.start`): a fade-in
 *  is `fade_gain(i/range)` from its start (its floor before it), a fade-out
 *  `fade_gain((range − i)/range)` (its floor after it). */
export function afadeGainAt(w: FadeWindow, sample: number): number {
  if (w.curve === 'nofade') return 1                 // ffmpeg passes the stream through
  // Outside the ramp the index clips to [0, range] (the frame that straddles
  // the edge; whole frames beyond are true silence — the two differ only for
  // `exp`, by 1e-5).
  const i = sample - w.start
  if (w.type === 'in') return fadeGain(w.curve, i, w.range)
  return i < 0 ? 1 : fadeGain(w.curve, w.range - i, w.range)
}

/** The fades the export emits for a clip's audio props, and where. */
export interface ClipFadeProps {
  fade_in?: number
  fade_out?: number
}

/** `_audio_props_filters` (v1 and PiP chains): fades on CLIP-LOCAL samples,
 *  the fade-out from `effective_duration − fade_out`. */
export function clipFadeWindows(audio: ClipFadeProps | null | undefined, effectiveDuration: number): FadeWindow[] {
  const out: FadeWindow[] = []
  const fin = audio?.fade_in ?? 0
  const fout = audio?.fade_out ?? 0
  if (fin > 0.001) out.push(afadeWindow('in', 0, fin))
  if (fout > 0.001) out.push(afadeWindow('out', Math.max(0, effectiveDuration - fout), fout))
  return out
}

/** `audio_mix._audio_clip_filter` (music / voice-over / audio lanes): fades
 *  on the RENDER clock (absolute output samples), from the clip's render
 *  window `[rs, re)`. */
export function laneFadeWindows(audio: ClipFadeProps | null | undefined, rs: number, re: number): FadeWindow[] {
  const out: FadeWindow[] = []
  const fin = audio?.fade_in ?? 0
  const fout = audio?.fade_out ?? 0
  if (fin > 0.001) out.push(afadeWindow('in', rs, fin))
  if (fout > 0.001) out.push(afadeWindow('out', Math.max(0, re - fout), fout))
  return out
}

// ---------------------------------------------------------------- acrossfade

/** Overlap samples of `acrossfade=d=<cost as %.6f>`. */
export const acrossfadeSamples = (costSeconds: number): number =>
  costSeconds > 0 ? microsToSamples(pyFixedUnits(costSeconds, 6)) : 0

/** `acrossfade` default curves (tri, tri) at overlap index `i ∈ [0, m)`:
 *  the outgoing side is `(m − 1 − i)/m`, the incoming `i/m` (measured: the
 *  two do not sum to 1). */
export function acrossfadeGains(m: number, i: number): [number, number] {
  return [fadeGain('tri', m - 1 - i, m), fadeGain('tri', i, m)]
}

// ---------------------------------------------------------------- clip gain

/** `volume=<gain_db %.2f>dB` (emitted only when |gain_db| > 0.01), linear. */
export function clipGainLinear(gainDb: number | null | undefined): number {
  const g = gainDb ?? 0
  if (!(Math.abs(g) > 0.01)) return 1
  return Math.pow(10, pyFixedValue(g, 2) / 20)
}

// ---------------------------------------------------------------- gain_env

export type Interp = 'linear' | 'ease-in' | 'ease-out' | 'ease-in-out' | 'step' | 'back-out' | 'bounce'

export interface GainEnv {
  keyframes: Array<[number, number]> | Array<readonly [number, number]>
  interp?: Interp | string
}

interface EnvSegment { t1: number; kind: 'const'; v: number }
interface EnvRamp { t1: number; kind: 'ramp'; t0: number; dt: number; v0: number; dv: number; interp: string }

/** The compiled `to_ffmpeg_expr`: its constants exactly as the filter text
 *  carries them, evaluated in the expression's own order. */
export interface CompiledEnv {
  first: { t0: number; v: number } | null
  segs: Array<EnvSegment | EnvRamp>
  last: number
}

export function compileEnv(env: GainEnv | null | undefined): CompiledEnv | null {
  const kfs = env?.keyframes ?? []
  if (!kfs.length) return null
  const interp = env?.interp ?? 'linear'
  const pts = kfs.map(([t, v]) => [Number(t), Number(v)] as [number, number])
    .sort((a, b) => a[0] - b[0])
  const f4 = (x: number) => pyFixedValue(x, 4)
  if (pts.length === 1) return { first: null, segs: [], last: f4(pts[0][1]) }
  const segs: Array<EnvSegment | EnvRamp> = []
  for (let i = 1; i < pts.length; i++) {
    const [t0, v0] = pts[i - 1]
    const [t1, v1] = pts[i]
    const t1r = f4(t1)
    if (interp === 'step' || t1 - t0 < 1e-6) {
      segs.push({ t1: t1r, kind: 'const', v: interp === 'step' ? f4(v0) : f4(v1) })
    } else {
      segs.push({ t1: t1r, kind: 'ramp', t0: f4(t0), dt: pyFixedValue(t1 - t0, 6), v0: f4(v0), dv: f4(v1 - v0), interp })
    }
  }
  return { first: { t0: f4(pts[0][0]), v: f4(pts[0][1]) }, segs, last: f4(pts[pts.length - 1][1]) }
}

/** The envelope's dB offset at clip-local time `t` (the `volume` filter's
 *  `t`), as ffmpeg evaluates the emitted expression. */
export function envDbAt(c: CompiledEnv, t: number): number {
  if (c.first && t < c.first.t0) return c.first.v
  for (const s of c.segs) {
    if (!(t < s.t1)) continue
    if (s.kind === 'const') return s.v
    const f = (t - s.t0) / s.dt
    let p: number
    switch (s.interp) {
      case 'ease-in': p = Math.pow(f, 2); break
      case 'ease-out': p = 1 - Math.pow(1 - f, 2); break
      case 'ease-in-out': p = f * f * (3 - 2 * f); break
      case 'back-out': p = 1 - Math.pow(1 - f, 3); break
      default: p = f
    }
    return s.v0 + p * s.dv
  }
  return c.last
}

/** `pow(10, env(t)/20)`: the linear gain of the envelope at `t`. */
export const envGainAt = (c: CompiledEnv, t: number): number => Math.pow(10, envDbAt(c, t) / 20)
