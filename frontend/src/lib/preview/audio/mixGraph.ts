// The MIX GRAPH (instant preview spec §3.6): an AudioPlan realised on an
// AudioContext (live playback) or an OfflineAudioContext (verification, the
// P1-A1/A2 tests) through ONE code path.
//
//   per clip  AudioBufferSourceNode per 1 s block, started at an INTEGER
//             output sample (its buffer holds exactly that block's source
//             samples: runs, reverse, silence past the source's end, the
//             chunk headroom already undone)
//             → ChannelSplitter → 2×2 matrix of GainNodes → ChannelMerger
//               (channel mode: stereo / left / right / mono)
//             → shape GainNode: clip gain × gain_env × afades × acrossfade ×
//               mute, as a PER-SAMPLE value curve per block (exact at every
//               sample, whatever the curve)
//   per lane  → generation GainNode (a structural edit cross-fades an old
//               generation of a lane out and the new one in over 5 ms)
//             → bus GainNode (track mute / solo) → [music: duck GainNode]
//   master    → gain (mix auto-level × loudness) → [limiter: Dynamics-
//               Compressor, APPROX, its 6 ms look-ahead compensated]
//               → [post: a mixed programme's loudness gain → a second
//               limiter at −1 dBFS, the server preview's order] → out
//               (transport: 5 ms ramps on pause) → destination
//
// Parameter-only edits rewrite automation from a context time with
// cancelAndHoldAtTime and a 5 ms ramp; nothing is restarted.

import {
  clipGainAt, DUCK_ATTACK_TAU_S, DUCK_HOLD_S, DUCK_LOOKAHEAD_S, DUCK_RELEASE_TAU_S, diffPlans,
  type AudioPlan, type ChannelMode, type ClipAudio, type PlanDiff,
} from './audioPlan'
import type { PcmReader } from './audioChunks'
import { SAMPLE_RATE } from './curves'
import { curveMap, sourceSeconds, type CurveMap } from '../timeline/speedCurve'
import { processStereo, reverbIr, type ReverbParams } from '../../voice/voiceFx'

const SR = SAMPLE_RATE
/** Scheduling block: sources and value curves are cut on this grid. */
export const BLOCK_SAMPLES = SR
/** Ramp of every parameter change, pause and structural cross-fade. */
export const RAMP_S = 0.005
/** DynamicsCompressorNode's fixed look-ahead in WebKit and Chromium: 6 ms
 *  (288 samples at 48 kHz, measured in both; tests/wk/test_wk_audio.py). */
export const LIMITER_LATENCY = 288
const LIMITER_RATIO = 20
/** Processing time after which a new limiter is transparent (see
 *  `MixGraph.limiterWarm`). */
export const LIMITER_WARMUP_S = 0.1
/** Live scheduling lead: nothing is started closer to "now" than this. */
const LIVE_LEAD_S = 0.01

/** The 2×2 channel matrix of `audio_mix.CHANNEL_PANS` ([LL, RL, LR, RR]:
 *  out L = LL·inL + RL·inR, out R = LR·inL + RR·inR). */
export function channelMatrix(mode: ChannelMode): [number, number, number, number] {
  switch (mode) {
    case 'left': return [1, 0, 1, 0]
    case 'right': return [0, 1, 0, 1]
    case 'mono': return [0.5, 0.5, 0.5, 0.5]
    default: return [1, 0, 0, 1]
  }
}

/** The static gain that undoes DynamicsCompressorNode's automatic makeup
 *  gain (Web Audio: (1/curve(1.0))^0.6) for a hard knee at `thresholdDb`. */
export function limiterMakeupUndo(thresholdDb: number, ratio = LIMITER_RATIO): number {
  const t = Math.min(0, thresholdDb)
  return Math.pow(10, (0.6 * t * (1 - 1 / ratio)) / 20)
}

/** Samples of look-ahead a planned master adds: one LIMITER_LATENCY per
 *  limiter stage (the mix limiter, and a mixed programme's loudness
 *  limiter after it — `MasterPlan.post`). */
export function masterLatency(m: AudioPlan['master']): number {
  return ((m.ceilingDb !== null ? 1 : 0) + (m.post ? 1 : 0)) * LIMITER_LATENCY
}

/** A master's STRUCTURE (which limiter stages exist): a change rebuilds it
 *  and, since the look-ahead moves, reschedules every lane. */
const masterShape = (m: AudioPlan['master']): string => `${m.ceilingDb !== null}|${m.post !== undefined}`

export interface Anchor {
  /** Context time at which output sample `sample` is HEARD. */
  ctxTime: number
  sample: number
}

interface Scheduled { node: AudioBufferSourceNode; p0: number; p1: number }

const IDENTITY: [number, number, number, number] = [1, 0, 0, 1]

/** The node matrix of a clip: its channel mode — except a voice-effect
 *  clip, whose samples already carry it (the effect follows the pan). */
const nodeMatrix = (c: ClipAudio) => (c.voice ? IDENTITY : channelMatrix(c.channels))

interface ClipNodes {
  clip: ClipAudio
  bus: string
  input: ChannelSplitterNode
  mtx: GainNode[]
  merger: ChannelMergerNode
  /** The Hall voice effect's convolver (merger → conv → shape), or null. */
  conv: ConvolverNode | null
  shape: GainNode
  sources: Scheduled[]
  /** Output samples scheduled so far: [from, until). */
  from: number
  until: number
}

interface BusNodes {
  gain: GainNode
  duck: GainNode | null
  /** The lane's current generation (a structural edit starts a new one). */
  gen: GainNode
  /** First output sample the current generation may play. */
  genStart: number
  tail: AudioNode
}

export interface MixGraphOptions {
  reader: PcmReader
  destination?: AudioNode
  /** Schedule early by the limiter's look-ahead (live). Offline renders
   *  trim it from the output instead (`renderOffline`). */
  compensateLatency?: boolean
  /** Called when a block could not be scheduled whole (a chunk not loaded). */
  onMissing?: (clip: ClipAudio, p0: number, p1: number) => void
}

