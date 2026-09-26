// The instant-preview engine itself (INSTANT_PREVIEW_SPEC §3, §4.1, §4.3,
// §7, §10): DOM (one muted laneA <video> under an opaque WebGL2 canvas),
// transport on the presented clock, the paused-seek state machine, and
// context loss. The public contract is in engine.ts; the program → laneA
// feed in engineFeed.ts; currentTime and WebKit's seek events in
// engineSeek.ts; pauses WebKit makes on its own in engineExternal.ts; the
// bake splice in engineBake.ts.

import type {
  AudioSink, EngineClock, EngineEvents, EngineMode, EngineSourceLookup, EngineStatus, PreviewEngine, ProgramDiff,
} from './engine'
import type { EdlLike } from './timeline/framePlan'
import { KIND_GAP, type ProgramMap } from './timeline/programMap'
import type { Support } from './timeline/support'
import { rateOf, samplesForFrames, ticksPerFrameExact, type Rational } from './timeline/timebase'
import { LaneA, browserMedia } from './media/laneA'
import { ProxyStore } from './media/proxyIndex'
import { Compositor } from './render/compositor'
import type { Size } from './render/geometry'
import { PresentedClock } from './clock/presentedClock'
import { AudioSync } from './clock/audioSync'
import { ProgramFeed } from './engineFeed'
import { layoutCanvas, mountEngineDom } from './engineDom'
import { Emitter, NullAudioSink, engineUnsupportedReason, type EngineOptions } from './engineOptions'
import { ElementSeeker, PlayingSeekGate } from './engineSeek'
import { DelayedFlag, drawProgramFrame, uploadProgramFrame } from './engineDraw'
import { ExternalPauses, type ExternalCause } from './engineExternal'
import { BakeSplice } from './engineBake'
import { FrameLoop, type PresentedMeta } from './engineLoop'

export { NullAudioSink, engineUnsupportedReason, type EngineOptions }

const SPINNER_MS = 80
const TOPUP_MS = 250
const PAUSE_RAMP_MS = 5
const CONTEXT_RESTORE_MS = 2000
/** A proxy that failed to open for a transient reason is tried again after this. */
const PROXY_REOPEN_MS = 10_000

export function createPreviewEngine(opts: EngineOptions = {}): ClientPreviewEngine {
  return new ClientPreviewEngine(opts)
}

export class ClientPreviewEngine implements PreviewEngine {
  readonly clock: EngineClock
  private readonly opts: EngineOptions
  private readonly store: ProxyStore
  private sink: AudioSink
  private readonly events = new Emitter<EngineEvents>()

  // DOM
  private host: HTMLElement | null = null
  private root: HTMLDivElement | null = null
  private canvas: HTMLCanvasElement | null = null
  private snapCanvas: HTMLCanvasElement | null = null
  private video: HTMLVideoElement | null = null
  private resizeObs: ResizeObserver | null = null
  private compositor: Compositor | null = null
  private lane: LaneA | null = null

  // program
  private R: Rational = { num: 30, den: 1 }
  private edl: EdlLike | null = null
  private renderHash = ''
  private pm: ProgramMap | null = null
  private support: Support | null = null
  private canvasEdl: Size = { w: 1080, h: 1920 }
  private readonly feed: ProgramFeed
  private readonly bake: BakeSplice

  // transport and display
  private presented = 0
  private target = 0
  private _playing = false
  private intent: 'play' | 'pause' = 'pause'
  private buffering = false
  private readonly spinner = new DelayedFlag(SPINNER_MS, () => this.emitStatus())
  /** Frame the element was last seeked to (paused), −1 unknown. An append
   *  over it resets this: WebKit hands back black for an overwritten frame
   *  until a re-seek, so the only uploads are at a completed 'seeked' and in
   *  rVFC while playing (never between an overwrite and its re-seek). */
  private elementFrame = -1
  private readonly seeker: ElementSeeker
  private readonly external: ExternalPauses
  private readonly playingSeek = new PlayingSeekGate()
  private mode: EngineMode = 'client'
  private reason: string | null = null
  private contextTimer: ReturnType<typeof setTimeout> | null = null
  private reopenTimer: ReturnType<typeof setTimeout> | null = null
  private destroyed = false

  // clock / audio
  private readonly pclock: PresentedClock
  private readonly audio: AudioSync
  private readonly loop: FrameLoop

