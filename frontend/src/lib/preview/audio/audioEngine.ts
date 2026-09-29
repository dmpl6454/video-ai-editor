// The live SOUND of the instant preview: the engine's AudioSink (engine.ts)
// over one AudioContext (instant preview spec §3.5 audio anchoring, §3.6).
//
// * start(at, from): output sample `from` is HEARD at context time `at`
//   (the engine's anchor, from getOutputTimestamp). Resumes the context in
//   the same task (the caller is inside the user's gesture). Called while
//   running it RE-ANCHORS: every lane cross-fades to a fresh generation over
//   5 ms.
// * The scheduling window runs from the playhead to +4 s and is refilled
//   every second; chunks are prefetched 8 s ahead.
// * stop(ms): the transport gain ramps to 0 over `ms`, every source stops,
//   and the context is suspended 20 ms later (unless playback restarted).
// * prepare(): a new program. While running, the difference is applied at
//   once — or at the sample reschedule() names — lane by lane: a lane whose
//   TIMING changed is rescheduled (5 ms cross-fade), a clip whose GAINS
//   changed has its automation rewritten from the edit (cancelAndHoldAtTime +
//   5 ms ramp); nothing else is touched.
// * A pause the engine did not issue (WebKit suspending the context, an
//   `interrupted` state) is reported through `onInterrupted`, so the engine
//   can pause the picture at the same k.

import type { AudioProgramInfo, AudioSink, ProgramDiff } from '../engine'
import type { EdlLike } from '../timeline/framePlan'
import type { AudioPlacement } from '../timeline/programMap'
import { samplesForFrames, type Rational } from '../timeline/timebase'
import { editLeadFrames } from '../clock/editLead'
import { buildAudioPlan, type AudioPlan } from './audioPlan'
import { limitingFrames } from './limiting'
import { AudioChunks, chunkReader, type PcmReader } from './audioChunks'
import { SAMPLE_RATE } from './curves'
import { BLOCK_SAMPLES, LIMITER_WARMUP_S, MixGraph, RAMP_S, renderOffline, sourceRange } from './mixGraph'
import { limiterWorkletReady, loadLimiterWorklet } from './limiterWorklet'

const SR = SAMPLE_RATE
export const WINDOW_S = 4
export const REFILL_S = 1
export const PREFETCH_S = 8
/** Suspend the context this long after a stop's ramp (§3.5). */
const SUSPEND_AFTER_MS = 20
const WHEN_RUNNING_POLL_MS = 20
/** soundHold: the sound this far past a start (s) — and the rest of the
 *  1 s block it reaches into, the graph schedules whole blocks — must be in
 *  memory before picture and sound run. */
const HOLD_AHEAD_S = 0.3

export interface AudioEngineOptions {
  chunks?: AudioChunks
  /** Route prefix for index.json / FLAC chunks (default /api/proxies). */
  proxyBase?: string
  createContext?: () => AudioContext
  /** Where the mix goes (default the context's destination; a test tap). */
  destination?: (ctx: AudioContext) => AudioNode
  /** The last known preview loudness gain (dB), for a canvas with a
   *  loudness target (APPROX, §7): the server preview's master gain, from
   *  GET /preview_loudness (PreviewController). */
  loudnessGainDb?: () => number | null
  /** Whether that gain was measured for the sound of `renderHash`: then
   *  loudnessOffDb() says how far the gain played is from it (over 1 dB the
   *  frames are APPROX 'audio:loudness'); not measured yet says nothing (K2,
   *  0.8.0 QA). Without it the sink says nothing about loudness (test
   *  harnesses that set the gain themselves). */
  loudnessCurrent?: (renderHash: string) => boolean
  /** The context stopped without us (suspended/interrupted by the system).
   *  The engine should pause the picture at the same k. */
  onInterrupted?: (state: string) => void
  /** Injectable clock and timers (tests). */
  now?: () => number
  setInterval?: (fn: () => void, ms: number) => unknown
  clearInterval?: (h: unknown) => void
  setTimeout?: (fn: () => void, ms: number) => unknown
}

interface Program {
  edl: EdlLike
  placements: readonly AudioPlacement[]
  info: AudioProgramInfo
  plan: AudioPlan
  /** The loudness gain (dB) the plan plays; null: none (the raw mix). */
  loudnessDb: number | null
}

function makeContext(): AudioContext {
  try {
    return new AudioContext({ sampleRate: SR, latencyHint: 'interactive' })
  } catch {
    return new AudioContext({ latencyHint: 'interactive' })
  }
}