type ParamLike = AudioParam & { cancelAndHoldAtTime?: (t: number) => AudioParam }

/** The last ramp this module put on a param (its value at any time after). */
interface Ramp { t0: number; v0: number; t1: number; v1: number }
const ramps = new WeakMap<AudioParam, Ramp>()

/** A param's value at context time `t` as this module automated it: the
 *  last ramp's (interpolated inside it), else its static value. */
export function trackedValue(param: AudioParam, t: number): number {
  const r = ramps.get(param)
  if (!r) return param.value
  if (t >= r.t1) return r.v1
  if (t <= r.t0) return r.v0
  return r.v0 + ((r.v1 - r.v0) * (t - r.t0)) / (r.t1 - r.t0)
}

/** Rewrite a param from `t`: pin the value it has at `t` (`at`, or what this
 *  module last ramped it to), then ramp to `v` over `ramp` seconds.
 *
 *  The explicit setValueAtTime matters: on a param with no automation
 *  cancelAndHoldAtTime inserts nothing, and a linear ramp with no event
 *  before it runs from the moment of the CALL — a generation cross-fade
 *  meant for 200 ms ahead started fading at once (measured in WebKit). */
export function holdAndRamp(param: ParamLike, t: number, v: number, ramp = RAMP_S, at?: number): void {
  const vt = at ?? trackedValue(param, t)
  if (typeof param.cancelAndHoldAtTime === 'function') param.cancelAndHoldAtTime(t)
  else param.cancelScheduledValues(t)
  param.setValueAtTime(vt, t)
  param.linearRampToValueAtTime(v, t + ramp)
  ramps.set(param, { t0: t, v0: vt, t1: t + ramp, v1: v })
}

/** Write per-sample gains from `t0`: one setValueAtTime for a constant
 *  stretch (the common case: no fade, no envelope), else a value curve with
 *  one point per sample. */
function setCurve(param: AudioParam, values: Float32Array, t0: number): void {
  const v0 = values[0]
  let constant = true
  for (let i = 1; i < values.length; i++) if (values[i] !== v0) { constant = false; break }
  if (constant) param.setValueAtTime(v0, t0)
  else param.setValueCurveAtTime(values, t0, (values.length - 1) / SR)
}

const identity = (c: ClipAudio) => `${c.bus}|${c.id}|${c.timing}`

export class MixGraph {
  readonly ctx: BaseAudioContext
  plan: AudioPlan
  anchor: Anchor = { ctxTime: 0, sample: 0 }
  /** Output node of the graph (transport gain). */
  readonly out: GainNode
  readonly stats = { sources: 0, missing: 0, rewrites: 0, reschedules: 0 }
  private readonly reader: PcmReader
  private readonly compensate: boolean
  private readonly onMissing?: (clip: ClipAudio, p0: number, p1: number) => void
  private readonly destination: AudioNode
  /** A realtime context (an OfflineAudioContext renders ahead of time). */
  private readonly live: boolean
  private master!: GainNode
  private limiter: DynamicsCompressorNode | null = null
  private limiterGain: GainNode | null = null
  /** The loudness stage after the mix limiter (`MasterPlan.post`). */
  private post: { gain: GainNode; lim: DynamicsCompressorNode; undo: GainNode } | null = null
  private buses = new Map<string, BusNodes>()
  /** The one node feeding `master`: the lanes summed TWO AT A TIME in
   *  creation order (`attachToMaster`), never N inputs on one node. */
  private sumTail: AudioNode | null = null
  private clips = new Map<string, ClipNodes>()
  private disposed = false
  /** The transport's start point: no sample before it is scheduled. */
  private startSample = 0
  /** Context time the current limiter was built at (its warm-up clock). */
  private limiterBorn = 0

  constructor(ctx: BaseAudioContext, plan: AudioPlan, opts: MixGraphOptions) {
    this.ctx = ctx
    this.plan = plan
    this.reader = opts.reader
    this.compensate = opts.compensateLatency ?? false
    this.onMissing = opts.onMissing
    this.destination = opts.destination ?? ctx.destination
    this.live = typeof (ctx as { startRendering?: unknown }).startRendering !== 'function'
    this.out = ctx.createGain()
    this.out.gain.value = 0
    this.out.connect(this.destination)
    this.buildMaster()
    for (const b of plan.buses) this.bus(b.id)
  }

  /** Samples the master adds before the output (the limiter's look-ahead). */
  get latency(): number {
    return ((this.limiter ? 1 : 0) + (this.post ? 1 : 0)) * LIMITER_LATENCY
  }

  /** Context time a source must START at for output sample `p` to be heard
   *  on the anchor. */
  when(p: number): number {
    const lat = this.compensate ? this.latency : 0
    // On a 48 kHz context the anchor snaps to the context's own sample grid,
    // so every start is an INTEGER frame: a fractional start is rendered
    // with sub-sample interpolation (measured in WebKit: a click leaks into
    // the sample before it).
    if (this.ctx.sampleRate === SR) return (Math.round(this.anchor.ctxTime * SR) + p - this.anchor.sample - lat) / SR
    return this.anchor.ctxTime + (p - this.anchor.sample - lat) / SR
  }

  /** The output sample being scheduled at context time `t` (inverse of `when`). */
  sampleAt(t: number): number {
    const lat = this.compensate ? this.latency : 0
    if (this.ctx.sampleRate === SR) return this.anchor.sample + lat + t * SR - Math.round(this.anchor.ctxTime * SR)
    return this.anchor.sample + lat + (t - this.anchor.ctxTime) * SR
  }

  // ------------------------------------------------------------ structure

  /** A hard-knee limiter at `ceiling` dBFS and the gain undoing its makeup. */
  private limiterStage(ceiling: number): [DynamicsCompressorNode, GainNode] {
    const lim = this.ctx.createDynamicsCompressor()
    lim.threshold.value = ceiling
    lim.knee.value = 0
    lim.ratio.value = LIMITER_RATIO
    lim.attack.value = 0.001
    lim.release.value = 0.05
    const g = this.ctx.createGain()
    g.gain.value = limiterMakeupUndo(ceiling)
    lim.connect(g)
    return [lim, g]
  }

