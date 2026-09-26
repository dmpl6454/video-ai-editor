// The client preview in the APP (INSTANT_PREVIEW_SPEC §3.5, §4.1-4.4, §7,
// §9.2): the one object between the store / Preview.tsx and the engine.
//
// * Timelines. Every committed edit arrives WITH its EDL and render hash
//   (`/dispatch?include=edl`): `applyTimeline(edl, hash)` hands it to the
//   engine at once. An EDL from anywhere else (the first load, an import, a
//   job, another window) arrives without a hash: it is shown at once too,
//   and the hash is fetched (`get_timeline`, read-only, `include=edl`).
// * Sources. The engine asks `lookup(src)`; an unknown src is looked up in
//   the background (`GET /proxy?src=`: proxy key, stream facts) and the
//   timeline is re-applied when it lands. The exact SourceInfo table comes
//   with every `/frame_map` answer and replaces the provisional one.
// * Structural check (R14, §8.2). After each hashed timeline the server's
//   `/frame_map` of that hash is fetched; its sources are absorbed first (so
//   both maps are built from the same numbers), then `DivergenceChecker`
//   compares, and any disagreeing range is demoted to BAKED. Latest wins,
//   never blocking. A mismatch rate above 5% falls back to server mode (§7).
// * Bakes (§4.1 step 8, §5.3). When a preview render of the CURRENT hash
//   lands, the engine splices its bake spans over the RAW frames of the
//   BAKED ranges.
// * Transport. `play()` is synchronous: the store calls it inside the key or
//   click handler, so the engine's `laneA.play()` and `AudioContext.resume()`
//   run in the user's gesture (§3.5).
// * The engine lives while the Preview is mounted (`attach`/`detach`); the
//   controller keeps the timeline and sources across remounts.

import { createPreviewEngine, type AudioSink, type ClientPreviewEngine, type EngineOptions, type EngineSource, type EngineStatus } from './engine'
import { AudioEngine } from './audio/audioEngine'
import { DivergenceChecker, type DivergenceEvent, type FrameMapBody } from './verify/divergence'
import type { EdlLike } from './timeline/framePlan'
import { defaultTimeBase, sourceFromJson, type SourceInfo, type SourceInfoJson } from './timeline/frameMap'
import { frameOf, rateOf } from './timeline/timebase'
import { MODE_BAKED } from './timeline/support'

export type FetchFn = (url: string, init?: RequestInit) => Promise<Response>

/** What the corner spinner is waiting for (§7), or null for nothing. */
export type WaitKind = 'pending' | 'buffering' | 'baking' | null

export interface ControllerView {
  /** The engine runs (attached and in client mode). */
  live: boolean
  playing: boolean
  wait: WaitKind
  /** Fidelity class at the presented frame (§7): EXACT, APPROX, BAKED, PENDING. */
  modeAtPlayhead: number
  presentedK: number
}

export interface ControllerOptions {
  sessionId: string
  fetch?: FetchFn
  /** Tests inject the engine factory (a fake, or one with a tapped sink). */
  createEngine?: (opts: EngineOptions) => ClientPreviewEngine
  /** The sound: an AudioEngine by default; false for none (NullAudioSink). */
  audio?: false | ((onInterrupted: () => void) => AudioSink)
  engineOptions?: EngineOptions
  /** The engine cannot run here after all (§7): switch the app to server mode. */
  onFallback?(reason: string): void
  /** Play state the engine decided on its own (end of timeline, a pause
   *  WebKit made, the resume after it). */
  onPlaying?(playing: boolean): void
  /** The spinner / buffering / fidelity view changed. */
  onView?(view: ControllerView): void
  /** A timeline fetched to learn its hash: the store may adopt it. */
  onServerTimeline?(edl: EdlLike, edlHash: string): void
  telemetry?(e: DivergenceEvent): void
  /** Retry delay while a source's proxy is still being probed (ms). */
  sourceRetryMs?: number
}

interface ProxySummary {
  key?: string
  state?: string
  w?: number
  h?: number
  frames?: number
  src_rate?: { num: number; den: number }
}

interface Known {
  source: EngineSource
  /** The info came from `/frame_map` (exact), not the proxy summary. */
  exact: boolean
}

const MAX_SOURCE_TRIES = 40
const MAX_MAP_TRIES = 25

const sameInfo = (a: SourceInfo, b: SourceInfo) =>
  a.frames === b.frames && a.w === b.w && a.h === b.h && a.startTicks === b.startTicks
  && a.rate.num * b.rate.den === b.rate.num * a.rate.den && a.tb.num * b.tb.den === b.tb.num * a.tb.den

