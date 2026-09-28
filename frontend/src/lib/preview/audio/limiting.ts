// WHERE THE MASTER LIMITER WORKS (wave E gate RX, finding 3). The client's
// limiter is a DynamicsCompressorNode (mixGraph.ts); the export's is ffmpeg's
// `alimiter`. Below the ceiling both are transparent and the offline mix is
// the server's sample for sample; once summed lanes push the master over it,
// their attack/release envelopes differ (measured: |Δ| up to 0.254, 0.64 dB
// per 50 ms block, all within ±50 ms of a server sample over 0.95). So the
// plan bounds the PRE-LIMITER peak of every stretch of the programme from the
// sources' recorded chunk peaks (index.json `audio.chunk_peak`) and calls a
// stretch whose bound tops the ceiling APPROX (`AudioPlan.limiting`, reason
// `limiting`; support.ts `audio:limiting`). The bound is the triangle
// inequality — master gain × Σ bus gain × clip gain × envelope max × source
// peak — so it can only flag too much, never too little. A clip whose peak is
// not known yet (the layout has not loaded, or it has no `chunk_peak`) or
// that runs a voice effect (its stages can add gain) is unbounded.

import type { BusPlan, ClipAudio } from './audioPlan'
import type { CompiledEnv } from './curves'
import { frameOf, type FpsLike } from '../timeline/timebase'

/** Samples either side of a stretch over the ceiling that the limiters'
 *  envelopes still touch: alimiter's 5 ms attack look-ahead and 50 ms
 *  release, the compressor's 6 ms look-ahead and 50 ms release — 100 ms. */
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
  if (c.voice) return Infinity
  const [s0, s1] = clipSourceSpan(c)
  if (s1 <= s0) return 0
  const p = peak(c.src, s0, s1)
  if (p === null || !Number.isFinite(p)) return Infinity
  return busGain * c.gain * envMaxLinear(c.env) * p * (c.map.kind === 'runs' ? 1 : RESAMPLE_OVERSHOOT)
}

/** `limiting` sample ranges as output FRAME ranges [k0, k1) for support.ts
 *  (one frame of margin each side: an output frame's samples straddle). */
export function limitingFrames(ranges: ReadonlyArray<readonly [number, number]>, fps: FpsLike): Array<[number, number]> {
  return ranges.map(([a, b]) => [Math.max(0, frameOf(a / 48000, fps) - 1), frameOf(b / 48000, fps) + 1])
}

/** Output-sample ranges (merged, widened by LIMITING_SPREAD, inside
 *  [0, total)) where the bound on the pre-limiter peak tops the ceiling. */
export function limitingRanges(clips: readonly ClipAudio[], buses: readonly BusPlan[], masterGain: number,
                               ceilingDb: number | null, total: number, peak: PeakLookup): Array<[number, number]> {
  if (ceilingDb === null || total <= 0) return []
  const ceiling = Math.pow(10, ceilingDb / 20)
  const busGain = new Map(buses.map((b) => [b.id, b.gain]))
  const live: Array<{ a: number; b: number; bound: number }> = []
  for (const c of clips) {
    const bound = clipPeakBound(c, busGain.get(c.bus) ?? 1, peak)
    if (bound > 0 && c.n > 0) live.push({ a: c.out0, b: c.out0 + c.n, bound })
  }
  const cuts = [...new Set(live.flatMap((x) => [x.a, x.b]))].sort((x, y) => x - y)
  const over: Array<[number, number]> = []
  for (let i = 0; i + 1 < cuts.length; i++) {
    const a = cuts[i], b = cuts[i + 1]
    let sum = 0
    for (const x of live) if (x.a <= a && b <= x.b) sum += x.bound
    if (masterGain * sum > ceiling) over.push([a - LIMITING_SPREAD, b + LIMITING_SPREAD])
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