  /** Test and telemetry counters (not part of the stable API). */
  readonly stats = {
    framesDrawn: 0, blackFrames: 0, heldFrames: 0, externalPauses: 0,
    sizeMismatches: 0, mediaElementsCreated: 0, seeks: 0, seekTimeouts: 0,
    /** rVFC handler time (upload + uniforms + draw), last 2048 frames, ms. */
    handlerMs: [] as number[],
  }

  constructor(opts: EngineOptions = {}) {
    this.opts = opts
    this.sink = opts.audioSink ?? new NullAudioSink()
    this.pclock = new PresentedClock(this.R)
    this.clock = { now: () => this.pclock.now() }
    this.store = new ProxyStore({
      baseUrl: opts.proxyBaseUrl ?? '/api/proxies',
      fetch: opts.fetch,
      maxBytes: opts.spanCacheBytes,
      onLoad: () => this.lane?.poke(),
      onError: (key, _msg, permanent) => {
        // degraded (§7) now; a transient failure is opened again later, and
        // a proxy that opens then is readable again (feed.openProxies)
        this.feed.markFailed(key)
        this.reclassify()
        if (!permanent) this.scheduleReopen()
      },
    })
    this.feed = new ProgramFeed(this.store)
    this.bake = new BakeSplice({
      feed: this.feed,
      renderHash: () => this.renderHash,
      hasProgram: () => !!this.pm,
      support: () => this.support,
      usable: () => !this.destroyed && this.mode !== 'server',
      reclassify: () => this.reclassify(false),
      refreshWant: () => this.refreshWant(),
      prefetch: () => this.prefetch(),
      emitStatus: () => this.emitStatus(),
    }, opts)
    this.feed.bakeStore = this.bake.store
    this.audio = new AudioSync(() => this.sink)
    this.loop = new FrameLoop(this.stats.handlerMs, (m) => this.onPresentedFrame(m), () => this.onTick(), TOPUP_MS)
    this.seeker = new ElementSeeker((k) => this.onSeekTimeout(k), () => {
      // its frame was trimmed while laneA finished: append it again first
      this.lane?.hold(false)
      this.showPaused()
    })
    this.external = new ExternalPauses({
      opts,
      isPlaying: () => this._playing,
      isDestroyed: () => this.destroyed,
      presentedK: () => this.presented,
      intent: () => this.intent,
      setIntent: (i) => { this.intent = i },
      stopPlayback: (why) => this.stopPlayback(why),
      play: () => this.play(),
      sink: () => this.sink,
      canDraw: () => !this.compositor?.lost,
      emitPause: (e) => { this.stats.externalPauses++; this.emit('pause-external', e) },
      emitStatus: () => this.emitStatus(),
      onHidden: () => { if (!this._playing) this.lane?.suspend(true) },
      onShown: () => { this.lane?.suspend(false); this.prefetch() },
    })
  }

  /** Sound-sync counters (starts, re-anchors, first-frame errors). */
  get audioStats() {
    return this.audio.stats
  }

  private get want(): Float64Array {
    return this.feed.want
  }

  // ------------------------------------------------------------- events

  on<E extends keyof EngineEvents>(event: E, cb: (e: EngineEvents[E]) => void): () => void {
    return this.events.on(event, cb)
  }

  private emit<E extends keyof EngineEvents>(event: E, payload: EngineEvents[E]): void {
    this.events.emit(event, payload)
  }

  get status(): EngineStatus {
    return {
      mode: this.mode, reason: this.reason, playing: this._playing, buffering: this.buffering,
      spinner: this.spinner.on || this.buffering, presentedK: this.presented, total: this.pm?.total ?? 0,
      ranges: this.support?.ranges ?? [],
    }
  }

  private emitStatus(): void {
    this.emit('status', this.status)
  }

  get presentedK(): number {
    return this.presented
  }

  /** The frame a paused seek is waiting to show (additive; = presentedK when settled). */
  get targetK(): number {
    return this.target
  }

  get playing(): boolean {
    return this._playing
  }

  get program(): ProgramMap | null {
    return this.pm
  }