export class PreviewController {
  readonly sessionId: string
  private readonly opts: ControllerOptions
  private readonly fetchFn: FetchFn
  private readonly checker: DivergenceChecker
  private engineRef: ClientPreviewEngine | null = null
  private offs: Array<() => void> = []
  private edl: EdlLike | null = null
  private hash: string | null = null
  private previewHash: string | null = null
  private readonly known = new Map<string, Known>()
  private readonly loading = new Map<string, number>()
  private pushQueued = false
  private verifyGen = 0
  private syncGen = 0
  private disposed = false
  private lastPlaying = false
  private lastView = ''
  /** One-shot answers the divergence checker reads instead of the network. */
  private readonly served = new Map<string, FrameMapBody>()
  /** The EDL object last handed over (the store's subscription skips it). */
  appliedEdl: EdlLike | null = null
  readonly stats = { applied: 0, verified: 0, mismatches: 0, splices: 0, hashSyncs: 0, sourceLookups: 0 }

  constructor(opts: ControllerOptions) {
    this.opts = opts
    this.sessionId = opts.sessionId
    this.fetchFn = opts.fetch ?? ((url, init) => fetch(url, init))
    const base = `/api/sessions/${encodeURIComponent(opts.sessionId)}`
    this.checker = new DivergenceChecker({
      url: (h) => `${base}/frame_map?h=${encodeURIComponent(h)}`,
      fetch: (url, init) => {
        const body = this.served.get(url)
        if (body) {
          this.served.delete(url)
          return Promise.resolve({ status: 200, ok: true, headers: { get: () => null }, json: () => Promise.resolve(body) })
        }
        return this.fetchFn(url, init as RequestInit)
      },
      telemetry: (e) => {
        if (e.type === 'mismatch') this.stats.mismatches++
        this.opts.telemetry?.(e)
      },
    })
  }

  private get base(): string {
    return `/api/sessions/${encodeURIComponent(this.sessionId)}`
  }

  get engine(): ClientPreviewEngine | null {
    return this.engineRef
  }

  get renderHash(): string | null {
    return this.hash
  }

  get timeline(): EdlLike | null {
    return this.edl
  }

  // ------------------------------------------------------------ mounting

  /** Mount the engine in `host` (Preview.tsx's canvas box). */
  attach(host: HTMLElement): ClientPreviewEngine | null {
    if (this.disposed) return null
    if (this.engineRef) this.detach()
    let engine: ClientPreviewEngine | null = null
    const audio = this.opts.audio === false ? undefined
      : (this.opts.audio ?? ((onInt: () => void) => new AudioEngine({ onInterrupted: onInt })))(
        () => engine?.pauseExternal())
    const eo: EngineOptions = { bakeBaseUrl: `${this.base}/bake`, ...this.opts.engineOptions, audioSink: audio }
    engine = (this.opts.createEngine ?? createPreviewEngine)(eo)
    this.engineRef = engine
    this.offs.push(engine.on('status', (st) => this.onStatus(st)))
    this.offs.push(engine.on('frame', () => this.emitView()))
    engine.attach(host)
    if (engine.status.mode === 'server') {
      this.opts.onFallback?.(engine.status.reason ?? 'engine')
      return engine
    }
    if (this.edl) this.pushTimeline()
    return engine
  }

  detach(): void {
    for (const off of this.offs) off()
    this.offs = []
    this.engineRef?.destroy()
    this.engineRef = null
    this.lastPlaying = false
  }

  dispose(): void {
    this.detach()
    this.disposed = true
    this.checker.cancel()
    this.verifyGen++
    this.syncGen++
  }

  // ------------------------------------------------------------ timelines

  /** A committed EDL. `renderHash` null: it did not come from a dispatch
   *  answer, so the hash is fetched (and the EDL with it). */
  applyTimeline(edl: EdlLike, renderHash: string | null): void {
    if (this.disposed) return
    this.edl = edl
    this.appliedEdl = edl
    this.hash = renderHash
    this.stats.applied++
    this.pushTimeline()
    if (renderHash) {
      void this.verify(renderHash)
      if (this.previewHash === renderHash) this.splice()
    } else {
      void this.syncHash()
    }
  }

  /** The engine's lookup of `src` (null: unknown yet → PENDING; asked for). */
  lookup = (src: string): EngineSource | null => {
    const k = this.known.get(src)
    if (k) return k.source
    this.loadSource(src)
    return null
  }

  private pushTimeline(): void {
    const engine = this.engineRef
    if (!engine || !this.edl || engine.status.mode === 'server') return
    engine.setTimeline(this.edl, this.hash ?? '', this.lookup)
    this.emitView()
  }

  /** Coalesce re-applies (many sources landing at once) into one. */
  private queuePush(): void {
    if (this.pushQueued) return
    this.pushQueued = true
    queueMicrotask(() => {
      this.pushQueued = false
      this.pushTimeline()
    })
  }