  private buildMaster(): void {
    this.master = this.ctx.createGain()
    this.master.gain.value = this.plan.master.gain
    const ceiling = this.plan.master.ceilingDb
    let tail: AudioNode = this.master
    if (ceiling !== null) {
      const [lim, g] = this.limiterStage(ceiling)
      tail.connect(lim)
      tail = g
      this.limiter = lim
      this.limiterGain = g
      this.limiterBorn = this.ctx.currentTime
    }
    const post = this.plan.master.post
    if (post) {
      // mix → [mix limiter] → loudness gain → −1 dBFS limiter (the server
      // preview's order, `audio_mix._preview_norm_chain`)
      const gain = this.ctx.createGain()
      gain.gain.value = post.gain
      const [lim, undo] = this.limiterStage(post.ceilingDb)
      tail.connect(gain).connect(lim)
      tail = undo
      this.post = { gain, lim, undo }
      this.limiterBorn = this.ctx.currentTime
    }
    tail.connect(this.out)
  }

  /** Set the master's parameters to `m` (same structure) — ramped from `t`,
   *  or at once when `t` is null (nothing scheduled). */
  private applyMasterParams(m: AudioPlan['master'], t: number | null): void {
    const set = (param: AudioParam, v: number) => {
      if (t === null) { param.cancelScheduledValues(0); param.value = v } else holdAndRamp(param, t, v)
    }
    set(this.master.gain, m.gain)
    if (this.limiter && m.ceilingDb !== null) {
      if (t === null) this.limiter.threshold.value = m.ceilingDb
      else this.limiter.threshold.setValueAtTime(m.ceilingDb, t)
      set(this.limiterGain!.gain, limiterMakeupUndo(m.ceilingDb))
    }
    if (this.post && m.post) {
      set(this.post.gain.gain, m.post.gain)
      if (t === null) this.post.lim.threshold.value = m.post.ceilingDb
      else this.post.lim.threshold.setValueAtTime(m.post.ceilingDb, t)
      set(this.post.undo.gain, limiterMakeupUndo(m.post.ceilingDb))
    }
  }

  private bus(id: string): BusNodes {
    let b = this.buses.get(id)
    if (b) return b
    const gain = this.ctx.createGain()
    gain.gain.value = this.plan.buses.find((x) => x.id === id)?.gain ?? 1
    const duck = id === this.plan.duck?.bus || id === 'music' ? this.ctx.createGain() : null
    if (duck) gain.connect(duck)
    const gen = this.ctx.createGain()
    gen.connect(gain)
    b = { gain, duck, gen, genStart: this.startSample, tail: duck ?? gain }
    this.buses.set(id, b)
    this.attachToMaster(b.tail)
    return b
  }

  /** Sum lane `tail` into the master through a node of its own with exactly
   *  TWO inputs (the lanes before it, and it). Web Audio sums every
   *  connection into an input in an unspecified order — Chromium and WebKit
   *  walk a hash set of the connected outputs, keyed by address, so the
   *  order changes from one context to the next — and float addition is not
   *  associative: three lanes sounding at once (v1, the bed and a voice-over)
   *  came out a few ULP apart between two offline renders of one plan
   *  (Chromium, P1-A2 `mix`: 5 of 5 render pairs differed, up to 1.2e-7 from
   *  the voice-over's first sample). Two inputs commute exactly, so a chain
   *  of pairs is the same sum in every render. */
  private attachToMaster(tail: AudioNode): void {
    const prev = this.sumTail
    if (!prev) {
      tail.connect(this.master)
      this.sumTail = tail
      return
    }
    const sum = this.ctx.createGain()
    try { prev.disconnect(this.master) } catch { /* not connected */ }
    prev.connect(sum)
    tail.connect(sum)
    sum.connect(this.master)
    this.sumTail = sum
  }

  private clipNodes(c: ClipAudio): ClipNodes {
    const id = identity(c)
    let n = this.clips.get(id)
    if (n) return n
    const ctx = this.ctx
    const input = ctx.createChannelSplitter(2)
    const merger = ctx.createChannelMerger(2)
    const m = nodeMatrix(c)
    // [inL→outL, inR→outL, inL→outR, inR→outR]
    const mtx = m.map((v) => { const g = ctx.createGain(); g.gain.value = v; return g })
    input.connect(mtx[0], 0).connect(merger, 0, 0)
    input.connect(mtx[1], 1).connect(merger, 0, 0)
    input.connect(mtx[2], 0).connect(merger, 0, 1)
    input.connect(mtx[3], 1).connect(merger, 0, 1)
    const shape = ctx.createGain()
    shape.gain.value = 0
    // A Hall voice effect: the export's own impulse response, after the
    // offline stages and before the clip's gains (voiceFx.ts).
    const conv = c.voice?.reverb ? reverbNode(ctx, c.voice.reverb) : null
    if (conv) merger.connect(conv).connect(shape)
    else merger.connect(shape)
    shape.connect(this.bus(c.bus).gen)
    n = { clip: c, bus: c.bus, input, mtx, merger, conv, shape, sources: [], from: Number.POSITIVE_INFINITY, until: Number.NEGATIVE_INFINITY }
    this.clips.set(id, n)
    return n
  }

  // ------------------------------------------------------------ scheduling

  /** Load every source chunk [p0, p1) of the programme needs. */
  async prefetch(p0: number, p1: number): Promise<void> {
    const jobs: Array<Promise<void>> = []
    for (const c of this.plan.clips) {
      const r = sourceRange(c, Math.max(p0, c.out0), Math.min(p1, c.out0 + c.n))
      if (r) jobs.push(this.reader.load(c.src, r[0], r[1]))
    }
    await Promise.all(jobs)
  }