  /** Internals for the WK test pages (not part of the stable API). */
  get internals() {
    return {
      lane: this.lane, compositor: this.compositor, store: this.store, video: this.video, canvas: this.canvas, want: this.feed.want,
      /** Redraw the paused frame from the current texture. */
      redraw: () => this.redrawPaused(),
      /** Paused, and the target frame is on the canvas. */
      settled: () => !this._playing && this.presented === this.target && !this.pendingDraw(),
      /** currentTime assignments vs 'seeking' events (equal when no seek is pending). */
      seekCounters: () => ({ issued: this.seeker.issued, seen: this.seeker.seen }),
    }
  }

  setAudioSink(sink: AudioSink): void {
    this.sink.stop(PAUSE_RAMP_MS)
    this.sink = sink
    if (this.pm && this.edl) this.prepareSink()
  }

  private fallback(reason: string): void {
    if (this.mode === 'server') return
    if (this._playing) this.stopPlayback('fallback')
    this.mode = 'server'
    this.reason = reason
    console.warn(`[preview engine] server mode: ${reason}`)
    this.emitStatus()
  }

  // --------------------------------------------------------------- DOM

  attach(host: HTMLElement): void {
    if (this.destroyed || this.host) return
    this.host = host
    const why = engineUnsupportedReason()
    if (why) {
      this.fallback(why)
      return
    }
    const { root, video, canvas, snap } = mountEngineDom(host)
    this.stats.mediaElementsCreated++
    this.root = root
    this.video = video
    this.canvas = canvas
    this.snapCanvas = snap
    try {
      this.compositor = new Compositor({
        canvas, snapshot: snap, mipmaps: this.opts.mipmaps,
        onContextLost: () => this.onContextLost(),
        onContextRestored: () => this.onContextRestored(),
      })
    } catch (e) {
      this.fallback(`no-webgl2: ${String((e as Error)?.message ?? e)}`)
      return
    }
    this.listenVideo(video)
    document.addEventListener('visibilitychange', this.external.onVisibility)
    if (typeof ResizeObserver !== 'undefined') {
      this.resizeObs = new ResizeObserver(() => this.layout())
      this.resizeObs.observe(host)
    }
    this.layout()
    this.makeLane()
    this.emitStatus()
  }

  private makeLane(): void {
    if (!this.video || this.mode === 'server') return
    this.lane?.destroy()
    this.elementFrame = -1
    this.compositor?.invalidateTexture()
    const lane = new LaneA({
      rate: this.R, media: browserMedia(this.video),
      events: {
        appended: (a, b) => this.onAppended(a, b),
        degraded: (r) => { this.reason = `degraded:${r}`; this.emitStatus() },
        fatal: (r) => this.fallback(r),
      },
    })
    this.lane = lane
    if (this.pm) lane.setProgram(this.feed.laneProgram())
    lane.setPlayhead(this.target, this._playing)
    void lane.open()
  }

  /** Canvas box: the EDL canvas aspect, contained in the host, centred. */
  private layout(): void {
    const { host, canvas, snapCanvas, compositor } = this
    if (!host || !canvas || !snapCanvas || !compositor) return
    const backing = layoutCanvas(host, [canvas, snapCanvas], this.canvasEdl, this.opts.canvasSize, this.opts.maxShortEdge ?? 1080)
    const changed = canvas.width !== Math.round(backing.w) || canvas.height !== Math.round(backing.h)
    compositor.resize(backing.w, backing.h)
    if (changed && !this._playing) this.redrawPaused()
  }

  // ------------------------------------------------------------ timeline

