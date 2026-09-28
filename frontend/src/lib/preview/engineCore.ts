// The instant-preview engine itself (INSTANT_PREVIEW_SPEC §3, §4.1, §4.3,
// §7, §10): DOM (one muted laneA <video> under an opaque WebGL2 canvas),
// transport on the presented clock, the paused-seek state machine, and
// context loss. The public contract is in engine.ts; the program → laneA
// feed in engineFeed.ts; currentTime and WebKit's seek events in
// engineSeek.ts; pauses WebKit makes on its own in engineExternal.ts; the
// bake splice in engineBake.ts; proxy I/O and hidden-page suspension in
// engineSources.ts; the degraded <video> tier in engineDegraded.ts; context
// loss and decode errors in engineRecovery.ts; the events and the status a
// caller reads in engineBase.ts.

import type {
  AudioSink, EngineClock, EngineSourceLookup, PreviewEngine, ProgramDiff,
} from './engine'
import type { EdlLike } from './timeline/framePlan'
import { KIND_GAP } from './timeline/programMap'
import { rateOf, samplesForFrames, ticksPerFrameExact, type Rational } from './timeline/timebase'
import { LaneA, browserMedia } from './media/laneA'
import type { ProxyStore } from './media/proxyIndex'
import { Compositor } from './render/compositor'
import type { Size } from './render/geometry'
import { PresentedClock } from './clock/presentedClock'
import { AudioSync } from './clock/audioSync'
import { ProgramFeed } from './engineFeed'
import { layoutCanvas, mountEngineDom } from './engineDom'
import { NullAudioSink, engineUnsupportedReason, type EngineOptions } from './engineOptions'
import { ElementSeeker, PlayingSeekGate, RunStartGate, SoughtFrame } from './engineSeek'
import { drawProgramFrame, preloadCanvasBackgrounds, uploadProgramFrame } from './engineDraw'
import { canvasBgDraw } from './render/canvasBg'
import { canvasBgImageState } from './render/canvasBgImages'
import { ExternalPauses, type ExternalCause } from './engineExternal'
import { BakeSplice } from './engineBake'
import { FrameLoop, StallWatch, type PresentedMeta } from './engineLoop'
import { DegradedTier } from './engineDegraded'
import { EngineRecovery } from './engineRecovery'
import { EngineSources } from './engineSources'
import { EngineBase } from './engineBase'
import { editLeadFrames } from './clock/editLead'

export { NullAudioSink, engineUnsupportedReason, type EngineOptions }

const TOPUP_MS = 250
const PAUSE_RAMP_MS = 5

export function createPreviewEngine(opts: EngineOptions = {}): ClientPreviewEngine {
  return new ClientPreviewEngine(opts)
}

export class ClientPreviewEngine extends EngineBase implements PreviewEngine {
  readonly clock: EngineClock
  private readonly opts: EngineOptions
  private readonly store: ProxyStore
  private readonly sources: EngineSources
  private sink: AudioSink

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
  private canvasEdl: Size = { w: 1080, h: 1920 }
  private readonly feed: ProgramFeed
  private readonly bake: BakeSplice

  // transport and display
  private intent: 'play' | 'pause' = 'pause'
  /** Frame the element was last seeked to (paused), −1 unknown. An append
   *  over it resets this: WebKit hands back black for an overwritten frame
   *  until a re-seek, so the only uploads are at a completed 'seeked' and in
   *  rVFC while playing (never between an overwrite and its re-seek). */
  private elementFrame = -1
  private readonly seeker: ElementSeeker
  private readonly external: ExternalPauses
  private readonly playingSeek = new PlayingSeekGate()
  private readonly runStart = new RunStartGate()
  private readonly stall = new StallWatch()
  private readonly sought = new SoughtFrame()
  private readonly degraded: DegradedTier
  private readonly recovery: EngineRecovery
  private destroyed = false

  // clock / audio
  private readonly pclock: PresentedClock
  private readonly audio: AudioSync
  private readonly loop: FrameLoop

