// Voice effects in the instant-preview engine (wave E, F3): the SAME preset
// table as the export (`edl/voice_effects.py`, dumped into voiceFxTable.ts by
// tests/gen_voice_fx_goldens.py and pinned equal to it), realised as pure
// sample processing the mix graph runs on each block it schedules
// (audio/mixGraph.ts) — plus the one stage that needs a live node, the Hall
// reverb, which the graph convolves with a ConvolverNode over the very
// impulse response `reverbIr` returns (the export's `aevalsrc` closed form).
//
// Stage by stage against ffmpeg (tests/goldens/voice_fx_cases.json,
// voiceFx.test.ts):
//   biquad   af_biquads' RBJ low/high-pass and peaking EQ, direct form I,
//            blended by its `m=` (the filter state is the unblended output);
//   echo     aecho: out_gain · (in_gain · x + Σ decay_j · x[n − d_j]);
//   drive    tanh(k·x) / tanh(k);   ring   x · (1 − d + d·sin(2π f t));
//   vibrato  af_vibrato: a 5 ms line read at 240 − depth·wave[n mod N]
//            samples back (linear interpolation), wave the sine table of
//            ff_generate_wave_table at phase 3π/2;
//   gain     volume in dB;
//   pitch    NOT ffmpeg's: the export re-clocks and WSOLA-stretches
//            (asetrate + atempo); here a fixed-grid granular shifter (2048-
//            sample Hann grains every 1024, each read around its own centre
//            resampled by the ratio and offset by up to half a period so
//            overlapping grains stay in phase — `pitch`). Same pitch
//            ratio, zero mean latency, APPROX in level and texture
//            (support.ts VOICE_FX_PARITY holds the measured numbers).
// Every stage is a function of the clip-local sample index, so a block
// computed with its look-back and look-ahead margins equals the same samples
// of the whole clip (IIR stages from a 4096-sample warm-up: their state has
// decayed by far more than 24 bits by then).

import { VOICE_FX_TABLE } from './voiceFxTable'

export const SR = 48000

export type StageKind = 'pitch' | 'biquad' | 'echo' | 'drive' | 'ring' | 'vibrato' | 'reverb' | 'gain'

export interface StageJson {
  kind: StageKind
  params: Record<string, number | string | number[]>
  scale: Record<string, number>
}

export interface VoicePresetJson {
  id: string
  label: string
  hint: string
  icon: string
  aliases: string[]
  stages: StageJson[]
}

export interface VoiceTable {
  presets: VoicePresetJson[]
  intensity_range: [number, number]
  default_intensity: number
  pitch_eps_semitones: number
  reverb_ir_seconds: number
  sample_rate: number
}

export const VOICE_TABLE = VOICE_FX_TABLE as unknown as VoiceTable
export const VOICE_PRESETS: readonly VoicePresetJson[] = VOICE_TABLE.presets
const BY_ID = new Map(VOICE_PRESETS.map((p) => [p.id, p]))

export function voicePreset(id: unknown): VoicePresetJson | null {
  return typeof id === 'string' ? BY_ID.get(id) ?? null : null
}

export interface Stage { kind: StageKind; p: Record<string, number | string | number[]> }

/** `voice_effects.stages_at`: the stages of `effect` at `intensity` that
 *  change anything (a parameter at i is neutral + (full − neutral) · i). */
export function stagesAt(effect: unknown, intensity: number): Stage[] {
  const pre = voicePreset(effect)
  if (!pre || !(intensity > 0)) return []
  const out: Stage[] = []
  for (const s of pre.stages) {
    const p: Record<string, number | string | number[]> = { ...s.params }
    for (const [k, neutral] of Object.entries(s.scale)) {
      const full = s.params[k]
      p[k] = Array.isArray(full) ? full.map((v) => neutral + (v - neutral) * intensity)
        : neutral + (Number(full) - neutral) * intensity
    }
    if (s.kind === 'pitch' && Math.abs(p.semitones as number) < VOICE_TABLE.pitch_eps_semitones) continue
    if (s.kind === 'drive' && (p.k as number) < 1e-3) continue
    if (s.kind === 'gain' && Math.abs(p.db as number) < 1e-3) continue
    out.push({ kind: s.kind, p })
  }
  return out
}

// ---------------------------------------------------------------- the plan

/** Granular pitch shifter geometry. */
export const GRAIN = 2048
export const HOP = GRAIN / 2
/** Samples an IIR stage runs before its first wanted sample. */
export const IIR_WARMUP = 4096
/** af_vibrato's delay line: lrint(48000 · 0.005). */
export const VIBRATO_BUF = 240
/** render/audio_mix.VOICE_PRIME_S (0.05 s) at 48 kHz (review RE). */
export const VOICE_PRIME_SAMPLES = 2400

