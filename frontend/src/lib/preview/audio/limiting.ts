// WHERE THE MASTER LIMITER WORKS (wave E gate RX, finding 3) — for the
// FALLBACK limiter only. The client's limiter is a port of ffmpeg's
// `alimiter` in an AudioWorklet (alimiter.ts, limiterWorklet.ts; P2 limiter
// tail, 0.8.0 final QA), the server's sample for sample, and a plan made for
// it has no ranges (AudioPlanOptions.exactLimiter). Where AudioWorklet is
// missing it is a DynamicsCompressorNode (mixGraph.ts), and the rest of this
// note applies: the server's limiter is ffmpeg's `alimiter`. Below the
// ceiling both are transparent and the offline mix is the server's sample
// for sample; once summed lanes push the master over it, their attack/
// release envelopes differ (measured: |Δ| up to 0.254, 0.75 dB per 50 ms
// block, and through the compressor's release up to ~91 ms after the last
// sample over the ceiling — mix_hot 3.20 s, which a test rule of "server
// near full scale" once called EXACT; see LIMITING_SPREAD). So the
// plan bounds the PRE-LIMITER peak of every stretch of the programme from the
// sources' recorded chunk peaks (index.json `audio.chunk_peak`) and calls a
// stretch whose bound tops the ceiling APPROX (`AudioPlan.limiting`, reason
// `limiting`; support.ts `audio:limiting`). The bound is the triangle
// inequality — master gain × Σ bus gain × clip gain × envelope max × source
// peak — so it can only flag too much, never too little. A clip whose peak is
// not known yet (the layout has not loaded, or it has no `chunk_peak`) is
// unbounded. A voice effect's stages can add gain: its clip is bounded by
// voicePeakBound (per stage — echo taps, filter L1 norms, the drive's
// ceiling, the reverb's impulse response) of its source peak, its priming
// included (K2, 0.8.0 QA: it was unbounded outright, and every voice effect
// on a project with a loudness target said "≈ Limiter on loud sound").

import type { BusPlan, ClipAudio } from './audioPlan'
import type { CompiledEnv } from './curves'
import { frameOf, type FpsLike } from '../timeline/timebase'
import { voicePeakBound } from '../../voice/voiceFx'

/** Samples either side of a stretch over the ceiling that the limiters'
 *  envelopes still touch: alimiter's 5 ms attack look-ahead and its linear
 *  50 ms release, the compressor's 6 ms look-ahead and its own release
 *  curve — 100 ms. Measured (P2 limiter tail, Chromium and WebKit, 0.5-30 dB
 *  over the ceiling for 20 ms-2 s, tones and click bursts, then a tone at 0.3
 *  or 0.9 of the ceiling): the two agree within 1e-4 per sample at most
 *  90.6 ms after the last sample over the ceiling (within 0.25 dB per 10 ms
 *  at most 50.3 ms), and part at most 0.6 ms before the first; on the real
 *  renders of tests/wk/test_wk_audio.py a range still runs ≥ 19.8 ms past its
 *  last differing sample. Only the DynamicsCompressorNode FALLBACK needs
 *  these ranges: the alimiter worklet (limiterWorklet.ts) is the server's
 *  limiter, and a plan made for it (`exactLimiter`) has none. */
export const LIMITING_SPREAD = 4800
/** A resampled clip (varispeed, atempo, a speed curve) can overshoot its
 *  source's sample peak (windowed sinc / WSOLA): +1 dB of margin. */
const RESAMPLE_OVERSHOOT = 1.122

/** The max |x| of source `src`'s samples [s0, s1) (chunk resolution, any
 *  channel, the headroom gain undone); 0 for a silent source; null when not
 *  known. */
export type PeakLookup = (src: string, s0: number, s1: number) => number | null

/** The largest linear factor an envelope applies (its dB points bound it). */
export function envMaxLinear(env: CompiledEnv | null): number {
  if (!env) return 1
  let db = env.last
  if (env.first) db = Math.max(db, env.first.v)
  for (const s of env.segs) db = Math.max(db, s.kind === 'const' ? s.v : Math.max(s.v0, s.v0 + s.dv))
  return Math.pow(10, db / 20)
}