type OutputTimestampish = { contextTime?: number; performanceTime?: number }

export class AudioEngine implements AudioSink {
  readonly chunks: AudioChunks
  readonly reader: PcmReader
  private ctx: AudioContext | null = null
  private graph: MixGraph | null = null
  private program: Program | null = null
  /** A prepared program not yet applied to the running graph. */
  private pending: Program | null = null
  private pendingFlush = false
  private running = false
  private timer: unknown = null
  private stopToken = 0
  /**
   * A suspend() WE issued (stop's idle suspend, the limiter warm-up) that has
   * not finished yet — through the resume that follows it when a start()
   * landed meanwhile. Its statechange is not an interruption (Final QA: a
   * restart a few ms after a stop saw 'running', skipped resume(), and the
   * late 'suspended' of our own suspend paused playback five frames in).
   */
  private ownSuspend: Promise<void> | null = null
  private readonly opts: AudioEngineOptions
  private keys = new Map<string, string | null>()
  /** The context whose output timestamps are of the RENDERED time (WebKit). */
  private renderedTimestamps: AudioContext | null = null
  /** soundLoadingFrames(), and its key (a change is told). */
  private loading: Array<[number, number]> = []
  private loadingKey = ''
  readonly stats = { starts: 0, reanchors: 0, stops: 0, applied: 0, interrupted: 0, missing: 0, warmups: 0 }

  constructor(opts: AudioEngineOptions = {}) {
    this.opts = opts
    this.chunks = opts.chunks ?? new AudioChunks({ base: opts.proxyBase })
    this.reader = chunkReader(this.chunks, (src) => this.keyOf(src))
    // a layout read while its sound was being built landed built (K2): its
    // peaks bound the limiter's ranges now
    this.chunks.onLayoutChange = () => {
      const p = this.pending ?? this.program
      if (p) this.refreshLimiting(p)
    }
  }

  // ------------------------------------------------------------ program

  private keyOf(src: string): string | null {
    if (this.keys.has(src)) return this.keys.get(src) ?? null
    const s = this.planning?.lookup(src) ?? this.program?.info.lookup(src) ?? this.pending?.info.lookup(src) ?? null
    const key = s?.proxy?.key ?? null
    if (key) this.keys.set(src, key)
    return key
  }

  /** Set by the preview engine (AudioSink): the limiting ranges changed. */
  onLimitingChange: (() => void) | null = null

  /** The program being planned: keyOf() must see ITS sources (on the first
   *  prepare there is no program yet, and every source read as silent). */
  private planning: AudioProgramInfo | null = null

  private planOf(edl: EdlLike, placements: readonly AudioPlacement[], program: AudioProgramInfo,
    loudnessDb: number | null): AudioPlan {
    this.planning = program
    try {
      return buildAudioPlan(edl, placements, program.R, {
        loudnessGainDb: loudnessDb,
        silent: (src) => this.reader.silent(src),
        peak: (src, a, b) => this.reader.peak?.(src, a, b) ?? null,
        exactLimiter: this.ctx !== null && limiterWorkletReady(this.ctx),
      })
    } finally {
      this.planning = null
    }
  }

  /** The planned master stage (gain × loudness, limiter ceiling) of the
   *  newest program; null before the first prepare. */
  plannedMaster(): AudioPlan['master'] | null {
    return (this.pending ?? this.program)?.plan.master ?? null
  }

  /** For the engine's classify (§7): how far (dB) the loudness gain the
   *  newest program plays (none: 0 dB, the raw mix) is from the gain the
   *  server measured for its render's sound. Over 1 dB the frames are APPROX
   *  'audio:loudness'. Undefined when that gain is not measured yet (K2,
   *  0.8.0 QA: no "≈ Loudness" on every fresh project — the controller has
   *  it measured promptly and keeps the wait in its telemetry), when the
   *  project has no loudness target, or no loudness source was given. */
  loudnessOffDb(): number | undefined {
    const p = this.pending ?? this.program
    const current = this.opts.loudnessCurrent
    if (!p || !current) return undefined
    const lufs = (p.edl.canvas as { loudness_lufs?: number | null } | undefined)?.loudness_lufs
    if (lufs === null || lufs === undefined) return undefined
    const measured = this.opts.loudnessGainDb?.() ?? null
    if (measured === null || !current(p.info.renderHash)) return undefined
    return Math.abs((p.loudnessDb ?? 0) - measured)
  }