  /** Schedule every clip's sound for output samples [p0, p1), block by block
   *  on the BLOCK_SAMPLES grid, skipping what is already scheduled. Returns
   *  the number of blocks that lacked a chunk. */
  schedule(p0: number, p1: number): number {
    if (this.disposed) return 0
    let missing = 0
    const floor = this.live ? this.ctx.currentTime + LIVE_LEAD_S : -Infinity
    const end = Math.min(p1, this.plan.total)
    for (const c of this.plan.clips) {
      // Nothing before the transport's start point — or before the start of
      // its lane's generation (a structural edit) — ever sounds.
      const a0 = Math.max(p0, c.out0, this.startSample, this.bus(c.bus).genStart)
      const b0 = Math.min(end, c.out0 + c.n)
      if (b0 <= a0) continue
      // Only what this clip has not scheduled yet: before and after the
      // span it holds.
      const nodes = this.clips.get(identity(c))
      const spans: Array<[number, number]> = nodes && nodes.until > nodes.from
        ? [[a0, Math.min(b0, nodes.from)], [Math.max(a0, nodes.until), b0]]
        : [[a0, b0]]
      for (const [s0, b] of spans) {
        let a = s0
        if (b <= a) continue
        if (this.when(a) < floor) a = Math.ceil(this.sampleAt(floor))
        for (let q = a; q < b;) {
          const qe = Math.min(b, (Math.floor(q / BLOCK_SAMPLES) + 1) * BLOCK_SAMPLES)
          if (this.live) {
            // Live: a block waits for its chunks (the next refill, or the load's
            // own retry), so a clip's sound is never cut into silent holes.
            const r = sourceRange(c, q, qe)
            if (r && !this.reader.ready(c.src, r[0], r[1])) { missing++; break }
          }
          if (!this.scheduleBlock(c, q, qe)) missing++
          q = qe
        }
      }
    }
    this.stats.missing += missing
    return missing
  }

  private scheduleBlock(c: ClipAudio, a: number, b: number): boolean {
    const nodes = this.clipNodes(c)
    const ctx = this.ctx
    const t = this.when(a)
    let ok = true
    const src = ctx.createBufferSource()
    if (c.voice) {
      // A voice effect (wave E, F3): the retimed samples of the block and its
      // margins, the channel mode, then the effect's offline stages
      // (voiceFx.processStereo — the same samples whatever block they fall in).
      const v = c.voice
      const e0 = a - v.back
      const e1 = b + v.ahead
      // review RE: the export primes a pitch / vibrato effect with real sound
      // before the head — the same samples here, and the stages' clock (the
      // LFO phase) counted from where the export's stream starts
      const pr = primeOf(c)
      const pcm = this.retimed(c, e0, e1, pr)
      if (!pcm.ok) ok = false
      const [yL, yR] = processStereo(v, pcm.L, pcm.R, e0 - c.out0 + pr)
      src.buffer = toBuffer(ctx, Float32Array.from(yL.subarray(a - e0, b - e0)), Float32Array.from(yR.subarray(a - e0, b - e0)))
      src.start(t)
    } else if (c.map.kind === 'runs') {
      const L = new Float32Array(b - a)
      const R = new Float32Array(b - a)
      for (const [off, cnt, first, dir] of c.map.runs) {
        const r0 = Math.max(a, c.out0 + off)
        const r1 = Math.min(b, c.out0 + off + cnt)
        if (r1 <= r0) continue
        const s = first + dir * (r0 - c.out0 - off)
        if (!this.reader.copy(c.src, s, r1 - r0, dir as 1 | -1, L, R, r0 - a)) ok = false
      }
      src.buffer = toBuffer(ctx, L, R)
      src.start(t)
    } else if (c.map.kind === 'curve') {
      // A speed curve: the warped source positions of every output sample,
      // linearly interpolated here (varispeed; the export's intermediate
      // uses a windowed sinc, or WSOLA to keep pitch). APPROX.
      const xs = curvePositions(c, a, b)
      const lo = Math.max(0, Math.floor(Math.min(xs[0], xs[xs.length - 1])))
      const hi = Math.min(c.map.end, Math.ceil(Math.max(xs[0], xs[xs.length - 1])) + 2)
      const SL = new Float32Array(Math.max(0, hi - lo))
      const SRr = new Float32Array(Math.max(0, hi - lo))
      if (hi > lo && !this.reader.copy(c.src, lo, hi - lo, 1, SL, SRr, 0)) ok = false
      const L = new Float32Array(b - a)
      const R = new Float32Array(b - a)
      for (let j = 0; j < xs.length; j++) {
        const x = xs[j] - lo
        const i0 = Math.floor(x)
        if (i0 < 0 || i0 + 1 >= SL.length + 1) continue
        const f = x - i0
        const l1 = i0 + 1 < SL.length ? SL[i0 + 1] : 0
        const r1 = i0 + 1 < SRr.length ? SRr[i0 + 1] : 0
        L[j] = SL[i0] + f * (l1 - SL[i0])
        R[j] = SRr[i0] + f * (r1 - SRr[i0])
      }
      src.buffer = toBuffer(ctx, L, R)
      src.start(t)
    } else {
      // A resample: the source block around the (fractional) positions,
      // played at `rate` from the exact fractional offset. APPROX.
      const { src0, rate, reverse, end } = c.map
      const x0 = reverse ? src0 - (a - c.out0) * rate : src0 + (a - c.out0) * rate
      const span = Math.ceil((b - a) * rate) + 2
      const base = reverse ? Math.floor(x0) : Math.floor(x0)
      const L = new Float32Array(span)
      const R = new Float32Array(span)
      const count = reverse ? span : Math.max(0, Math.min(span, end - base))
      if (count > 0 && !this.reader.copy(c.src, base, count, reverse ? -1 : 1, L, R, 0)) ok = false
      src.buffer = toBuffer(ctx, L, R)
      src.playbackRate.value = rate
      const frac = reverse ? 0 : x0 - base
      src.start(t, frac / SR)
      src.stop(this.when(b))
    }
    src.connect(nodes.input)
    nodes.sources.push({ node: src, p0: a, p1: b })
    const vals = new Float32Array(b - a)
    for (let j = 0; j < vals.length; j++) vals[j] = clipGainAt(c, a + j)
    // The value before a node's first curve is its first value: an event
    // time a hair past its frame (context times far from 0 are not exact
    // multiples of 1/48000) must not mute the clip's first sample.
    if (nodes.until < nodes.from) nodes.shape.gain.value = vals[0]
    setCurve(nodes.shape.gain, vals, t)
    // A reverb's tail stops where the clip does (the export cuts the chain
    // to the clip's exact length).
    if (nodes.conv && b >= c.out0 + c.n) nodes.shape.gain.setValueAtTime(0, this.when(c.out0 + c.n))
    nodes.from = Math.min(nodes.from, a)
    nodes.until = Math.max(nodes.until, b)
    this.stats.sources++
    if (this.live) {
      // Live: forget a block once it has played. Only the reference: a
      // finished source leaves the graph by itself, and a disconnect() from
      // an `ended` handler raced WebKit's render thread — in an offline
      // render it silenced OTHER sources from a random quantum on (measured:
      // 3 of 25 renders; none of 100 without the handler), so an offline
      // render keeps its whole graph untouched until it is done.
      src.onended = () => {
        const i = nodes.sources.findIndex((s) => s.node === src)
        if (i >= 0) nodes.sources.splice(i, 1)
      }
    }
    if (!ok) this.onMissing?.(c, a, b)
    return ok
  }