export interface ReverbParams { dry: number; tail: number; rt60: number; predelay_ms: number }

export interface VoicePlan {
  effect: string
  intensity: number
  /** Stages run on the samples (every stage but the reverb). */
  offline: Stage[]
  /** The reverb, convolved by a node AFTER the offline stages (it is last
   *  in every preset that has one — tests/test_voice_effects.py). */
  reverb: ReverbParams | null
  /** Clip-local samples the offline stages read before / after a block. */
  back: number
  ahead: number
  /** Samples of REAL sound before the clip head the export primes a
   *  latency-bearing effect with (a pitch or vibrato stage; render/audio_mix.
   *  VOICE_PRIME_S, review RE) — the stages then run from that far before
   *  the head, so their state (and the vibrato / ring LFO phase) at the head
   *  is the export's. 0 for every other effect. */
  prime: number
  /** A stable key of everything that decides the sound (plan timing). */
  key: string
}

function stageMargins(s: Stage): [number, number] {
  switch (s.kind) {
    case 'pitch': {
      // grains reaching the block, their reads (± the alignment offset) and
      // the period analysis around each grain centre
      const k = Math.pow(2, (s.p.semitones as number) / 12)
      const m = GRAIN / 2 + Math.max(PERIOD_WINDOW / 2, Math.ceil((GRAIN / 2) * Math.abs(k - 1)) + PERIOD_MAX_OFFSET) + 2
      return [m, m]
    }
    case 'biquad': return [IIR_WARMUP * Number(s.p.passes ?? 1), 0]
    case 'echo': return [Math.ceil(Math.max(...(s.p.delays_ms as number[])) * SR / 1000) + 1, 0]
    case 'vibrato': return [VIBRATO_BUF + 2, 0]
    default: return [0, 0]
  }
}

/** The plan of a clip's voice effect, or null when it has none (or 0 %). */
export function voicePlan(effect: unknown, intensity: unknown): VoicePlan | null {
  const i = typeof intensity === 'number' && Number.isFinite(intensity)
    ? Math.min(1, Math.max(0, intensity)) : VOICE_TABLE.default_intensity
  const stages = stagesAt(effect, i)
  if (!stages.length) return null
  const offline = stages.filter((s) => s.kind !== 'reverb')
  const rv = stages.find((s) => s.kind === 'reverb')
  let back = 0
  let ahead = 0
  for (const s of offline) {
    const [b, a] = stageMargins(s)
    back += b
    ahead += a
  }
  return {
    effect: String(effect), intensity: i, offline,
    reverb: rv ? { dry: rv.p.dry as number, tail: rv.p.tail as number, rt60: rv.p.rt60 as number,
                   predelay_ms: rv.p.predelay_ms as number } : null,
    back, ahead, key: JSON.stringify([effect, i]),
    prime: offline.some((s) => s.kind === 'pitch' || s.kind === 'vibrato') ? VOICE_PRIME_SAMPLES : 0,
  }
}

// ---------------------------------------------------------------- stages

type Buf = Float64Array

function biquadCoefs(p: Stage['p']): [number, number, number, number, number] {
  const w0 = (2 * Math.PI * (p.f as number)) / SR
  const cw = Math.cos(w0)
  const alpha = Math.sin(w0) / (2 * (p.q as number))
  let a0: number, a1: number, a2: number, b0: number, b1: number, b2: number
  if (p.type === 'lowpass') {
    a0 = 1 + alpha; a1 = -2 * cw; a2 = 1 - alpha
    b0 = (1 - cw) / 2; b1 = 1 - cw; b2 = (1 - cw) / 2
  } else if (p.type === 'highpass') {
    a0 = 1 + alpha; a1 = -2 * cw; a2 = 1 - alpha
    b0 = (1 + cw) / 2; b1 = -(1 + cw); b2 = (1 + cw) / 2
  } else {                                           // peaking (ffmpeg `equalizer`)
    const A = Math.pow(10, (p.gain_db as number) / 40)
    a0 = 1 + alpha / A; a1 = -2 * cw; a2 = 1 - alpha / A
    b0 = 1 + alpha * A; b1 = -2 * cw; b2 = 1 - alpha * A
  }
  return [b0 / a0, b1 / a0, b2 / a0, -a1 / a0, -a2 / a0]
}