  /** The loudness gain (or whether it is current) changed: re-plan the newest
   *  program with it — while playing, the master gain ramps to it at once —
   *  and have the engine reclassify. */
  refreshLoudness(): void {
    const p = this.pending ?? this.program
    if (!p) return
    this.prepare(p.edl, p.placements, p.info)
    if (this.running && this.pending) this.apply(Math.ceil(this.schedSample()))
    this.onLimitingChange?.()
  }

  /** The live graph's limiter is the alimiter worklet (or it has none): the
   *  mix over the ceiling is the server's. False before the first prepare. */
  get exactLimiter(): boolean {
    return this.graph?.exactLimiter ?? false
  }

  /** Output frame ranges this run could not play, or cannot yet, because
   *  their sound chunks were not in memory (final sweep 3, run 3): APPROX
   *  'audio:pending' — the preview is silent there, the export is not.
   *  Empty while stopped. */
  soundLoadingFrames(): Array<[number, number]> {
    return this.loading
  }

  /** Re-read the graph's gaps; tell the engine when they changed. */
  private noteGaps(): void {
    const g = this.graph
    const R = (this.program ?? this.pending)?.info.R
    const frames = g && R && this.running ? limitingFrames(g.soundGaps(), R) : []
    const key = frames.map(([a, b]) => `${a}-${b}`).join(',')
    if (key === this.loadingKey) return
    this.loadingKey = key
    this.loading = frames
    this.onLimitingChange?.()
  }

  /** Before a start at output sample `fromSample` (inside the user's
   *  gesture): null when the sound under it is in memory; else resumes the
   *  context, loads it, and resolves true once it is — false after
   *  `timeoutMs` (INSTANT_PREVIEW_SPEC §11.1: buffering ≤ 700 ms, together
   *  with the picture). A source with no proxy yet is not waited for (it is
   *  silence, and the engine labels it). */
  soundHold(fromSample: number, timeoutMs: number): Promise<boolean> | null {
    const prog = this.pending ?? this.program
    if (!prog) return null
    const p1 = (Math.floor((fromSample + HOLD_AHEAD_S * SR) / BLOCK_SAMPLES) + 1) * BLOCK_SAMPLES
    const loads: Array<Promise<void>> = []
    for (const c of prog.plan.clips) {
      if (!this.keyOf(c.src)) continue
      const r = sourceRange(c, Math.max(fromSample, c.out0), Math.min(p1, c.out0 + c.n))
      if (r && !this.reader.ready(c.src, r[0], r[1])) loads.push(this.reader.load(c.src, r[0], r[1]))
    }
    if (!loads.length) return null
    const ctx = this.context_()
    if (ctx.state !== 'running') void ctx.resume().catch(() => { /* reported by statechange */ })
    const later = this.opts.setTimeout ?? ((fn: () => void, ms: number) => setTimeout(fn, ms))
    // a hold nobody started after (paused meanwhile): suspend what we resumed
    const token = this.stopToken
    later(() => {
      if (token === this.stopToken && !this.running && ctx.state === 'running') this.suspendOwn(ctx)
    }, timeoutMs + 100)
    return new Promise<boolean>((resolve) => {
      later(() => resolve(false), timeoutMs)
      void Promise.allSettled(loads).then(() => resolve(true))
    })
  }

  /** Output frame ranges where the master limiter may work (APPROX). */
  limitingFrames(): Array<[number, number]> {
    const p = this.pending ?? this.program
    return p ? limitingFrames(p.plan.limiting, p.info.R) : []
  }

  /** The layouts landed: a peak that was unknown at prepare() is known now,
   *  so the limiting ranges (only) are re-derived and the engine told. */
  private refreshLimiting(prog: Program): void {
    if (prog !== this.program && prog !== this.pending) return
    const again = this.planOf(prog.edl, prog.placements, prog.info, prog.loudnessDb)
    const same = again.limiting.length === prog.plan.limiting.length &&
      again.limiting.every(([a, b], i) => a === prog.plan.limiting[i][0] && b === prog.plan.limiting[i][1])
    if (same) return
    prog.plan = { ...prog.plan, limiting: again.limiting, approx: again.approx }
    this.onLimitingChange?.()
  }