  setTimeline(edl: EdlLike, renderHash: string, sourceLookup: EngineSourceLookup): ProgramDiff {
    const empty: ProgramDiff = { dirtyFrames: [], dirtyParams: new Set() }
    if (this.destroyed) return empty
    this.edl = edl
    this.renderHash = renderHash
    const c = edl.canvas as { w?: number; h?: number; fps?: number } | undefined
    const size = { w: Math.max(2, c?.w ?? 1080), h: Math.max(2, c?.h ?? 1920) }
    const sizeChanged = size.w !== this.canvasEdl.w || size.h !== this.canvasEdl.h
    this.canvasEdl = size
    const R = rateOf(c?.fps ?? 30)
    if (ticksPerFrameExact(R) === null) {
      this.fallback('rate')
      return empty
    }
    const rateChanged = R.num !== this.R.num || R.den !== this.R.den
    if (rateChanged) {
      if (this._playing) this.stopPlayback('rate')
      this.R = R
      this.pclock.R = R
      this.pm = null
      this.feed.reset()
    }
    let diff: ProgramDiff
    try {
      diff = this.feed.build(edl, sourceLookup, R, size)
    } catch (e) {
      console.error('[preview engine] program map failed', e)
      this.reason = 'program-error'
      this.emitStatus()
      return empty
    }
    const pm = this.feed.pm!
    this.pm = pm
    // a bake is the picture of ONE render hash: a new program shows RAW
    // client frames in its BAKED ranges until its own bake lands
    if (this.feed.bakeKey !== renderHash) this.feed.setBake(null)
    this.feed.demote = this.bake.demotedFor(renderHash)
    this.openProxies()
    // a paused seek issued for the OLD program would upload the old picture
    // under the new content id: drop it; showPaused() below re-seeks
    this.dropSeek()
    this.target = Math.min(this.target, Math.max(0, pm.total - 1))
    if (!this._playing) this.presented = Math.min(this.presented, Math.max(0, pm.total - 1))
    this.reclassify(false)
    this.feed.applyBake(this.support)
    if (rateChanged && this.video) this.makeLane()
    else if (this.lane) this.lane.setProgram(this.feed.laneProgram())
    this.prepareSink()
    if (this._playing) {
      this.sink.reschedule(diff, samplesForFrames(this.presented + 6, this.R))
    }
    if (sizeChanged) this.layout()
    this.prefetch()
    if (!this._playing) this.showPaused()
    this.emitStatus()
    return diff
  }

  /** Open the program's proxies; one that failed transiently and opens now
   *  is readable again (reclassified, its frames re-requested). */
  private openProxies(): void {
    this.feed.openProxies(() => this.lane?.poke(), () => {
      if (this.destroyed || !this.pm) return
      this.reclassify(false)
      this.lane?.setProgram(this.feed.laneProgram())
      this.prefetch()
      if (!this._playing) this.showPaused()
      this.emitStatus()
    })
  }

  private scheduleReopen(): void {
    if (this.reopenTimer || this.destroyed) return
    this.reopenTimer = setTimeout(() => {
      this.reopenTimer = null
      if (!this.destroyed && this.pm) this.openProxies()
    }, PROXY_REOPEN_MS)
  }

  private prepareSink(): void {
    if (!this.pm || !this.edl) return
    this.sink.prepare(this.edl, this.feed.audioPlacements(), { R: this.R, totalFrames: this.pm.total, renderHash: this.renderHash, lookup: this.feed.lookup })
  }

  private reclassify(emit = true): void {
    const s = this.feed.classify()
    if (s) this.support = s
    if (emit) this.emitStatus()
  }

  /** Ask for every span the laneA window needs, nearest first. */
  private prefetch(): void {
    const lane = this.lane
    if (!lane) return
    if (!this._playing && typeof document !== 'undefined' && document.visibilityState === 'hidden') return
    const P = this._playing ? this.presented : this.target
    const back = Math.round((10 * this.R.num) / this.R.den)
    this.feed.prefetch(P, back, lane.lookAheadFrames)
    this.feed.prefetchBake(this.support, P, back, lane.lookAheadFrames)
  }

  // ------------------------------------------------------- bake splice

  /** The preview render of `renderHash` landed (§4.1 step 8): see engineBake.ts. */
  spliceBake(renderHash: string): boolean {
    return this.bake.splice(renderHash)
  }

  /** Output ranges the structural check (R14) found in disagreement for
   *  `renderHash`: they become BAKED (and take the bake when it lands). */
  setDemoted(renderHash: string, ranges: ReadonlyArray<readonly [number, number]>): void {
    this.bake.setDemoted(renderHash, ranges)
  }

  /** BAKED frames: how many show the bake, how many still show RAW frames. */
  get bakeState(): { hash: string | null; baked: number; waiting: number } {
    return this.bake.state()
  }

  /** Output frame k shows the server's bake frame (not a RAW client frame). */
  isBakedFrame(k: number): boolean {
    return this.feed.isBaked(k)
  }

  /** Re-point BAKED frames at landed bake samples and hand laneA the result. */
  private refreshWant(): void {
    this.feed.applyBake(this.support)
    this.lane?.setProgram(this.feed.laneProgram())
    this.prefetch()
    if (!this._playing) this.showPaused()
  }

  // ------------------------------------------------------------ drawing