function biquad(x: Buf, p: Stage['p']): Buf {
  const [b0, b1, b2, a1, a2] = biquadCoefs(p)
  const wet = p.mix as number
  const dry = 1 - wet
  let cur = x
  for (let pass = 0; pass < Number(p.passes ?? 1); pass++) {
    const y = new Float64Array(cur.length)
    let i1 = 0, i2 = 0, o1 = 0, o2 = 0
    for (let n = 0; n < cur.length; n++) {
      const v = cur[n]
      const o = v * b0 + i1 * b1 + i2 * b2 + o1 * a1 + o2 * a2
      i2 = i1; i1 = v; o2 = o1; o1 = o
      y[n] = o * wet + v * dry
    }
    cur = y
  }
  return cur
}

function echo(x: Buf, p: Stage['p']): Buf {
  const ig = p.in_gain as number
  const og = p.out_gain as number
  const taps = (p.delays_ms as number[]).map((d, j) => [Math.trunc((d * SR) / 1000), (p.decays as number[])[j]])
  const y = new Float64Array(x.length)
  for (let n = 0; n < x.length; n++) {
    let o = x[n] * ig
    for (const [d, g] of taps) if (n - d >= 0) o += x[n - d] * g
    y[n] = o * og
  }
  return y
}

function drive(x: Buf, p: Stage['p']): Buf {
  const k = p.k as number
  const norm = Math.tanh(k)
  return x.map((v) => Math.tanh(k * v) / norm)
}

function ring(x: Buf, p: Stage['p'], n0: number): Buf {
  const d = p.depth as number
  const w = 2 * Math.PI * (p.freq as number)
  return x.map((v, j) => v * (1 - d + d * Math.sin((w * (n0 + j)) / SR)))
}

function gain(x: Buf, p: Stage['p']): Buf {
  const g = Math.pow(10, (p.db as number) / 20)
  return x.map((v) => v * g)
}

/** ff_generate_wave_table(WAVE_SIN, …, 0, buf_size − 1, 3π/2) for af_vibrato. */
export function vibratoTable(freq: number): Float64Array {
  const size = Math.round(SR / freq)
  const t = new Float64Array(size)
  const off = Math.trunc(((3 * Math.PI) / 2 / Math.PI / 2) * size + 0.5)
  for (let i = 0; i < size; i++) {
    const point = (i + off) % size
    t[i] = ((Math.sin((point / size) * 2 * Math.PI) + 1) / 2) * (VIBRATO_BUF - 1)
  }
  return t
}

function vibrato(x: Buf, p: Stage['p'], n0: number): Buf {
  const table = vibratoTable(p.f as number)
  const depth = p.d as number
  const y = new Float64Array(x.length)
  const at = (j: number) => (j >= 0 && j < x.length ? x[j] : 0)
  for (let j = 0; j < x.length; j++) {
    const n = n0 + j
    const w = depth * table[((n % table.length) + table.length) % table.length]
    const integer = Math.trunc(w)
    const decimal = w - integer
    // buf[buf_index + integer] was written (240 − integer) samples ago; its
    // neighbour one sample later.
    const s1 = at(j - (VIBRATO_BUF - integer))
    const s2 = at(j - (VIBRATO_BUF - integer) + 1)
    y[j] = n < 0 ? 0 : s1 + decimal * (s2 - s1)
  }
  return y
}

/** Analysis window of the period estimate, and its decimation. */
export const PERIOD_WINDOW = 2048
const PERIOD_DECIMATE = 4
const PERIOD_MIN_HZ = 60
const PERIOD_MAX_HZ = 500
/** Largest read offset a grain's alignment adds (half the longest period). */
export const PERIOD_MAX_OFFSET = Math.ceil(SR / PERIOD_MIN_HZ / 2)

/** The local period (samples) of `m` around array index `c`: the lag of the
 *  largest autocorrelation over PERIOD_WINDOW samples, found at a quarter of
 *  the rate (60-500 Hz) and refined at the full rate. */
export function localPeriod(m: Float64Array, c: number): number {
  const W = PERIOD_WINDOW
  const D = PERIOD_DECIMATE
  const a0 = c - W / 2
  const seg = new Float64Array(W)
  for (let j = 0; j < W; j++) {
    const i = a0 + j
    seg[j] = i >= 0 && i < m.length ? m[i] : 0
  }
  const Z = W / D
  const z = new Float64Array(Z)
  for (let j = 0; j < Z; j++) {
    let s = 0
    for (let q = 0; q < D; q++) s += seg[j * D + q]
    z[j] = s / D
  }
  const lo = Math.floor(SR / D / PERIOD_MAX_HZ)
  const hi = Math.ceil(SR / D / PERIOD_MIN_HZ)
  let best = -Infinity
  let tau = lo
  for (let l = lo; l <= hi; l++) {
    let v = 0
    for (let j = 0; j + l < Z; j++) v += z[j] * z[j + l]
    if (v > best) { best = v; tau = l }
  }
  const t0 = tau * D
  best = -Infinity
  let T = t0
  for (let l = t0 - D; l <= t0 + D; l++) {
    let v = 0
    for (let j = 0; j + l < W; j++) v += seg[j] * seg[j + l]
    if (v > best) { best = v; T = l }
  }
  return T
}