  prepare(edl: EdlLike, placements: readonly AudioPlacement[], program: AudioProgramInfo): void {
    this.keys.clear()
    const loudnessDb = this.opts.loudnessGainDb?.() ?? null
    const plan = this.planOf(edl, placements, program, loudnessDb)
    const next: Program = { edl, placements, info: program, plan, loudnessDb }
    // Layouts first: the reader needs them to answer ready()/copy().
    const layouts: Array<Promise<unknown>> = []
    for (const c of plan.clips) {
      const key = this.keyOf(c.src)
      if (key) layouts.push(this.chunks.layout(key).catch(() => null /* PENDING: retried on use */))
    }
    if (layouts.length) void Promise.all(layouts).then(() => this.refreshLimiting(next))
    if (!this.running || !this.graph) {
      this.program = next
      this.pending = null
      this.idle(next.plan)
      return
    }
    this.pending = next
    if (!this.pendingFlush) {
      // Applied at the default point unless reschedule()/setParams() in the
      // same task names one.
      this.pendingFlush = true
      queueMicrotask(() => {
        this.pendingFlush = false
        if (this.pending) this.apply(this.editSample())
      })
    }
  }

  /** A program while paused: the graph (master, limiter, buses) follows it
   *  at once, so a new limiter can warm up before the next play. */
  private idle(plan: AudioPlan): void {
    const ctx = this.context_()
    if (!this.graph) this.graph = this.makeGraph(ctx, plan)
    else this.graph.idlePlan(plan)
    if (this.graph.hasLimiter && !this.graph.limiterWarm()) this.warmUp()
  }

  private makeGraph(ctx: AudioContext, plan: AudioPlan): MixGraph {
    return new MixGraph(ctx, plan, {
      reader: this.reader, compensateLatency: true, destination: this.opts.destination?.(ctx),
      onMissing: () => { this.stats.missing++ },
    })
  }

  /** Run a paused context for LIMITER_WARMUP_S + margin so a new limiter
   *  is transparent by the next play (WebKit resumes without a gesture;
   *  elsewhere the first play's first ~100 ms carry the release, APPROX). */
  private warmUp(): void {
    const ctx = this.ctx
    if (!ctx || this.running || ctx.state === 'running' || ctx.state === 'closed') return
    this.stats.warmups++
    const token = this.stopToken
    void ctx.resume().catch(() => { /* needs a gesture here */ })
    const later = this.opts.setTimeout ?? ((fn: () => void, ms: number) => setTimeout(fn, ms))
    later(() => {
      if (token === this.stopToken && !this.running && ctx.state === 'running') this.suspendOwn(ctx)
    }, Math.round((LIMITER_WARMUP_S + 0.05) * 1000))
  }

  /** Suspend `ctx` ourselves. If a start() lands before the suspend does,
   *  resume once it has — start() cannot, since the state still read
   *  'running' when it looked. */
  private suspendOwn(ctx: AudioContext): void {
    const p: Promise<void> = ctx.suspend()
      .catch(() => { /* closed */ })
      .then(async () => {
        if (this.running && this.ctx === ctx && ctx.state !== 'running' && ctx.state !== 'closed') {
          await ctx.resume().catch(() => { /* reported by statechange */ })
        }
      })
      .finally(() => { if (this.ownSuspend === p) this.ownSuspend = null })
    this.ownSuspend = p
  }

  /** The output sample an edit while playing lands on: the picture's lead
   *  past the presented frame (clock/editLead.ts: 150 ms rounded up to whole
   *  frames, 5 at 30 fps, 4 at 24, 9 at 60; it was 6 frames, 250 ms at 24). */
  private editSample(): number {
    const R = this.program?.info.R ?? this.pending?.info.R
    const lead = R ? samplesForFrames(editLeadFrames(R), R) : Math.round(0.15 * SR)
    return Math.round(this.heardSample()) + lead
  }

  private apply(fromSample: number): void {
    const next = this.pending
    if (!next || !this.graph) return
    this.pending = null
    this.program = next
    this.graph.setPlan(next.plan, Math.max(fromSample, Math.ceil(this.schedSample())), this.windowEnd())
    this.stats.applied++
    this.refill()
  }

  reschedule(_dirty: ProgramDiff, fromSample: number): void {
    if (this.pending) this.apply(fromSample)
  }

  /** A gain-only edit of `clipId`: the pending program applies NOW (its
   *  diff rewrites only the clips whose gains changed). */
  setParams(clipId: string): void {
    if (this.pending && clipId !== undefined) this.apply(Math.ceil(this.schedSample()))
  }

  /** The program's current sound plan (tests, telemetry). */
  get plan(): AudioPlan | null {
    return this.pending?.plan ?? this.program?.plan ?? null
  }