  /** Test and telemetry counters (not part of the stable API). */
  readonly stats = {
    framesDrawn: 0, blackFrames: 0, heldFrames: 0, externalPauses: 0,
    sizeMismatches: 0, mediaElementsCreated: 0, seeks: 0, seekTimeouts: 0, stallRestarts: 0,
    /** Paused 'seeked' before the element presented the frame (waited for). */
    seekedEarly: 0,
    /** rVFC handler time (upload + uniforms + draw), last 2048 frames, ms. */
    handlerMs: [] as number[],
  }

  constructor(opts: EngineOptions = {}) {
    super()
    this.opts = opts
    this.sink = opts.audioSink ?? new NullAudioSink()
    // the master limiter's APPROX ranges can land after prepare (gate RX)
    if ('onLimitingChange' in this.sink) this.sink.onLimitingChange = () => { if (!this.destroyed) this.reclassify() }
    this.pclock = new PresentedClock(this.R)
    this.clock = { now: () => this.pclock.now() }
    this.sources = new EngineSources(opts, {
      lane: () => this.lane,
      hasProgram: () => !!this.pm,
      destroyed: () => this.destroyed,
      playhead: () => (this._playing && !this.runStart.pending ? this.presented : this.target),
      rate: () => this.R,
      support: () => this.support,
      bakeStore: () => this.bake.store,
      // a proxy that fails AFTER its paused frame was asked for: the
      // degraded tier shows that frame now, not at the next seek (RD3)
      failed: () => { this.reclassify(); if (!this._playing) this.showPaused() },
      recovered: () => {
        this.reclassify(false)
        this.lane?.setProgram(this.feed.laneProgram())
        this.sources.prefetch()
        if (!this._playing) this.showPaused()
        this.emitStatus()
      },
      shown: () => { if (!this._playing) this.showPaused() },
    })
    this.store = this.sources.store
    this.feed = new ProgramFeed(this.store)
    this.sources.feed = this.feed
    this.bake = new BakeSplice({
      feed: this.feed,
      renderHash: () => this.renderHash,
      hasProgram: () => !!this.pm,
      support: () => this.support,
      usable: () => !this.destroyed && this.mode !== 'server',
      reclassify: () => this.reclassify(false),
      refreshWant: () => this.refreshWant(),
      prefetch: () => this.sources.prefetch(),
      emitStatus: () => this.emitStatus(),
      hiddenNow: () => this.sources.hiddenNow(),
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
      onHidden: () => this.sources.suspend(true),
      onShown: () => this.sources.suspend(false),
      elementMoved: () => { this.elementFrame = -1 },
    })
    this.degraded = new DegradedTier({
      root: () => this.root,
      compositor: () => this.compositor,
      wanted: (k, id) => !this._playing && this.target === k && this.want[k] === id && !this.destroyed,
      draw: (k) => this.drawFrame(k, false),
      created: () => { this.stats.mediaElementsCreated++ },
      failed: (k, why) => { console.warn(`[preview engine] degraded frame ${k}: ${why}`) },
    }, this.feed)
    this.recovery = new EngineRecovery({
      isPlaying: () => this._playing,
      presentedK: () => this.presented,
      live: () => !this.destroyed && this.mode === 'client' && !!this.lane,
      compositorLost: () => !!this.compositor?.lost,
      snapshotK: () => this.compositor?.snapshotK ?? -1,
      clearSnapshot: () => this.compositor?.clearSnapshot(),
      spinner: (on) => this.setSpinner(on),
      pauseExternal: (cause) => this.external.pause(cause),
      resumeAfterRestore: () => this.external.onContextRestored(),
      forgetElementFrame: () => { this.elementFrame = -1 },
      showSnapshot: (on) => this.compositor?.showSnapshot(on),
      showPaused: () => this.showPaused(),
      rebuildLane: () => { this.seeker.finish(); this.makeLane() },
      stopPlayback: (why) => this.stopPlayback(why),
      play: () => this.play(),
      fallback: (r) => this.fallback(r),
      emitStatus: () => this.emitStatus(),
    })
  }

  /** Sound-sync counters (starts, re-anchors, first-frame errors). */
  get audioStats() {
    return this.audio.stats
  }