  private async syncHash(): Promise<void> {
    const gen = ++this.syncGen
    this.stats.hashSyncs++
    try {
      const r = await this.fetchFn(`${this.base}/dispatch?include=edl`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tool: 'get_timeline', args: { summary: true } }),
      })
      if (!r.ok || gen !== this.syncGen || this.disposed) return
      const body = await r.json() as { edl?: EdlLike; edl_hash?: string; render_hash?: string }
      if (gen !== this.syncGen || this.disposed || !body.render_hash) return
      if (body.edl) {
        // marked as handed over FIRST: the store adopting it must not come
        // back here as "an EDL from elsewhere"
        this.appliedEdl = body.edl
        this.opts.onServerTimeline?.(body.edl, body.edl_hash ?? '')
        this.applyTimeline(body.edl, body.render_hash)
      }
    } catch {
      // offline, or a prompt run holds the session: the hashless timeline
      // stays on screen; the next edit carries a hash
    }
  }

  // ------------------------------------------------------------ sources

  private standIn(summary: ProxySummary): SourceInfo {
    const canvas = (this.edl?.canvas ?? {}) as { w?: number; h?: number; fps?: number }
    const rate = summary.src_rate ? { num: summary.src_rate.num, den: summary.src_rate.den } : rateOf(canvas.fps ?? 30)
    return {
      rate, tb: defaultTimeBase(rate), frames: summary.frames ?? 1 << 21, startTicks: 0,
      w: summary.w ?? canvas.w ?? 1920, h: summary.h ?? canvas.h ?? 1080,
    }
  }

  private loadSource(src: string): void {
    if (this.loading.has(src) || this.disposed) return
    this.loading.set(src, 0)
    void this.tryLoadSource(src)
  }

  private async tryLoadSource(src: string): Promise<void> {
    for (let attempt = 0; attempt < MAX_SOURCE_TRIES && !this.disposed; attempt++) {
      this.stats.sourceLookups++
      let summary: ProxySummary | null = null
      let status: number
      try {
        const r = await this.fetchFn(`${this.base}/proxy?src=${encodeURIComponent(src)}`)
        status = r.status
        if (r.ok) summary = await r.json() as ProxySummary
      } catch {
        status = 0
      }
      if (this.disposed) return
      if (status === 403 || status === 404 || status === 400) {
        // not a file this session may play (offline media): its frames stay
        // PENDING; the server's own render covers it
        this.loading.delete(src)
        return
      }
      if (summary?.key) {
        const failed = summary.state === 'failed'
        const ready = typeof summary.frames === 'number' && summary.frames > 0
        if (ready || failed) {
          const prev = this.known.get(src)
          const info = prev?.exact ? prev.source.info : this.standIn(summary)
          this.known.set(src, {
            source: { info, proxy: { key: summary.key, state: failed ? 'failed' : (summary.state as 'ready' | 'partial' | 'pending') ?? 'ready' } },
            exact: prev?.exact ?? false,
          })
          this.loading.delete(src)
          this.queuePush()
          return
        }
      }
      await new Promise((res) => setTimeout(res, Math.min(2000, (this.opts.sourceRetryMs ?? 250) * (1 + attempt / 4))))
    }
    this.loading.delete(src)
  }

  /** The `/frame_map` answer's SourceInfo table replaces provisional infos. */
  private absorbSources(sources: Record<string, SourceInfoJson> | undefined): boolean {
    let changed = false
    for (const [src, j] of Object.entries(sources ?? {})) {
      const info = sourceFromJson(j)
      const prev = this.known.get(src)
      if (prev?.exact && sameInfo(prev.source.info, info)) continue
      if (prev && sameInfo(prev.source.info, info)) {
        this.known.set(src, { source: prev.source, exact: true })
        continue
      }
      this.known.set(src, { source: { info, proxy: prev?.source.proxy ?? null }, exact: true })
      if (!prev?.source.proxy) this.loadSource(src)
      changed = true
    }
    return changed
  }

  // ------------------------------------------------------ structural check

  private async fetchFrameMap(h: string, gen: number): Promise<FrameMapBody | null> {
    const url = `${this.base}/frame_map?h=${encodeURIComponent(h)}`
    for (let i = 0; i < MAX_MAP_TRIES; i++) {
      let r: Response
      try {
        r = await this.fetchFn(url, { headers: { Accept: 'application/json' } })
      } catch {
        return null
      }
      if (gen !== this.verifyGen || this.disposed) return null
      if (r.status === 202) {
        const ra = Number.parseFloat(r.headers.get('Retry-After') ?? '')
        await new Promise((res) => setTimeout(res, Number.isFinite(ra) && ra >= 0 ? Math.min(2000, ra * 1000) : 200))
        if (gen !== this.verifyGen) return null
        continue
      }
      if (!r.ok) return null
      return await r.json() as FrameMapBody
    }
    return null
  }

  private async verify(h: string): Promise<void> {
    const gen = ++this.verifyGen
    const body = await this.fetchFrameMap(h, gen)
    if (!body || gen !== this.verifyGen || h !== this.hash || this.disposed) return
    if (this.absorbSources(body.sources)) this.pushTimeline()
    const pm = this.engineRef?.program
    if (!pm || !this.engineRef || this.engineRef.status.mode === 'server') return
    const url = `${this.base}/frame_map?h=${encodeURIComponent(h)}`
    this.served.set(url, body)
    const out = await this.checker.check(h, pm, body.sources)
    this.served.delete(url)
    if (out.outcome === 'superseded' || gen !== this.verifyGen) return
    this.stats.verified++
    if (out.outcome === 'mismatch' && out.demote.length) {
      this.engineRef?.setDemoted(h, out.demote)
      if (this.previewHash === h) this.splice()
    }
    if (this.checker.shouldFallback()) this.opts.onFallback?.('frame-map-mismatch')
  }

  /** Verdicts so far (tests, telemetry). */
  divergence(): ReturnType<DivergenceChecker['stats']> & { recent: readonly DivergenceEvent[] } {
    return { ...this.checker.stats(), recent: this.checker.recent() }
  }

  // ------------------------------------------------------------ bakes

  /** A preview render landed (store.previewHash). */
  onPreviewLanded(previewHash: string | null): void {
    this.previewHash = previewHash
    if (previewHash && previewHash === this.hash) this.splice()
  }

  private splice(): void {
    if (this.engineRef?.spliceBake(this.hash ?? '')) this.stats.splices++
    this.emitView()
  }

  /** Any BAKED range in the current program: the server render is needed
   *  soon (250 ms), not at the 1.5 s idle cadence (§4.1 step 8). */
  needsBake(): boolean {
    return (this.engineRef?.status.ranges ?? []).some((r) => r.mode === MODE_BAKED)
  }

  // ------------------------------------------------------------ transport

  /** Inside the gesture: start picture and sound from `fromTime` (s). */
  play(fromTime?: number): boolean {
    const engine = this.engineRef
    if (!engine || engine.status.mode === 'server') return false
    if (fromTime !== undefined && !engine.playing) {
      const k = this.frameAt(fromTime)
      if (k !== engine.targetK) engine.seek(k)
    }
    engine.play()
    this.lastPlaying = engine.playing
    this.emitView()
    return engine.playing
  }

  pause(): void {
    const engine = this.engineRef
    if (!engine) return
    engine.pause()
    this.lastPlaying = false
    this.emitView()
  }

  /** Show the frame at `t` seconds (paused seek; while playing, restart
   *  picture and sound there). */
  seekTime(t: number): void {
    const engine = this.engineRef
    if (!engine?.program) return
    const k = this.frameAt(t)
    if (!engine.playing && k === engine.targetK) return
    engine.seek(k)
  }

  frameAt(t: number): number {
    const fps = (this.edl?.canvas as { fps?: number } | undefined)?.fps ?? 30
    return Math.max(0, frameOf(Math.max(0, t), fps))
  }

  /** Seconds of the frame on screen (the overlays' clock). */
  now(): number {
    return this.engineRef?.clock.now() ?? 0
  }

  // ------------------------------------------------------------ state out

  private onStatus(st: EngineStatus): void {
    if (st.mode === 'server') {
      this.opts.onFallback?.(st.reason ?? 'engine')
      return
    }
    if (st.playing !== this.lastPlaying) {
      this.lastPlaying = st.playing
      this.opts.onPlaying?.(st.playing)
    }
    this.emitView()
  }

  view(): ControllerView {
    const engine = this.engineRef
    const st = engine?.status
    if (!engine || !st) return { live: false, playing: false, wait: null, modeAtPlayhead: 0, presentedK: 0 }
    const k = engine.playing ? engine.presentedK : engine.targetK
    const range = st.ranges.find((r) => k >= r.k0 && k < r.k1)
    const mode = range?.mode ?? 0
    let wait: WaitKind = null
    if (st.buffering) wait = 'buffering'
    else if (st.spinner) wait = 'pending'
    else if (mode === MODE_BAKED && !engine.isBakedFrame(k)) wait = 'baking'
    return { live: st.mode === 'client', playing: st.playing, wait, modeAtPlayhead: mode, presentedK: st.presentedK }
  }

  private emitView(): void {
    if (!this.opts.onView) return
    const v = this.view()
    const key = `${v.live}|${v.playing}|${v.wait}|${v.modeAtPlayhead}`
    if (key === this.lastView) return
    this.lastView = key
    this.opts.onView(v)
  }
}