  /** Draws frame k from the current texture (the caller checked it holds
   *  want[k]), or black for a gap. */
  private drawFrame(k: number, playing: boolean): boolean {
    const comp = this.compositor
    const pm = this.pm
    if (!comp || !pm || k < 0 || k >= pm.total || !drawProgramFrame(comp, pm, this.feed, this.canvasEdl, k)) return false
    if (pm.kind[k] === KIND_GAP) this.stats.blackFrames++
    this.stats.framesDrawn++
    this.presented = k
    if (!playing) {
      comp.snapshot(k)
      this.pclock.setPaused(k)
    }
    this.setSpinner(false)
    this.emit('frame', { k, t: (k * this.R.den) / this.R.num, playing, drawn: pm.kind[k] === KIND_GAP ? 'black' : 'picture' })
    return true
  }

  private uploadCurrent(k: number): boolean {
    return !!this.compositor && !!this.video && uploadProgramFrame(this.compositor, this.video, this.feed, k)
  }

  private setSpinner(on: boolean): void {
    this.spinner.set(on, () => this.presented !== this.target || this.pendingDraw())
  }

  /** The paused frame is not on the canvas yet. */
  private pendingDraw(): boolean {
    return !this._playing && (this.presented !== this.target || this.seeker.inFlight >= 0
      || (this.compositor?.textureContent !== this.want[this.target] && this.pm?.kind[this.target] !== KIND_GAP))
  }

  /** Shows `target` while paused: black for a gap; the current texture if
   *  it already holds the frame; else a (k + 0.5)/R seek, then upload. */
  private showPaused(): void {
    const pm = this.pm
    const lane = this.lane
    const video = this.video
    if (this._playing || !pm || !lane || !video || this.mode === 'server' || pm.total === 0) return
    const k = this.target
    if (pm.kind[k] === KIND_GAP) {
      this.drawFrame(k, false)
      return
    }
    if (!lane.isReady(k)) {
      if (this.seeker.inFlight >= 0) this.dropSeek()
      lane.hold(false)
      this.setSpinner(true)
      return
    }
    const comp = this.compositor
    if (comp && !comp.lost && comp.textureContent === this.want[k] && this.seeker.inFlight < 0) {
      this.drawFrame(k, false)
      return
    }
    if (this.seeker.inFlight === k) return
    this.setSpinner(true)
    this.seekElement(k)
  }

  /** The paused seek (§4.1 step 5): the playhead frames are appended; now
   *  appends HOLD (WebKit re-enqueues on every append, and texImage2D at
   *  'seeked' then returned an older or a black picture — measured 6/200),
   *  the element seeks to (k + 0.5)/R, the frame is uploaded at 'seeked',
   *  and only then does the rest of the window fill. */
  private seekElement(k: number): void {
    const video = this.video
    const lane = this.lane
    if (!video || !lane) return
    this.stats.seeks++
    this.seeker.start(k, video, lane, () => this.lane === lane && !this._playing)
  }

  private onSeekTimeout(k: number): void {
    // a seek WebKit never completed (a hole under it): try again
    void k
    this.stats.seekTimeouts++
    this.lane?.hold(false)
    this.showPaused()
  }

  /** Abandon a paused seek that no longer matters (a new target). */
  private dropSeek(): void {
    if (this.seeker.inFlight < 0) return
    this.seeker.finish()
    this.lane?.hold(false)
  }

  private redrawPaused(): void {
    if (this._playing || !this.pm) return
    const k = this.presented
    const comp = this.compositor
    if (!comp || comp.lost) return
    if (this.pm.kind[k] === KIND_GAP || comp.textureContent === this.want[k]) this.drawFrame(k, false)
  }

  private onSeeked = (): void => {
    const video = this.video
    const lane = this.lane
    if (!video || !lane || this.seeker.inFlight < 0) return
    // A 'seeked' of an EARLIER seek can be queued before a newer one starts
    // (seeked₁, seeking₂, seeked₂): the element then still shows the old
    // frame while currentTime already names the new one. Only the seek that
    // is really over counts.
    if (!this.seeker.isLatest(video)) return
    const k = this.seeker.inFlight
    this.seeker.finish()
    if (lane.frameAt(video.currentTime - (0.5 * this.R.den) / this.R.num) !== k) {
      this.elementFrame = -1
      lane.hold(false)
      this.showPaused()
      return
    }
    this.elementFrame = k
    if (this._playing) {
      lane.hold(false)
      this.audio.requestRestart()
      return
    }
    if (k !== this.target || !lane.isReady(k)) {
      lane.hold(false)
      this.showPaused()
      return
    }
    // Upload BEFORE the hold is released: hold(false) pumps laneA, and an
    // append issued synchronously in there makes WebKit re-enqueue, so a
    // texImage2D after it read a neighbouring frame (measured in WK: a
    // paused seek to k = 587 drew source frame 64 instead of 62 when the
    // stale frame 590 was appended inside this handler).
    const uploaded = this.uploadCurrent(k)
    lane.hold(false)
    if (uploaded) this.drawFrame(k, false)
  }