  /** A voice-effect clip's retimed sound for output samples [p0, p1), its
   *  channel mode applied: zero outside the clip [out0, out0 + n) — the
   *  export's effect sees only the clip's own samples. Every map is read
   *  sample by sample here (a resample by linear interpolation, like the
   *  curve path) so the effect runs on exactly what the block plays. */
  private retimed(c: ClipAudio, p0: number, p1: number, prime = 0): { L: Float32Array; R: Float32Array; ok: boolean } {
    const n = Math.max(0, p1 - p0)
    const L = new Float32Array(n)
    const R = new Float32Array(n)
    const q0 = Math.max(p0, c.out0 - prime)
    const q1 = Math.min(p1, c.out0 + c.n)
    let ok = true
    if (q1 > q0) {
      if (c.map.kind === 'runs') {
        for (const [off, cnt, first, dir] of c.map.runs) {
          // the first run also reaches `prime` samples back (primeOf)
          const r0 = Math.max(q0, c.out0 + off - (off === 0 ? prime : 0))
          const r1 = Math.min(q1, c.out0 + off + cnt)
          if (r1 <= r0) continue
          const s = first + dir * (r0 - c.out0 - off)
          if (!this.reader.copy(c.src, s, r1 - r0, dir as 1 | -1, L, R, r0 - p0)) ok = false
        }
      } else {
        let xs: Float64Array
        let end: number
        if (c.map.kind === 'curve') {
          xs = curvePositions(c, q0, q1)
          end = c.map.end
        } else {
          const { src0, rate, reverse } = c.map
          xs = new Float64Array(q1 - q0)
          for (let j = 0; j < xs.length; j++) xs[j] = src0 + (reverse ? -1 : 1) * (q0 + j - c.out0) * rate
          end = c.map.end
        }
        const lo = Math.max(0, Math.floor(Math.min(xs[0], xs[xs.length - 1])))
        const hi = Math.min(end, Math.ceil(Math.max(xs[0], xs[xs.length - 1])) + 2)
        const SL = new Float32Array(Math.max(0, hi - lo))
        const SRr = new Float32Array(Math.max(0, hi - lo))
        if (hi > lo && !this.reader.copy(c.src, lo, hi - lo, 1, SL, SRr, 0)) ok = false
        for (let j = 0; j < xs.length; j++) {
          const x = xs[j] - lo
          const i0 = Math.floor(x)
          if (i0 < 0 || i0 >= SL.length) continue
          const f = x - i0
          const l1 = i0 + 1 < SL.length ? SL[i0 + 1] : 0
          const r1 = i0 + 1 < SRr.length ? SRr[i0 + 1] : 0
          L[q0 - p0 + j] = SL[i0] + f * (l1 - SL[i0])
          R[q0 - p0 + j] = SRr[i0] + f * (r1 - SRr[i0])
        }
      }
    }
    const [ll, rl, lr, rr] = channelMatrix(c.channels)
    if (ll !== 1 || rl !== 0 || lr !== 0 || rr !== 1) {
      for (let j = 0; j < n; j++) {
        const l = L[j], r = R[j]
        L[j] = ll * l + rl * r
        R[j] = lr * l + rr * r
      }
    }
    return { L, R, ok }
  }

  // ------------------------------------------------------------ edits

  /** Apply a new plan from output sample `p` (context time `when(p)`):
   *  buses whose TIMING changed are cross-faded to a fresh generation and
   *  rescheduled from `p`; gain-only changes rewrite automation. Returns the
   *  diff it applied. */
  setPlan(next: AudioPlan, p: number, scheduleUntil: number): PlanDiff {
    const diff = diffPlans(this.plan, next)
    const prev = this.plan
    const t = this.when(p)
    if (masterShape(prev.master) !== masterShape(next.master)) {
      // A limiter stage (and its latency) came or went: everything moves.
      for (const b of next.buses) diff.dirtyBuses.add(b.id)
      for (const b of prev.buses) diff.dirtyBuses.add(b.id)
    }
    this.plan = next
    for (const bus of diff.dirtyBuses) {
      this.newGeneration(bus, t)
      this.bus(bus).genStart = p
    }
    // Re-key surviving clips to the new plan's objects; rewrite their gains.
    const byId = new Map(next.clips.map((c) => [identity(c), c]))
    for (const [id, nodes] of this.clips) {
      const c = byId.get(id)
      if (!c) continue
      const old = nodes.clip
      nodes.clip = c
      if (old.params !== c.params) this.rewriteClip(nodes, p, old)
    }
    if (diff.busGains) for (const b of next.buses) holdAndRamp(this.bus(b.id).gain.gain, t, b.gain)
    if (diff.master) {
      if (masterShape(prev.master) === masterShape(next.master)) this.applyMasterParams(next.master, t)
      else this.rebuildMaster()
    }
    if (diff.duck) this.applyDuck(p)
    if (diff.dirtyBuses.size) {
      this.stats.reschedules++
      this.schedule(p, scheduleUntil)
    }
    return diff
  }