  get isRunning(): boolean {
    return this.running
  }

  get context(): AudioContext | null {
    return this.ctx
  }

  // ------------------------------------------------------------ clock

  private context_(): AudioContext {
    if (!this.ctx) {
      const ctx = (this.opts.createContext ?? makeContext)()
      this.ctx = ctx
      ctx.addEventListener?.('statechange', () => this.onState())
      void loadLimiterWorklet(ctx).then((ok) => { if (ok && this.ctx === ctx) this.limiterReady() })
    }
    return this.ctx
  }

  /** The alimiter worklet is registered on the context: the graph's limiter
   *  stages move onto it, and the plans drop their `limiting` ranges (the
   *  mix over the ceiling is the server's now) — the engine reclassifies. */
  private limiterReady(): void {
    this.graph?.upgradeLimiter()
    for (const p of [this.program, this.pending]) if (p) this.refreshLimiting(p)
  }

  private onState(): void {
    const st = this.ctx?.state as string | undefined
    if (this.running && st && st !== 'running') {
      // Ours: a suspend we issued landing after a start() — suspendOwn()
      // resumes it. Anything else is not ours (stop() clears `running`
      // before it suspends).
      if (st === 'suspended' && this.ownSuspend) return
      this.stats.interrupted++
      this.halt(RAMP_S * 1000)
      this.opts.onInterrupted?.(st)
    }
  }

  private perfNow(): number {
    return (this.opts.now ?? (() => performance.now()))()
  }

  ctxTimeAt(perfMs: number): number | null {
    const ctx = this.ctx
    if (!ctx) return null
    const ts = (ctx.getOutputTimestamp?.() ?? {}) as OutputTimestampish
    const outLat = (ctx as { outputLatency?: number }).outputLatency ?? 0
    if (ctx.state === 'running' && ts.contextTime && ts.performanceTime) {
      const t = ts.contextTime + (perfMs - ts.performanceTime) / 1000
      // The spec's contextTime is the sample the DEVICE outputs, at least
      // outputLatency behind what is being rendered. WebKit (measured in
      // WKWebView, macOS 27) returns the rendered time (currentTime − one
      // quantum) with performanceTime = now, leaving out outputLatency
      // (15.6 ms): take it out here, or sound lands that late (R11).
      // One sighting of a timestamp less than outputLatency behind currentTime
      // settles it for this context (a stale timestamp can look further behind).
      const renderedLag = ctx.currentTime - (ts.contextTime + (this.perfNow() - ts.performanceTime) / 1000)
      if (outLat > 0 && renderedLag < outLat / 2) this.renderedTimestamps = ctx
      return this.renderedTimestamps === ctx ? t - outLat : t
    }
    // No timestamp (a device change, a suspended context): what is rendered
    // at currentTime is heard base + output latency later.
    const lat = (ctx.baseLatency ?? 0) + ((ctx as { outputLatency?: number }).outputLatency ?? 0)
    return ctx.currentTime - lat + (perfMs - this.perfNow()) / 1000
  }

  /** The output sample being heard now. */
  heardSample(): number {
    const g = this.graph
    if (!g) return 0
    const t = this.ctxTimeAt(this.perfNow())
    return t === null ? g.anchor.sample : g.anchor.sample + (t - g.anchor.ctxTime) * SR
  }

  /** The output sample the context is rendering now (≥ heard). */
  private schedSample(): number {
    const g = this.graph
    return g && this.ctx ? g.sampleAt(this.ctx.currentTime) : 0
  }

  private windowEnd(): number {
    return Math.ceil(this.schedSample()) + WINDOW_S * SR
  }

  /** True once the context runs AND its clock moves (see AudioSink). Resumes
   *  it if needed; polls every 20 ms; false after `timeoutMs`. */
  whenRunning(timeoutMs: number): Promise<boolean> {
    const ctx = this.context_()
    if (ctx.state !== 'running') void ctx.resume().catch(() => { /* reported by statechange */ })
    const later = this.opts.setTimeout ?? ((fn: () => void, ms: number) => setTimeout(fn, ms))
    const polls = Math.max(1, Math.ceil(timeoutMs / WHEN_RUNNING_POLL_MS))
    return new Promise((resolve) => {
      let last = ctx.currentTime
      let n = 0
      const poll = () => {
        if (this.ctx !== ctx || ctx.state === 'closed') return resolve(false)
        const t = ctx.currentTime
        if (ctx.state === 'running' && t > last) return resolve(true)
        last = t
        if (++n >= polls) return resolve(false)
        later(poll, WHEN_RUNNING_POLL_MS)
      }
      later(poll, WHEN_RUNNING_POLL_MS)
    })
  }