  private onAppended(a: number, b: number): void {
    // An overwrite of the frame the element shows: WebKit hands back black
    // for it until the re-seek completes (the upload waits for 'seeked').
    if (this.elementFrame >= a && this.elementFrame < b) this.elementFrame = -1
    if (!this._playing && this.target >= a && this.target < b) {
      if (this.compositor?.textureContent !== this.want[this.target] || this.presented !== this.target) this.showPaused()
    }
  }

  // ------------------------------------------------------------ transport

  play(): void {
    const video = this.video
    const lane = this.lane
    const pm = this.pm
    this.intent = 'play'
    if (this._playing || !video || !lane || !pm || pm.total === 0 || this.mode === 'server') return
    this.external.reset()
    if (this.target >= pm.total - 1) this.target = 0
    const k = this.target
    // the element must stand where the canvas does
    lane.hold(false)
    if (this.elementFrame !== k || this.seeker.inFlight >= 0) {
      this.seeker.finish()
      this.seeker.assign(video, lane.seekTime(k))
      this.elementFrame = k
    }
    this._playing = true
    this.buffering = false
    this.playingSeek.clear()
    lane.setPlayhead(k, true)
    const playCalledAt = performance.now()
    const p = video.play()
    if (p && typeof p.catch === 'function') {
      p.catch((e: unknown) => {
        if (this._playing) {
          console.warn('[preview engine] play() refused', e)
          this.stopPlayback('refused')
          this.intent = 'pause'
          this.emitStatus()
        }
      })
    }
    this.audio.play(playCalledAt, lane.seekTime(k))
    this.loop.start(video)
    this.emitStatus()
  }

  pause(): void {
    this.intent = 'pause'
    this.external.reset()
    if (this._playing) this.stopPlayback('user')
  }

  private stopPlayback(why: string): void {
    void why
    const video = this.video
    this._playing = false
    this.buffering = false
    this.playingSeek.clear()
    this.loop.stop()
    this.audio.stop(PAUSE_RAMP_MS)
    if (video && !video.paused) {
      this.external.expectOwnPause()
      video.pause()
    }
    this.target = this.presented
    this.pclock.setPaused(this.presented)
    this.lane?.setPlayhead(this.presented, false)
    // park the element on the frame the canvas shows; the texture already
    // holds it, so a redraw needs no upload
    this.elementFrame = -1
    if (this.video && this.lane) {
      this.seekElement(this.presented)
    }
    this.redrawPaused()
    this.emitStatus()
  }

  seek(k: number): void {
    const pm = this.pm
    if (!pm || pm.total === 0) return
    k = Math.max(0, Math.min(pm.total - 1, Math.round(k)))
    this.target = k
    if (this._playing) {
      // stop the sound where the picture is; restart both at the new place,
      // on a frame of the NEW position (see PLAYING_SEEK_WAIT_MS)
      this.audio.stop(PAUSE_RAMP_MS)
      this.audio.requestRestart()
      this.playingSeek.seeked(k)
      this.lane?.setPlayhead(k, true)
      if (this.video && this.lane) this.seeker.assign(this.video, this.lane.seekTime(k))
      this.prefetch()
      return
    }
    if (this.seeker.inFlight >= 0 && this.seeker.inFlight !== k) this.dropSeek()
    this.lane?.setPlayhead(k, false)
    this.prefetch()
    this.showPaused()
    if (this.presented !== k) this.setSpinner(true)
  }