  /** Hold a clip's shape at `p` and ramp into the new gains, then write its
   *  already-scheduled blocks' curves from there. */
  private rewriteClip(nodes: ClipNodes, p: number, old: ClipAudio): void {
    const c = nodes.clip
    const t = this.when(p)
    const pr = p + Math.round(RAMP_S * SR)
    holdAndRamp(nodes.shape.gain, t, clipGainAt(c, pr), RAMP_S, clipGainAt(old, p))
    const m = nodeMatrix(c)
    nodes.mtx.forEach((g, i) => holdAndRamp(g.gain, t, m[i]))
    for (const s of nodes.sources) {
      const a = Math.max(s.p0, pr + 1)
      if (s.p1 <= a) continue
      const vals = new Float32Array(s.p1 - a)
      for (let j = 0; j < vals.length; j++) vals[j] = clipGainAt(c, a + j)
      setCurve(nodes.shape.gain, vals, this.when(a))
    }
    this.stats.rewrites++
  }

  /** Cross-fade `bus`'s current generation out over [t, t + 5 ms], stop its
   *  sources, and start a new, empty generation fading in. */
  private newGeneration(busId: string, t: number): void {
    const b = this.bus(busId)
    const old = b.gen
    holdAndRamp(old.gain, t, 0)
    const gen = this.ctx.createGain()
    gen.gain.value = 0
    holdAndRamp(gen.gain, t, 1, RAMP_S, 0)
    gen.connect(b.gain)
    b.gen = gen
    const stopAt = t + RAMP_S + 0.002
    for (const [id, nodes] of [...this.clips]) {
      if (nodes.bus !== busId) continue
      for (const s of nodes.sources) {
        try { s.node.stop(Math.max(stopAt, s.node.context.currentTime)) } catch { /* not started */ }
      }
      this.clips.delete(id)
    }
    const ctx = this.ctx
    if (this.live) {
      setTimeout(() => { try { old.disconnect() } catch { /* gone */ } }, (stopAt - ctx.currentTime) * 1000 + 50)
    }
  }

  private rebuildMaster(): void {
    const old = this.master
    const oldLim = this.limiter
    const oldGain = this.limiterGain
    const oldPost = this.post
    this.limiter = null
    this.limiterGain = null
    this.post = null
    this.buildMaster()
    // the lanes' pairwise sum (`attachToMaster`) moves over as one node
    if (this.sumTail) {
      try { this.sumTail.disconnect(old) } catch { /* not connected */ }
      this.sumTail.connect(this.master)
    }
    for (const n of [old, oldLim, oldGain, oldPost?.gain, oldPost?.lim, oldPost?.undo]) {
      try { n?.disconnect() } catch { /* gone */ }
    }
  }

  /** The music duck (APPROX trapezoid): the bed dips to `floor` from 60 ms
   *  before the key sounds (attack τ ≈ 26 ms) and swells back after the
   *  server's hold (≈ 1.47 s) with its release τ ≈ 159 ms. */
  private applyDuck(p: number): void {
    const d = this.plan.duck
    const nodes = d ? this.buses.get(d.bus) : this.buses.get('music')
    if (!nodes?.duck) return
    const g = nodes.duck.gain
    const t = Math.max(0, this.when(p))
    const v = d ? duckValueAt(d, p) : 1
    holdAndRamp(g as ParamLike, t, v, RAMP_S, v)
    if (!d) return
    for (const [on, off] of duckWindows(d)) {
      const ton = this.when(on) - DUCK_LOOKAHEAD_S
      const toff = this.when(off) + DUCK_HOLD_S - DUCK_LOOKAHEAD_S
      if (toff <= t) continue
      if (ton > t + RAMP_S) g.setTargetAtTime(d.floor, ton, DUCK_ATTACK_TAU_S)
      g.setTargetAtTime(1, Math.max(toff, t + RAMP_S), DUCK_RELEASE_TAU_S)
    }
  }

  // ------------------------------------------------------------ transport

  /** Fade the output to silence over 5 ms from `t` and stop every source
   *  shortly after; the graph keeps its structure (bus, master gains). */
  stopAll(t: number = this.ctx.currentTime, ramp = RAMP_S): void {
    holdAndRamp(this.out.gain, t, 0, ramp)
    const at = t + ramp + 0.002
    for (const nodes of this.clips.values()) {
      for (const s of nodes.sources) {
        try { s.node.stop(at) } catch { /* not started */ }
      }
    }
    this.dropClips(at)
  }

  /** Forget every clip node (after its sources stop at `at`). */
  private dropClips(at: number): void {
    const doomed = [...this.clips.values()]
    this.clips.clear()
    for (const b of this.buses.values()) {
      const gen = this.ctx.createGain()
      gen.connect(b.gain)
      const old = b.gen
      b.gen = gen
      const ctx = this.ctx
      if (this.live) {
        setTimeout(() => {
          try { old.disconnect() } catch { /* gone */ }
          for (const n of doomed) { try { n.shape.disconnect() } catch { /* gone */ } }
        }, Math.max(0, (at - ctx.currentTime) * 1000) + 50)
      }
    }
  }

  /** Start from a fresh anchor: output sample `anchor.sample` heard at
   *  `anchor.ctxTime`, the transport gain opening over the 5 ms before it
   *  (live) or open from the first sample (offline). */
  restart(anchor: Anchor, scheduleUntil: number): number {
    this.anchor = anchor
    this.startSample = anchor.sample
    for (const b of this.buses.values()) b.genStart = anchor.sample
    const t = this.when(anchor.sample)
    const g = this.out.gain as ParamLike
    if (this.live) {
      const from = Math.max(this.ctx.currentTime, t - RAMP_S)
      holdAndRamp(g, from, 1, Math.max(0.001, t - from))
    } else {
      g.setValueAtTime(1, 0)
    }
    this.applyDuck(anchor.sample)
    return this.schedule(anchor.sample, scheduleUntil)
  }