  // ------------------------------------------------------------ transport

  start(atCtxTime: number, fromSample: number): void {
    const ctx = this.context_()
    if (ctx.state !== 'running') void ctx.resume().catch(() => { /* reported by statechange */ })
    const program = this.pending ?? this.program
    if (!program) return
    this.pending = null
    this.program = program
    this.stopToken++
    if (this.running && this.graph) {
      this.stats.reanchors++
      if (this.graph.plan !== program.plan) this.graph.setPlan(program.plan, fromSample, fromSample)
      this.graph.reanchor({ ctxTime: atCtxTime, sample: fromSample }, fromSample + WINDOW_S * SR)
    } else {
      this.stats.starts++
      if (!this.graph) this.graph = this.makeGraph(ctx, program.plan)
      else if (this.graph.plan !== program.plan) this.graph.idlePlan(program.plan)
      this.running = true
      this.graph.restart({ ctxTime: atCtxTime, sample: fromSample }, fromSample + WINDOW_S * SR)
    }
    this.running = true
    this.startTimer()
    this.refill()
  }

  stop(rampMs: number): void {
    if (!this.running) return
    this.stats.stops++
    this.halt(rampMs)
    const token = ++this.stopToken
    const ctx = this.ctx
    const later = this.opts.setTimeout ?? ((fn: () => void, ms: number) => setTimeout(fn, ms))
    later(() => {
      if (token === this.stopToken && !this.running && ctx && ctx.state === 'running') this.suspendOwn(ctx)
    }, rampMs + SUSPEND_AFTER_MS)
  }

  /** Silence now (ramp), forget the schedule, keep the program. */
  private halt(rampMs: number): void {
    this.running = false
    this.stopTimer()
    const g = this.graph
    if (g && this.ctx) g.stopAll(this.ctx.currentTime, Math.max(0.001, rampMs / 1000))
    this.noteGaps()
  }

  private startTimer(): void {
    if (this.timer !== null) return
    const every = this.opts.setInterval ?? ((fn: () => void, ms: number) => setInterval(fn, ms))
    this.timer = every(() => this.refill(), REFILL_S * 1000)
  }

  private stopTimer(): void {
    if (this.timer === null) return
    const clear = this.opts.clearInterval ?? ((h: unknown) => clearInterval(h as ReturnType<typeof setInterval>))
    clear(this.timer)
    this.timer = null
  }

  /** Top up the window [now, now + 4 s] and prefetch chunks 8 s ahead; a
   *  block whose chunks were missing is scheduled when they arrive. */
  refill(): void {
    const g = this.graph
    if (!g || !this.running) return
    const p0 = Math.max(0, Math.floor(this.schedSample()))
    g.schedule(p0, p0 + WINDOW_S * SR)
    const loads: Array<Promise<void>> = []
    for (const c of g.plan.clips) {
      const r = sourceRange(c, Math.max(p0, c.out0), Math.min(p0 + PREFETCH_S * SR, c.out0 + c.n))
      if (r && !this.reader.ready(c.src, r[0], r[1])) loads.push(this.reader.load(c.src, r[0], r[1]))
    }
    if (loads.length) {
      void Promise.allSettled(loads).then(() => {
        if (this.running && this.graph === g) {
          const q = Math.max(0, Math.floor(this.schedSample()))
          g.schedule(q, q + WINDOW_S * SR)
          this.noteGaps()
        }
      })
    }
    this.noteGaps()
  }

  /** Render output samples [p0, p1) of the current program offline — the
   *  same graph code, on an OfflineAudioContext (§8.4). */
  async renderRange(p0: number, p1: number): Promise<{ L: Float32Array; R: Float32Array }> {
    const plan = this.plan
    if (!plan) throw new Error('no program')
    return renderOffline(plan, this.reader, p0, p1)
  }

  dispose(): void {
    this.halt(RAMP_S * 1000)
    this.graph?.dispose()
    this.graph = null
    const ctx = this.ctx
    this.ctx = null
    if (ctx) void ctx.close().catch(() => { /* closed */ })
    this.chunks.onLayoutChange = null
    this.chunks.clear()
  }
}

/** Rate helper for callers holding a Rational: S(k). */
export const sampleOfFrame = (k: number, R: Rational): number => samplesForFrames(k, R)