  private onPresentedFrame(meta: PresentedMeta): void {
    const pm = this.pm
    const lane = this.lane
    if (!pm || !lane) return
    const k = Math.round(meta.mediaTime * this.R.num / this.R.den)
    if (k >= pm.total) {
      this.stopPlayback('end')
      return
    }
    if (this.buffering) {
      this.buffering = false
      this.audio.requestRestart()
      this.emit('buffering', { buffering: false, k })
      this.emitStatus()
    }
    let drawn = false
    if (pm.kind[k] === KIND_GAP) {
      drawn = this.drawFrame(k, true)
    } else if (lane.isReady(k)) {
      const h = this.feed.handleAt(k)
      if (h && (meta.width !== h.index.w || meta.height !== h.index.h)) this.stats.sizeMismatches++
      if (this.uploadCurrent(k)) drawn = this.drawFrame(k, true)
    } else {
      this.stats.heldFrames++
    }
    if (drawn) this.pclock.onPresented(k, meta.expectedDisplayTime)
    // the sound (re)starts on a frame of the position the user asked for
    if (this.playingSeek.admits(k, this.R.num / this.R.den)) this.audio.onFrame(meta)
    if (k >= pm.total - 1) this.stopPlayback('end')
  }

  /** Every TOPUP_MS while playing: the window follows the presented frame;
   *  the watchdog catches a pause WebKit made without telling us. */
  private onTick(): void {
    if (!this._playing) return
    this.lane?.setPlayhead(this.presented, true)
    this.prefetch()
    if (this.video) this.external.watch(this.video)
  }

  private listenVideo(video: HTMLVideoElement): void {
    video.addEventListener('seeked', this.onSeeked)
    video.addEventListener('seeking', this.seeker.onSeeking)
    this.external.listen(video)
    video.addEventListener('waiting', () => {
      if (!this._playing) return
      const pm = this.pm
      if (pm && this.presented >= pm.total - 2) {
        this.stopPlayback('end')
        return
      }
      if (this.buffering) return
      this.buffering = true
      this.audio.stop(PAUSE_RAMP_MS)
      this.emit('buffering', { buffering: true, k: this.presented })
      this.emitStatus()
    })
  }

  /** A pause someone else made (the AudioContext interrupted by the system):
   *  picture and sound stop together at the presented k; with the page
   *  hidden the user's intent to play is kept for the return. */
  pauseExternal(cause: ExternalCause = document.visibilityState === 'hidden' ? 'hidden' : 'element'): void {
    this.external.pause(cause)
  }

  // ------------------------------------------------------ context loss

  /** §7: WebKit dropped the WebGL context. The 2D snapshot stands in for the
   *  canvas — but only while it holds the frame on screen: playing, it holds
   *  the LAST PAUSE's frame, so picture and sound stop together at
   *  presentedK (resumed on restore) and the snapshot goes black with the
   *  spinner up rather than show another frame. Not restored in 2 s: the
   *  server preview. */
  private onContextLost(): void {
    if (this.contextTimer) clearTimeout(this.contextTimer)
    this.contextTimer = setTimeout(() => {
      this.contextTimer = null
      if (this.compositor?.lost) this.fallback('webgl-lost')
    }, CONTEXT_RESTORE_MS)
    if (this._playing) this.external.pause('context')
    const comp = this.compositor
    if (comp && comp.snapshotK !== this.presented) {
      comp.clearSnapshot()
      this.setSpinner(true)
    }
    this.emitStatus()
  }

  private onContextRestored(): void {
    if (this.contextTimer) clearTimeout(this.contextTimer)
    this.contextTimer = null
    this.compositor?.showSnapshot(false)
    // the texture is gone: re-seek (paused) or wait for the next rVFC
    this.elementFrame = -1
    if (!this._playing && !this.external.onContextRestored()) this.showPaused()
    this.emitStatus()
  }

  // ------------------------------------------------------------- teardown

  destroy(): void {
    if (this.destroyed) return
    this.destroyed = true
    this._playing = false
    this.loop.stop()
    this.external.cancelResume()
    this.seeker.clearTimer()
    this.spinner.cancel()
    for (const t of [this.contextTimer, this.reopenTimer]) if (t) clearTimeout(t)
    this.sink.stop(0)
    this.sink.dispose?.()
    document.removeEventListener('visibilitychange', this.external.onVisibility)
    this.resizeObs?.disconnect()
    this.lane?.destroy()
    this.compositor?.destroy()
    this.store.dispose()
    this.bake.dispose()
    this.root?.remove()
    this.lane = null
    this.compositor = null
    this.video = null
    this.events.clear()
  }
}