/** Period-aligned granular pitch shift of one channel holding clip-local
 *  samples [n0, n0 + length): output sample n is Σ over the Hann grains
 *  centred at c = i·HOP of w(n − c) · x(c + d_c + (n − c)·k). The read offset
 *  d_c ≡ −c·(1 − k) modulo the local period T_c (wrapped to ±T/2) makes
 *  overlapping grains read the waveform in phase — what WSOLA's search
 *  achieves in the export — and depends only on c and the samples around it,
 *  so any block with its margins matches the whole clip. Each channel finds
 *  its own period (a stereo pair need not share one). */
function pitch(x: Buf, p: Stage['p'], n0: number): Buf {
  const k = Math.pow(2, (p.semitones as number) / 12)
  const len = x.length
  const y = new Float64Array(len)
  const half = GRAIN / 2
  const win = new Float64Array(GRAIN)
  for (let m = 0; m < GRAIN; m++) win[m] = 0.5 - 0.5 * Math.cos((2 * Math.PI * m) / GRAIN)
  const iFirst = Math.floor((n0 - half) / HOP)
  const iLast = Math.ceil((n0 + len + half) / HOP)
  for (let i = iFirst; i <= iLast; i++) {
    const c = i * HOP
    const T = localPeriod(x, c - n0)
    let d = (((-c * (1 - k)) % T) + T) % T
    if (d > T / 2) d -= T
    for (let m = 0; m < GRAIN; m++) {
      const j = c - half + m - n0
      if (j < 0 || j >= len) continue
      // outside the array is silence (outside the clip, as in the export)
      const u = c + d + (m - half) * k - n0
      const q = Math.floor(u)
      if (q < -1 || q >= len) continue
      const f = u - q
      const s0 = q >= 0 ? x[q] : 0
      const s1 = q + 1 < len ? x[q + 1] : 0
      y[j] += win[m] * (s0 + f * (s1 - s0))
    }
  }
  return y
}

function perChannel(s: Stage, x: Buf, n0: number): Buf {
  switch (s.kind) {
    case 'biquad': return biquad(x, s.p)
    case 'echo': return echo(x, s.p)
    case 'drive': return drive(x, s.p)
    case 'ring': return ring(x, s.p, n0)
    case 'vibrato': return vibrato(x, s.p, n0)
    case 'gain': return gain(x, s.p)
    case 'pitch': return pitch(x, s.p, n0)
    default: return x
  }
}

/** Run the offline stages over a stereo pair holding clip-local samples
 *  [n0, n0 + L.length) (zero outside the clip). The first `plan.back` and the
 *  last `plan.ahead` output samples are margin, not valid output. */
export function processStereo(plan: VoicePlan, L: Float64Array | Float32Array, R: Float64Array | Float32Array,
                              n0: number): [Float64Array, Float64Array] {
  let l: Buf = Float64Array.from(L)
  let r: Buf = Float64Array.from(R)
  for (const s of plan.offline) {
    l = perChannel(s, l, n0)
    r = perChannel(s, r, n0)
  }
  return [l, r]
}

// ---------------------------------------------------------------- the reverb IR

/** `voice_effects.reverb_constants`. */
export function reverbConstants(p: ReverbParams): { dry: number; tail: number; a: number; P: number; n: number } {
  return {
    dry: p.dry, tail: p.tail, a: Math.log(1000) / (p.rt60 * SR),
    P: Math.round((p.predelay_ms * SR) / 1000), n: Math.round(VOICE_TABLE.reverb_ir_seconds * SR),
  }
}

/** The Hall impulse response of `channel` — the closed form the export's
 *  `aevalsrc` evaluates (voice_effects.reverb_ir). */
export function reverbIr(p: ReverbParams, channel: number): Float32Array {
  const k = reverbConstants(p)
  const out = new Float32Array(k.n)
  for (let n = 0; n < k.n; n++) {
    const x = Math.sin((n + 7919 * channel) * 12.9898) * 43758.5453
    let v = n >= k.P ? k.tail * Math.exp(-k.a * (n - k.P)) * (2 * (x - Math.floor(x)) - 1) : 0
    if (n === 0) v += k.dry
    out[n] = v
  }
  return out
}