/** Source samples [lo, hi) a clip may read. */
export function clipSourceSpan(c: ClipAudio): [number, number] {
  const m = c.map
  if (m.kind === 'runs') {
    let lo = Infinity, hi = -Infinity
    for (const [, cnt, first, dir] of m.runs) {
      if (cnt <= 0) continue
      const last = first + dir * (cnt - 1)
      lo = Math.min(lo, first, last)
      hi = Math.max(hi, first, last)
    }
    return lo <= hi ? [Math.max(0, lo), hi + 1] : [0, 0]
  }
  if (m.kind === 'curve') return [Math.max(0, Math.floor(m.src0) - 1), Math.min(m.end, Math.ceil(m.src0 + m.seconds * 48000) + 3)]
  const x1 = m.src0 + (m.reverse ? -1 : 1) * c.n * m.rate
  return [Math.max(0, Math.floor(Math.min(m.src0, x1)) - 2), Math.min(m.end, Math.ceil(Math.max(m.src0, x1)) + 3)]
}

/** Upper bound of a clip's contribution to the master (before the master
 *  gain); Infinity when it cannot be bounded. */
export function clipPeakBound(c: ClipAudio, busGain: number, peak: PeakLookup): number {
  if (c.mute || c.gain <= 0 || busGain <= 0) return 0
  let [s0, s1] = clipSourceSpan(c)
  // a primed effect also reads real sound before the head (mixGraph.primeOf:
  // a forward run's `prime` samples)
  if (c.voice && c.map.kind === 'runs') s0 = Math.max(0, s0 - c.voice.prime)
  if (s1 <= s0) return 0
  const p = peak(c.src, s0, s1)
  if (p === null || !Number.isFinite(p)) return Infinity
  const input = p * (c.map.kind === 'runs' ? 1 : RESAMPLE_OVERSHOOT)
  // the effect runs on the retimed samples, before the clip's gain
  return busGain * c.gain * envMaxLinear(c.env) * (c.voice ? voicePeakBound(c.voice, input) : input)
}

/** Source grain a sample-exact clip is bounded at: 1 s, a divisor of the
 *  proxy index's 5 s `chunk_samples`, so a piece never straddles a chunk
 *  (any other chunk size only makes a piece's bound coarser, never low). */
export const PEAK_GRAIN = 48000

interface Piece { a: number; b: number; bound: number }

/**
 * A clip's contribution bound per stretch of OUTPUT samples (final sweep 2).
 * Bounding a clip once by its whole source span flagged a 200 s talking head
 * as "≈ Limiter on loud sound" end to end for one hot 5 s chunk. A
 * sample-exact (`runs`) clip without a voice effect maps each output sample
 * to one source sample, so it is cut where its source crosses a PEAK_GRAIN
 * boundary and each piece is bounded by its own source samples. A resample
 * (`rate`/`curve`) and a voice effect (echo/reverb tails carry a hot chunk
 * past its own samples) keep the whole-clip bound.
 */