  /** Move the running sound onto a new anchor: every lane's current
   *  generation fades out over 5 ms from the next quantum while a fresh one,
   *  scheduled on the new anchor, fades in (§3.5 re-anchor). */
  reanchor(anchor: Anchor, scheduleUntil: number): number {
    const t0 = this.live ? this.ctx.currentTime + LIVE_LEAD_S : this.when(anchor.sample)
    for (const id of this.buses.keys()) this.newGeneration(id, t0)
    this.anchor = anchor
    const p = Math.ceil(this.sampleAt(t0))
    this.startSample = p
    for (const b of this.buses.values()) b.genStart = p
    this.applyDuck(p)
    this.stats.reschedules++
    return this.schedule(p, Math.max(scheduleUntil, p + BLOCK_SAMPLES))
  }

  /** Take a new plan while NOTHING is scheduled (paused): bus gains,
   *  master and limiter follow it at once; sources come with restart(). */
  idlePlan(next: AudioPlan): void {
    const prev = this.plan
    this.plan = next
    this.dropClips(this.ctx.currentTime)
    for (const b of next.buses) {
      const g = this.bus(b.id).gain.gain
      g.cancelScheduledValues(0)
      g.value = b.gain
    }
    if (masterShape(prev.master) !== masterShape(next.master)) this.rebuildMaster()
    else this.applyMasterParams(next.master, null)
  }

  /** DynamicsCompressorNode starts fully compressed and releases over its
   *  first ~100 ms of processing, silence included (measured in WebKit and
   *  Chromium: −17 dB at 0 ms, −1 dB at 40 ms, transparent from 100 ms).
   *  True once the current limiter has processed that long (or there is none). */
  limiterWarm(): boolean {
    return !this.limiter || this.ctx.currentTime - this.limiterBorn >= LIMITER_WARMUP_S
  }

  get hasLimiter(): boolean {
    return this.limiter !== null
  }

  /** Clips currently holding nodes (tests, telemetry). */
  get liveClips(): number {
    return this.clips.size
  }

  get liveSources(): number {
    let n = 0
    for (const c of this.clips.values()) n += c.sources.length
    return n
  }

  dispose(): void {
    this.disposed = true
    try { this.stopAll() } catch { /* closed */ }
    try { this.out.disconnect() } catch { /* gone */ }
  }
}

// ---------------------------------------------------------------- helpers

const irCache = new WeakMap<BaseAudioContext, Map<string, AudioBuffer>>()

/** A ConvolverNode over the Hall's impulse response (voiceFx.reverbIr: the
 *  export's `aevalsrc` IR), unnormalised — its levels are the effect's. */
function reverbNode(ctx: BaseAudioContext, p: ReverbParams): ConvolverNode {
  let cache = irCache.get(ctx)
  if (!cache) { cache = new Map(); irCache.set(ctx, cache) }
  const key = JSON.stringify(p)
  let buf = cache.get(key)
  if (!buf) {
    buf = toBuffer(ctx, reverbIr(p, 0), reverbIr(p, 1))
    cache.set(key, buf)
  }
  const conv = ctx.createConvolver()
  conv.normalize = false
  conv.buffer = buf
  return conv
}

function toBuffer(ctx: BaseAudioContext, L: Float32Array, R: Float32Array): AudioBuffer {
  const buf = ctx.createBuffer(2, Math.max(1, L.length), SR)
  if (L.length) {
    buf.copyToChannel(L as Float32Array<ArrayBuffer>, 0)
    buf.copyToChannel(R as Float32Array<ArrayBuffer>, 1)
  }
  return buf
}

/** Samples of real sound before clip `c`'s head its voice effect is primed
 *  with (review RE; `VoicePlan.prime`, render/audio_mix.VOICE_PRIME_S): a
 *  forward, sample-exact clip reaches that far back into its source — never
 *  before the file's first sample. Other maps (a retime, a curve, a reversed
 *  intermediate, which starts at the clip) are not primed. */
export function primeOf(c: ClipAudio): number {
  const pr = c.voice?.prime ?? 0
  if (pr <= 0 || c.map.kind !== 'runs') return 0
  const head = c.map.runs.find((r) => r[0] === 0)
  if (!head || head[3] !== 1) return 0
  return Math.max(0, Math.min(pr, head[2]))
}

/** Source sample range [a, b) the clip needs for output samples [p0, p1). */
export function sourceRange(c: ClipAudio, p0: number, p1: number): [number, number] | null {
  if (c.voice) {
    // the effect reads its margins (echo taps, filter warm-up, grains) and
    // its priming before the head
    p0 = Math.max(c.out0 - primeOf(c), p0 - c.voice.back)
    p1 = Math.min(c.out0 + c.n, p1 + c.voice.ahead)
  }
  if (p1 <= p0) return null
  if (c.map.kind === 'runs') {
    let lo = Infinity, hi = -Infinity
    const pr = primeOf(c)
    for (const [off, cnt, first, dir] of c.map.runs) {
      const r0 = Math.max(p0, c.out0 + off - (off === 0 ? pr : 0))
      const r1 = Math.min(p1, c.out0 + off + cnt)
      if (r1 <= r0) continue
      const s0 = first + dir * (r0 - c.out0 - off)
      const s1 = first + dir * (r1 - 1 - c.out0 - off)
      lo = Math.min(lo, s0, s1)
      hi = Math.max(hi, s0, s1)
    }
    return lo <= hi ? [Math.max(0, lo), hi + 1] : null
  }
  if (c.map.kind === 'curve') {
    const xs = curvePositions(c, p0, p1)
    return [Math.max(0, Math.floor(Math.min(xs[0], xs[xs.length - 1])) - 1), Math.ceil(Math.max(xs[0], xs[xs.length - 1])) + 3]
  }
  const { src0, rate, reverse } = c.map
  const x0 = src0 + (reverse ? -1 : 1) * (p0 - c.out0) * rate
  const x1 = src0 + (reverse ? -1 : 1) * (p1 - c.out0) * rate
  const lo = Math.floor(Math.min(x0, x1)) - 2
  const hi = Math.ceil(Math.max(x0, x1)) + 3
  return [Math.max(0, lo), hi]
}