  private get want(): Float64Array {
    return this.feed.want
  }

  /** Internals for the WK test pages (not part of the stable API). */
  get internals() {
    return {
      lane: this.lane, compositor: this.compositor, store: this.store, video: this.video, canvas: this.canvas, want: this.feed.want,
      degraded: this.degraded, recovery: this.recovery,
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
        canvas, snapshot: snap, mipmaps: this.opts.mipmaps, canvasBgBaseUrl: this.opts.canvasBgBaseUrl,
        onAssetReady: () => { if (!this._playing) this.redrawPaused() },
        onContextLost: this.recovery.onContextLost,
        onContextRestored: this.recovery.onContextRestored,
      })
    } catch (e) {
      this.fallback(`no-webgl2: ${String((e as Error)?.message ?? e)}`)
      return
    }
    this.listenVideo(video)
    document.addEventListener('visibilitychange', this.external.onVisibility)
    this.sources.resumeIfVisible()
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
      // §3.5: hidden before WebKit dispatched 'visibilitychange' counts
      hiddenNow: () => this.sources.hiddenNow(),
      events: {
        appended: (a, b) => this.onAppended(a, b),
        degraded: (r) => { this.reason = `degraded:${r}`; this.emitStatus() },
        fatal: (r) => this.fallback(r),
      },
    })
    this.lane = lane
    if (this.sources.isSuspended) lane.suspend(true)
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
    this.sources.open()
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
      this.sink.reschedule(diff, samplesForFrames(this.presented + editLeadFrames(this.R), this.R))
    }
    if (sizeChanged) this.layout()
    this.sources.prefetch()
    // review RE: an image background not decoded yet (or failed, retried)
    // keeps its frames PENDING; its arrival reclassifies and redraws
    const bgBase = this.opts.canvasBgBaseUrl
    this.feed.canvasImagePending = bgBase ? (clip) => {
      const d = canvasBgDraw(clip, size, size, bgBase)
      return d?.mode === 'image' && canvasBgImageState(d.url) !== 'ready'
    } : null
    preloadCanvasBackgrounds(pm.clips, size, bgBase, () => {
      this.reclassify()
      if (!this._playing) this.redrawPaused()
    })
    this.reclassify(false)
    if (!this._playing) this.showPaused()
    this.emitStatus()
    return diff
  }

  private prepareSink(): void {
    if (!this.pm || !this.edl) return
    this.sink.prepare(this.edl, this.feed.audioPlacements(), { R: this.R, totalFrames: this.pm.total, renderHash: this.renderHash, lookup: this.feed.lookup })
  }

  private reclassify(emit = true): void {
    const s = this.feed.classify(this.sink.limitingFrames?.(), this.sink.loudnessCurrent?.())
    if (s) this.support = s
    if (emit) this.emitStatus()
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
    this.sources.prefetch()
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
    return !this._playing && (this.presented !== this.target || this.seeker.inFlight >= 0 || this.degraded.pending >= 0 || this.sought.k >= 0
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
      this.degraded.cancel()
      this.drawFrame(k, false)
      return
    }
    const dg = this.degraded.request(k)
    if (dg) {
      // proxy failed (§7): the master's paused frame, not laneA's
      if (this.seeker.inFlight >= 0) this.dropSeek()
      if (this.degraded.pending !== k) this.setSpinner(true)
      this.degraded.show(k, dg)
      return
    }
    this.degraded.cancel()
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
    if (this.seeker.inFlight === k || this.sought.k === k) return
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
    this.sought.cancel()
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
    if (this.sought.k >= 0) { this.sought.cancel(); this.lane?.hold(false) }
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
    this.sought.complete(k, video, lane, {
      stillWanted: (j) => this.lane === lane && !this._playing && this.target === j && lane.isReady(j),
      upload: (j) => this.uploadCurrent(j),
      draw: (j) => this.drawFrame(j, false),
      retry: () => { this.elementFrame = -1; this.showPaused() },
      early: () => { this.stats.seekedEarly++ },
    })
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
    this.degraded.cancel()
    this.sought.cancel()
    if (this.target >= pm.total - 1) this.target = 0
    const k = this.target
    // the element must stand where the canvas does
    lane.hold(false)
    const elementAt = this.seeker.inFlight >= 0 ? -1 : this.elementFrame
    if (elementAt !== k) {
      this.seeker.finish()
      this.seeker.assign(video, lane.seekTime(k))
      this.elementFrame = k
    }
    this._playing = true
    this.buffering = false
    this.playingSeek.clear()
    // gated on where the ELEMENT stood, not the canvas: after WebKit moved
    // the parked element (elementMoved) the canvas still shows k, and the
    // element's first presentation came from 1.3 s on and was drawn (C1)
    this.runStart.begin(k, elementAt)
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
    this.stall.arm(playCalledAt)
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
      this.runStart.begin(k, this.presented)
      this.stall.arm(performance.now())
      this.lane?.setPlayhead(k, true)
      if (this.video && this.lane) this.seeker.assign(this.video, this.lane.seekTime(k))
      this.sources.prefetch()
      return
    }
    if (this.seeker.inFlight >= 0 && this.seeker.inFlight !== k) this.dropSeek()
    this.lane?.setPlayhead(k, false)
    this.sources.prefetch()
    this.showPaused()
    if (this.presented !== k) this.setSpinner(true)
  }

  private onPresentedFrame(meta: PresentedMeta): void {
    const pm = this.pm
    const lane = this.lane
    if (!pm || !lane) return
    const k = Math.round(meta.mediaTime * this.R.num / this.R.den)
    if (this.runStart.stale(k, this.R.num / this.R.den)) return
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
    // (a run not yet presented at its start: the window belongs THERE)
    this.lane?.setPlayhead(this.runStart.pending ? this.target : this.presented, true)
    this.sources.prefetch()
    if (this.video) this.external.watch(this.video)
    if (this.stall.check(this.presented, this.buffering, performance.now(), this.video?.currentTime)) this.restartStalled()
  }

  /** The stuck-play watchdog (review RD3): playing, no new frame for
   *  STALL_MS and no 'waiting' — say what the run looked like, then start it
   *  again from the presented frame (pause + play), as the user would. */
  private restartStalled(): void {
    const v = this.video
    console.warn('[preview engine] playback stalled; restarting the run', {
      presented: this.presented, target: this.target, runStartPending: this.runStart.pending,
      element: v ? { t: v.currentTime, paused: v.paused, readyState: v.readyState, seeking: v.seeking } : null,
      buffered: this.lane?.buffered, ready: this.lane?.isReady(this.presented + 1),
    })
    this.stats.stallRestarts++
    this.stopPlayback('stall')
    this.play()
  }

  private listenVideo(video: HTMLVideoElement): void {
    video.addEventListener('seeked', this.onSeeked)
    video.addEventListener('seeking', this.seeker.onSeeking)
    // a MediaError leaves a MediaSource-backed element dead (§7 decode errors)
    video.addEventListener('error', () => {
      const code = video.error?.code ?? 0
      if (code !== 1 && video.src) this.recovery.onMediaError(code)
    })
    this.external.listen(video)
    video.addEventListener('waiting', () => {
      if (!this._playing) return
      const pm = this.pm
      if (pm && this.presented >= pm.total - 2 && !this.runStart.pending) {
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

  // ------------------------------------------------------------- teardown

  destroy(): void {
    if (this.destroyed) return
    this.destroyed = true
    this._playing = false
    this.loop.stop()
    this.external.cancelResume()
    this.seeker.clearTimer()
    this.sought.cancel()
    this.spinner.cancel()
    this.recovery.destroy()
    this.degraded.destroy()
    this.sink.stop(0)
    this.sink.dispose?.()
    document.removeEventListener('visibilitychange', this.external.onVisibility)
    this.resizeObs?.disconnect()
    this.lane?.destroy()
    this.compositor?.destroy()
    this.sources.destroy()
    this.bake.dispose()
    this.root?.remove()
    this.lane = null
    this.compositor = null
    this.video = null
    this.events.clear()
  }
}