export function clipPieces(c: ClipAudio, busGain: number, peak: PeakLookup): Piece[] {
  if (c.n <= 0) return []
  const whole = (): Piece[] => {
    const bound = clipPeakBound(c, busGain, peak)
    return bound > 0 ? [{ a: c.out0, b: c.out0 + c.n, bound }] : []
  }
  const m = c.map
  if (c.voice || m.kind !== 'runs' || m.runs.some(([, , , dir]) => dir !== 1 && dir !== -1 && dir !== 0)) return whole()
  if (c.mute || c.gain <= 0 || busGain <= 0) return []
  const k = busGain * c.gain * envMaxLinear(c.env)
  const out: Piece[] = []
  for (const [off, cnt, first, dir] of m.runs) {
    for (let i = 0; i < cnt;) {
      const src = first + dir * i
      const blk = Math.floor(src / PEAK_GRAIN)
      const j = dir === 0 ? cnt
        : dir > 0 ? Math.min(cnt, (blk + 1) * PEAK_GRAIN - first)
          : Math.min(cnt, first - blk * PEAK_GRAIN + 1)
      const last = first + dir * (j - 1)
      const lo = Math.max(0, Math.min(src, last)), hi = Math.max(src, last) + 1
      const p = hi > lo ? peak(c.src, lo, hi) : 0
      const bound = p === null || !Number.isFinite(p) ? Infinity : k * p
      const a = c.out0 + off + i, b = c.out0 + off + j
      const prev = out[out.length - 1]
      if (prev && prev.b === a && prev.bound === bound) prev.b = b
      else if (bound > 0) out.push({ a, b, bound })
      i = Math.max(j, i + 1)
    }
  }
  return out
}

/** `limiting` sample ranges as output FRAME ranges [k0, k1) for support.ts
 *  (one frame of margin each side: an output frame's samples straddle). */
export function limitingFrames(ranges: ReadonlyArray<readonly [number, number]>, fps: FpsLike): Array<[number, number]> {
  return ranges.map(([a, b]) => [Math.max(0, frameOf(a / 48000, fps) - 1), frameOf(b / 48000, fps) + 1])
}

/** Output-sample ranges (merged, widened by LIMITING_SPREAD, inside
 *  [0, total)) where the bound on the pre-limiter peak tops the ceiling.
 *  With `post` (a mixed programme's loudness stage, MasterPlan.post) a
 *  stretch is also flagged where that stage's input — the first limiter's
 *  output, at most its ceiling — times the post gain tops ITS ceiling; the
 *  first stage is checked on its own, so a negative loudness gain can no
 *  longer hide a mix the server's 0.97 limiter works on (final QA). */
export function limitingRanges(clips: readonly ClipAudio[], buses: readonly BusPlan[], masterGain: number,
                               ceilingDb: number | null, total: number, peak: PeakLookup,
                               post?: { gain: number; ceilingDb: number }): Array<[number, number]> {
  if (ceilingDb === null || total <= 0) return []
  const ceiling = Math.pow(10, ceilingDb / 20)
  const busGain = new Map(buses.map((b) => [b.id, b.gain]))
  const live = clips.flatMap((c) => clipPieces(c, busGain.get(c.bus) ?? 1, peak))
  const cuts = [...new Set(live.flatMap((x) => [x.a, x.b]))].sort((x, y) => x - y)
  // Σ of the bounds live over each stretch between cuts: a sweep (a long
  // clip is many pieces now), an unbounded piece counted apart from the sum.
  const at = new Map(cuts.map((t, i) => [t, i]))
  const dSum = new Float64Array(cuts.length + 1)
  const dInf = new Int32Array(cuts.length + 1)
  for (const x of live) {
    const i = at.get(x.a)!, j = at.get(x.b)!
    if (x.bound === Infinity) { dInf[i]++; dInf[j]-- } else { dSum[i] += x.bound; dSum[j] -= x.bound }
  }
  const over: Array<[number, number]> = []
  let sum = 0, inf = 0
  for (let i = 0; i + 1 < cuts.length; i++) {
    sum += dSum[i]; inf += dInf[i]
    const a = cuts[i], b = cuts[i + 1]
    const level = masterGain * sum
    const hot = inf > 0 || level > ceiling
      || (post !== undefined && post.gain * Math.min(level, ceiling) > Math.pow(10, post.ceilingDb / 20))
    if (hot) over.push([a - LIMITING_SPREAD, b + LIMITING_SPREAD])
  }
  const out: Array<[number, number]> = []
  for (const [a0, b0] of over) {
    const a = Math.max(0, a0), b = Math.min(total, b0)
    if (b <= a) continue
    const last = out[out.length - 1]
    if (last && a <= last[1]) last[1] = Math.max(last[1], b)
    else out.push([a, b])
  }
  return out
}