const curveMaps = new WeakMap<object, CurveMap | null>()

/** Source positions (fractional samples) of output samples [a, b) of a
 *  speed-curve clip: src0 + 48000 · source_seconds((p − out0) / 48000). */
export function curvePositions(c: ClipAudio, a: number, b: number): Float64Array {
  const m = c.map as Extract<ClipAudio['map'], { kind: 'curve' }>
  let cm = curveMaps.get(m)
  if (cm === undefined) { cm = curveMap(m.points, m.seconds); curveMaps.set(m, cm) }
  const xs = new Float64Array(Math.max(1, b - a))
  for (let j = 0; j < xs.length; j++) {
    xs[j] = m.src0 + (cm ? SR * sourceSeconds(cm, (a + j - c.out0) / SR) : a + j - c.out0)
  }
  return xs
}

/** Key intervals merged across the hold (a gap shorter than the hold keeps
 *  the bed down). */
export function duckWindows(d: { key: Array<[number, number]> }): Array<[number, number]> {
  const hold = Math.round(DUCK_HOLD_S * SR)
  const out: Array<[number, number]> = []
  for (const [a, b] of d.key) {
    const last = out[out.length - 1]
    if (last && a - last[1] < hold) last[1] = Math.max(last[1], b)
    else out.push([a, b])
  }
  return out
}

/** The trapezoid's settled value at output sample `p` (1 or the floor). */
export function duckValueAt(d: { floor: number; key: Array<[number, number]> }, p: number): number {
  const look = DUCK_LOOKAHEAD_S * SR
  const hold = DUCK_HOLD_S * SR
  for (const [a, b] of duckWindows(d)) if (p >= a - look && p < b + hold - look) return d.floor
  return 1
}

type OfflineRender = { L: Float32Array; R: Float32Array; stats: MixGraph['stats'] }
type MakeOffline = (channels: number, length: number, rate: number) => OfflineAudioContext

/** Renders an offline mix may take before one is accepted (see below). */
export const OFFLINE_RENDER_ATTEMPTS = 6

async function renderOfflineOnce(plan: AudioPlan, reader: PcmReader, p0: number, p1: number,
                                  make: MakeOffline): Promise<OfflineRender> {
  // A limiter needs its warm-up (LIMITER_WARMUP_S of silence) and adds its
  // look-ahead; both are rendered and trimmed.
  const lat = masterLatency(plan.master)
  const pre = lat > 0 ? Math.round(LIMITER_WARMUP_S * SR) : 0
  const len = Math.max(1, pre + lat + p1 - p0)
  const ctx = make(2, len, SR)
  const g = new MixGraph(ctx, plan, { reader, compensateLatency: false })
  await g.prefetch(p0, p1)
  g.restart({ ctxTime: pre / SR, sample: p0 }, p1)
  const buf = await ctx.startRendering()
  const L = buf.getChannelData(0).slice(pre + lat, pre + lat + (p1 - p0))
  const R = buf.getChannelData(1).slice(pre + lat, pre + lat + (p1 - p0))
  return { L, R, stats: g.stats }
}

const sameRender = (a: OfflineRender, b: OfflineRender): boolean => {
  if (a.L.length !== b.L.length) return false
  for (let j = 0; j < a.L.length; j++) if (a.L[j] !== b.L[j] || a.R[j] !== b.R[j]) return false
  return true
}

/** Render output samples [p0, p1) of `plan` offline (the verification path
 *  of §8.4 and the P1-A1/A2 tests): stereo Float32Arrays, the limiter's
 *  warm-up and look-ahead trimmed.
 *
 *  Rendered until two renders AGREE sample for sample (wave E, d2-followups
 *  item 30). WebKit's `AudioBufferSourceNode::process` begins with
 *  `if (!m_processLock.tryLock()) { outputBus->zero(); return; }`: when
 *  another thread holds the node's lock for that quantum it outputs silence
 *  AND does not advance its playhead, so the rest of that source plays one
 *  render quantum late. Traced in WKWebView and Playwright WebKit
 *  (`render_repeat`, placement30): 128 zeros at a random quantum, then the
 *  source 128 samples late to its end (c0 from quantum 13 / 18, k1 at 1225,
 *  c3 at 1609 / 1644) — 5 of 1,280 renders before this; keeping every clip
 *  chain fed through the render did not change it (at least 7 of 2,560). Nothing in
 *  the graph can prevent another thread taking that lock, so the render is
 *  repeated: a skipped quantum lands at a random place, so two renders agree
 *  only on the true mix. Chromium's renders agree the first time — since the
 *  lanes are summed two at a time (`attachToMaster`; with every lane on the
 *  master's one input, three sounding lanes came out a few ULP apart from
 *  render to render and P1-A2 `mix` failed 1 run in 5); this path is the
 *  verification render, not playback, so the second render is its whole
 *  cost. */
export async function renderOffline(plan: AudioPlan, reader: PcmReader, p0: number, p1: number,
                                    make: MakeOffline = (c, l, r) => new OfflineAudioContext(c, l, r)): Promise<OfflineRender> {
  let prev = await renderOfflineOnce(plan, reader, p0, p1, make)
  for (let i = 1; i < OFFLINE_RENDER_ATTEMPTS; i++) {
    const next = await renderOfflineOnce(plan, reader, p0, p1, make)
    if (sameRender(prev, next)) return next
    prev = next
  }
  throw new Error(`offline mix: ${OFFLINE_RENDER_ATTEMPTS} renders of [${p0}, ${p1}) never agreed`)
}
