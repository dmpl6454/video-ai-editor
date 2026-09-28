// laneA: the engine's picture lane (INSTANT_PREVIEW_SPEC §3.2, §4.1, §4.3,
// §6 R7, §10). One MediaSource + one `segments` SourceBuffer on one muted
// <video>, fed by media/fmp4Writer on the project frame grid: output frame k
// is one all-intra proxy sample with tfdt = k·T, so a cut, a duplicate, a
// reverse or a speed change is only WHICH sample sits at which k.
//
// Invariants (each one pinned by laneA.test.ts against a fake SourceBuffer):
//
// * CONTIGUOUS WINDOW. The buffered range is one interval [bufA, bufB)
//   around the playhead (−10 s … +30 s, clamped to the program). A hole in
//   the buffered ranges stalls WebKit, so appends only ever overwrite inside
//   the interval or extend it at an end, and timeline GAPS are written as
//   filler samples (the writer's last sample; the compositor draws black
//   there from the program map, never from the filler's pixels).
// * PER-FRAME CONTENT. `have[k]` is the content id appended at k; frame k is
//   READY when it equals what the program wants (for a gap: anything). An
//   edit is just a new `want` — the frames that differ are re-appended,
//   nearest the playhead first. A stale frame whose new bytes are not here
//   yet is cut out of the interval (the interval is truncated there), so
//   playback stalls (buffering) instead of showing the old picture.
// * INIT SWITCHING per init class (the avcC hex, index.json `init_key`) —
//   done by the writer, which remembers the class the SourceBuffer holds.
// * ORDER. Paused: the 1…5 frames at the playhead, then stale frames by
//   distance, then outward in 1 s batches. Playing: from presentedK + 150 ms
//   (nearer frames race the decoder), then outward.
// * SHRINK with remove() and `duration = total/R`; endOfStream() is never
//   called, so the source stays editable.
// * QUOTA. QuotaExceededError: remove behind the playhead down to −2 s,
//   halve the look-ahead, retry once, then report `degraded`.
// * SEEKS while paused go to (k + 0.5)/R (exact boundaries fail to repaint).

import { Fmp4Writer, type FrameEntry, type FrameSample, type Segment } from './fmp4Writer'
import type { Rational } from '../timeline/timebase'
import { EDIT_LEAD_S } from '../clock/editLead'

/** `want[k]` of a timeline gap. */
export const GAP = -1
/** `have[k]`: a gap filler was appended. */
export const FILLER = -2
/** `have[k]`: nothing appended. */
export const NONE = -3

/** What the engine wants on screen, frame by frame. */
export interface LaneProgram {
  readonly total: number
  /** Content id per output frame (≥ 0: a source frame; GAP). */
  readonly want: ArrayLike<number>
  /** The bytes of content `id`, or null while they are not loaded. */
  sample(id: number): FrameSample | null
  /** Load content `id` (lower priority first; `urgent`: the frame on
   *  screen is waiting for it). */
  request(id: number, priority: number, urgent: boolean): void
}

// ---- the DOM surface laneA touches (a fake in laneA.test.ts) -------------

export interface TimeRangesLike { readonly length: number; start(i: number): number; end(i: number): number }

export interface SourceBufferLike extends EventTarget {
  mode: string
  readonly updating: boolean
  readonly buffered: TimeRangesLike
  appendBuffer(data: Uint8Array): void
  remove(start: number, end: number): void
  changeType?(type: string): void
  /** Resets the segment parser: the next media segment starts a new coded
   *  frame group (MSE resetParserState). */
  abort?(): void
}

export interface MediaSourceLike extends EventTarget {
  readonly readyState: string
  duration: number
  addSourceBuffer(type: string): SourceBufferLike
}

export interface VideoLike extends EventTarget {
  src: string
  currentTime: number
  readonly paused: boolean
  muted: boolean
  play(): Promise<void>
  pause(): void
  removeAttribute(name: string): void
  load(): void
}

export interface LaneMedia {
  video: VideoLike
  createMediaSource(): MediaSourceLike
  createObjectURL(ms: MediaSourceLike): string
  revokeObjectURL(url: string): void
}

/** The browser implementation: MediaSource (or ManagedMediaSource). */
export function browserMedia(video: HTMLVideoElement): LaneMedia {
  const MS = (window.MediaSource ?? (window as unknown as { ManagedMediaSource?: typeof MediaSource }).ManagedMediaSource)
  if (!MS) throw new Error('no MediaSource')
  return {
    video: video as unknown as VideoLike,
    createMediaSource: () => new MS() as unknown as MediaSourceLike,
    createObjectURL: (ms) => URL.createObjectURL(ms as unknown as MediaSource),
    revokeObjectURL: (url) => URL.revokeObjectURL(url),
  }
}

export interface LaneEvents {
  /** Frames [a, b) now hold their wanted content. */
  appended?(a: number, b: number): void
  removed?(a: number, b: number): void
  /** Quota twice in a row: the window could not be kept. */
  degraded?(reason: string): void
  /** ≥ 3 SourceBuffer errors in 60 s: the engine should fall back (§7). */
  fatal?(reason: string): void
}

export interface LaneOptions {
  rate: Rational
  media: LaneMedia
  events?: LaneEvents
  backSeconds?: number
  aheadSeconds?: number
  /** Frames at the playhead appended first while paused (§3.2: 1 to 5). */
  pausedNearFrames?: number
  /** While playing, stale frames nearer than this are left (≈ 150 ms). */
  playingLeadSeconds?: number
  now?: () => number
  /** The page is hidden NOW, its 'visibilitychange' dispatched or not
   *  (WebKit flips visibilityState a task before it dispatches the event,
   *  §3.5). Asked before every append and remove: true stops the pump like
   *  suspend(true), and the owner must suspend now and resume later (the
   *  lane itself latches nothing, so the owner's resume always pokes). */
  hiddenNow?: () => boolean
}

export type LaneAction =
  | { kind: 'remove'; a: number; b: number; why: string }
  | { kind: 'append'; a: number; b: number; why: string }
  | { kind: 'duration'; frames: number }

const ERROR_WINDOW_MS = 60_000
const FATAL_ERRORS = 3

/** The rejection of `waitUpdate` on a SourceBuffer 'error' event. The
 *  SourceBuffer's own permanent 'error' listener already counted that event
 *  (§7: 3 errors in 60 s), so the awaiting caller must not count it again. */
class SourceBufferEventError extends Error {
  constructor() {
    super('SourceBuffer error')
    this.name = 'SourceBufferEventError'
  }
}

function isQuota(e: unknown): boolean {
  return typeof e === 'object' && e !== null && (e as { name?: string }).name === 'QuotaExceededError'
}

export class LaneA {
  readonly R: Rational
  readonly media: LaneMedia
  readonly writer: Fmp4Writer
  readonly stats = {
    appends: 0, appendedFrames: 0, fillerFrames: 0, inits: 0, removes: 0, quotaHits: 0, errors: 0,
    truncations: 0, resets: 0,
    /** Appends skipped because no init segment had named a codec yet. */
    noCodec: 0,
    /** Media segments that did not continue the previous one (a new coded
     *  frame group was started for them). */
    jumps: 0,
    /** Frames the SourceBuffer lost next to an append (re-appended). */
    lost: 0,
  }
  /** End frame of the last media segment appended (-1: none since a reset). */
  private lastEnd = -1
  private readonly events: LaneEvents
  private back: number
  private readonly backMax: number
  private ahead: number
  private readonly aheadMax: number
  private readonly pausedNear: number
  private readonly playingLead: number
  private readonly now: () => number
  private readonly hiddenNow: () => boolean

  private ms: MediaSourceLike | null = null
  private sb: SourceBufferLike | null = null
  private url: string | null = null
  private codec: string | null = null
  private opened: Promise<void> | null = null

  private program: LaneProgram = { total: 0, want: [], sample: () => null, request: () => undefined }
  private have = new Float64Array(0)
  /** The buffered interval [bufA, bufB) (bufA === bufB: empty). */
  bufA = 0
  bufB = 0
  private playhead = 0
  private playing = false
  private pumping = false
  private again = false
  private held = false
  /** The page is hidden (§3.5): no appends or removes until it is back. */
  private suspended = false
  private idleWaiters: Array<() => void> = []
  private quotaRetry = false
  private stalledUntil = 0
  private errorTimes: number[] = []
  private destroyed = false
  /** The program length the MediaSource duration was last set to. */
  private durationFrames = -1
  private stallTimer: ReturnType<typeof setTimeout> | null = null

  constructor(opts: LaneOptions) {
    this.R = opts.rate
    this.media = opts.media
    this.events = opts.events ?? {}
    this.writer = new Fmp4Writer({ rate: opts.rate })
    const fps = opts.rate.num / opts.rate.den
    this.backMax = Math.round((opts.backSeconds ?? 10) * fps)
    this.back = this.backMax
    this.aheadMax = Math.round((opts.aheadSeconds ?? 30) * fps)
    this.ahead = this.aheadMax
    this.pausedNear = opts.pausedNearFrames ?? 5
    this.playingLead = Math.ceil((opts.playingLeadSeconds ?? EDIT_LEAD_S) * fps - 1e-9)
    this.now = opts.now ?? (() => performance.now())
    this.hiddenNow = opts.hiddenNow ?? (() => false)
  }

  get video(): VideoLike {
    return this.media.video
  }

  get sourceBuffer(): SourceBufferLike | null {
    return this.sb
  }

  get mediaSource(): MediaSourceLike | null {
    return this.ms
  }

  get lookAheadFrames(): number {
    return this.ahead
  }

  /** Attaches a fresh MediaSource to the video and waits for `sourceopen`. */
  open(): Promise<void> {
    if (!this.opened) {
      this.opened = new Promise<void>((resolve) => {
        const ms = this.media.createMediaSource()
        this.ms = ms
        ms.addEventListener('sourceopen', () => resolve(), { once: true })
        this.url = this.media.createObjectURL(ms)
        this.media.video.src = this.url
      }).then(() => { void this.pump() })
    }
    return this.opened
  }

  // --------------------------------------------------------------- state

  /** Seconds a paused seek to k targets (R7). */
  seekTime(k: number): number {
    return ((k + 0.5) * this.R.den) / this.R.num
  }

  /** The output frame the element shows at media time t. */
  frameAt(t: number): number {
    return Math.round((t * this.R.num) / this.R.den)
  }

  private haveAt(k: number): number {
    return k >= 0 && k < this.have.length ? this.have[k] : NONE
  }

  /** Frame k holds its wanted content (so its picture may be drawn). */
  isReady(k: number): boolean {
    if (k < 0 || k >= this.program.total) return false
    const w = this.program.want[k]
    const h = this.haveAt(k)
    return w === GAP ? h !== NONE : h === w
  }

  isBuffered(k: number): boolean {
    return k >= this.bufA && k < this.bufB
  }

  get buffered(): [number, number] {
    return [this.bufA, this.bufB]
  }

  /** Content appended at k (tests, telemetry). */
  contentAt(k: number): number {
    return this.haveAt(k)
  }

  setProgram(program: LaneProgram): void {
    this.program = program
    if (this.have.length < program.total) {
      const grown = new Float64Array(Math.max(program.total, this.have.length * 2))
      grown.fill(NONE)
      grown.set(this.have)
      this.have = grown
    }
    this.poke()
  }

  setPlayhead(k: number, playing: boolean): void {
    this.playhead = Math.max(0, Math.floor(k))
    this.playing = playing
    this.poke()
  }

  /** Something changed (bytes landed): run the append loop. */
  poke(): void {
    void this.pump()
  }

  // ------------------------------------------------------------ planning

  private available(k: number): boolean {
    const w = this.program.want[k]
    if (w === GAP) return this.writer.currentInitKey !== null || this.nearestSampleId(k) !== null
    return this.program.sample(w) !== null
  }

  /** A loaded clip frame to stand in for a gap when nothing was appended yet. */
  private nearestSampleId(k: number): number | null {
    const { total, want } = this.program
    for (let d = 0; d < total; d++) {
      for (const j of [k + d, k - d]) {
        if (j < 0 || j >= total) continue
        const w = want[j]
        if (w !== GAP && this.program.sample(w) !== null) return w
      }
      if (k + d >= total && k - d < 0) break
    }
    return null
  }

  private requestFrame(k: number, urgent: boolean): void {
    const w = this.program.want[k]
    if (w === undefined || w === GAP) return
    this.program.request(w, Math.abs(k - this.playhead), urgent)
  }

  /** Contiguous frames from `k` in direction `dir` that need an append and
   *  whose bytes are here, at most `limit`, never past `stop`. */
  private run(k: number, dir: 1 | -1, limit: number, stop: number): [number, number] | null {
    let a = k
    let b = k
    let n = 0
    for (let j = k; dir > 0 ? j < stop : j >= stop; j += dir) {
      if (n >= limit || this.isReady(j) || !this.available(j)) {
        if (!this.isReady(j) && !this.available(j)) this.requestFrame(j, false)
        break
      }
      if (dir > 0) b = j + 1
      else a = j
      n++
    }
    if (dir > 0) return b > a ? [a, b] : null
    return n > 0 ? [a, k + 1] : null
  }

  /** The next thing to do, or null when the window is complete. Pure
   *  decision over (program, have, interval, playhead) — exported for tests
   *  through `plan()`. */
  plan(): LaneAction | null {
    const total = this.program.total
    const R = this.R.num / this.R.den
    const fps = Math.max(1, Math.round(R))
    if (total <= 0) {
      return this.bufB > this.bufA ? { kind: 'remove', a: this.bufA, b: this.bufB, why: 'empty-program' } : null
    }
    const P = Math.min(Math.max(0, this.playhead), total - 1)
    const winA = Math.max(0, P - this.back)
    const winB = Math.min(total, P + this.ahead)
    const empty = this.bufB <= this.bufA

    // 1. the program got shorter than the interval
    if (!empty && this.bufB > total) return { kind: 'remove', a: Math.max(this.bufA, total), b: this.bufB, why: 'shrink' }
    // 2. media duration follows the program (after any shrink)
    if (this.durationFrames !== total && this.ms && this.ms.readyState === 'open' && !(this.sb?.updating)) {
      return { kind: 'duration', frames: total }
    }
    // 3. the playhead left the interval: start a new one there
    if (!empty && (P < this.bufA - fps || P > this.bufB + fps)) return { kind: 'remove', a: this.bufA, b: this.bufB, why: 'reset' }
    // 4. trim outside the window (2 s hysteresis)
    if (!empty && this.bufA < winA - 2 * fps) return { kind: 'remove', a: this.bufA, b: winA, why: 'behind' }
    if (!empty && this.bufB > winB + 2 * fps) return { kind: 'remove', a: winB, b: this.bufB, why: 'ahead' }
    // 5. stale frames whose bytes are not here: cut them out of the interval
    if (!empty) {
      let s0 = -1
      let s1 = -1
      for (let k = this.bufA; k < this.bufB; k++) {
        if (this.isReady(k) || this.available(k)) continue
        this.requestFrame(k, k === P)
        if (k < P) s0 = k
        else if (s1 < 0) s1 = k
      }
      if (s1 >= 0) return { kind: 'remove', a: s1, b: this.bufB, why: 'stale-unavailable' }
      if (s0 >= 0) return { kind: 'remove', a: this.bufA, b: s0 + 1, why: 'stale-unavailable' }
    }
    // 6. appends, by priority
    if (empty) {
      if (!this.available(P)) {
        this.requestFrame(P, true)
        return null
      }
      const r = this.run(P, 1, this.playing ? fps : this.pausedNear, winB)
      return r ? { kind: 'append', a: r[0], b: r[1], why: 'start' } : null
    }
    const near = this.playing ? this.playingLead : 0
    if (!this.playing) {
      // the frames at the playhead
      if (P < this.bufA) {
        const r = this.run(this.bufA - 1, -1, this.bufA - P, P)
        if (r && r[0] <= P) return { kind: 'append', a: r[0], b: r[1], why: 'near' }
      } else {
        for (let k = P; k < Math.min(total, P + this.pausedNear, this.bufB + 1); k++) {
          if (this.isReady(k)) continue
          if (!this.available(k)) { this.requestFrame(k, k === P); break }
          const r = this.run(k, 1, P + this.pausedNear - k, this.bufB + 1)
          if (r) return { kind: 'append', a: r[0], b: r[1], why: 'near' }
          break
        }
      }
    }
    // stale frames inside the interval, forward from the lead then backward
    for (let k = Math.max(this.bufA, P + near); k < this.bufB; k++) {
      if (this.isReady(k)) continue
      const r = this.run(k, 1, fps, this.bufB)
      if (r) return { kind: 'append', a: r[0], b: r[1], why: 'stale' }
    }
    // forward extension
    if (this.bufB < winB) {
      const r = this.run(this.bufB, 1, fps, winB)
      if (r) return { kind: 'append', a: r[0], b: r[1], why: 'forward' }
    }
    // (while playing, [P, P + lead) stays stale: those frames race the
    // decoder, and the compositor holds the last good frame over them)
    for (let k = Math.min(this.bufB, P) - 1; k >= this.bufA; k--) {
      if (this.isReady(k)) continue
      const r = this.run(k, -1, Math.min(fps, 30), this.bufA)
      if (r) return { kind: 'append', a: r[0], b: r[1], why: 'stale-behind' }
    }
    // backward extension
    if (this.bufA > winA) {
      const r = this.run(this.bufA - 1, -1, Math.min(fps, 30), winA)
      if (r) return { kind: 'append', a: r[0], b: r[1], why: 'backward' }
    }
    return null
  }

  // ------------------------------------------------------------ doing

  private async pump(): Promise<void> {
    if (this.pumping) {
      this.again = true
      return
    }
    if (!this.ms || this.ms.readyState !== 'open' || this.destroyed) return
    this.pumping = true
    try {
      do {
        this.again = false
        for (let guard = 0; guard < 10_000; guard++) {
          if (this.stopped() || this.now() < this.stalledUntil) break
          const action = this.plan()
          if (!action) break
          await this.execute(action)
        }
      } while (this.again && !this.stopped())
    } finally {
      this.pumping = false
      const waiters = this.idleWaiters
      this.idleWaiters = []
      for (const w of waiters) w()
    }
  }

  /** No append or remove may start now (hidden is asked last: it may
   *  suspend the page's loaders). */
  private stopped(): boolean {
    return this.destroyed || this.held || this.suspended || this.hiddenNow()
  }

  /** Stop starting appends/removes (a paused seek is reading the element:
   *  WebKit re-enqueues on appends, and the frame at 'seeked' must be the one
   *  sought). `hold(false)` resumes. */
  hold(on: boolean): void {
    if (this.held === on) return
    this.held = on
    if (!on) this.poke()
  }

  /** Page hidden or window occluded (§3.5): stop appending and removing
   *  (independent of hold(), which paused seeks own); false resumes. */
  suspend(on: boolean): void {
    if (this.suspended === on) return
    this.suspended = on
    if (!on) this.poke()
  }

  get isHeld(): boolean {
    return this.held
  }

  /** Resolves once no append or remove is running. */
  idle(): Promise<void> {
    if (!this.pumping) return Promise.resolve()
    return new Promise((resolve) => this.idleWaiters.push(resolve))
  }

  private async execute(action: LaneAction): Promise<void> {
    if (action.kind === 'duration') {
      try {
        this.ms!.duration = action.frames * this.R.den / this.R.num
        this.durationFrames = action.frames
      } catch {
        // an update is running or frames sit past it: the next plan retries
        this.stall(5)
      }
      return
    }
    if (action.kind === 'remove') {
      await this.removeFrames(action.a, action.b)
      if (action.why === 'stale-unavailable') this.stats.truncations++
      if (action.why === 'reset') this.stats.resets++
      return
    }
    await this.appendFrames(action.a, action.b)
  }

  private waitUpdate(sb: SourceBufferLike): Promise<void> {
    return new Promise((resolve, reject) => {
      const ok = () => { sb.removeEventListener('error', bad); resolve() }
      const bad = () => { sb.removeEventListener('updateend', ok); reject(new SourceBufferEventError()) }
      sb.addEventListener('updateend', ok, { once: true })
      sb.addEventListener('error', bad, { once: true })
    })
  }

  private async removeFrames(a: number, b: number): Promise<void> {
    a = Math.max(a, this.bufA)
    b = Math.min(b, this.bufB)
    if (b <= a) {
      if (this.bufB <= this.bufA) { this.bufA = this.bufB = 0 }
      return
    }
    const sb = this.sb
    if (sb) {
      const start = Math.max(0, ((a - 0.5) * this.R.den) / this.R.num)
      const end = ((b - 0.5) * this.R.den) / this.R.num
      const done = this.waitUpdate(sb)
      try {
        sb.remove(start, end)
        await done
      } catch (e) {
        done.catch(() => undefined)
        this.onFailure(e)
        return
      }
    }
    this.stats.removes++
    for (let k = a; k < b && k < this.have.length; k++) this.have[k] = NONE
    if (a <= this.bufA) this.bufA = b
    if (b >= this.bufB) this.bufB = a
    if (this.bufB <= this.bufA) { this.bufA = 0; this.bufB = 0 }
    this.events.removed?.(a, b)
  }

  private ensureSourceBuffer(codec: string): SourceBufferLike {
    const type = `video/mp4; codecs="${codec}"`
    if (!this.sb) {
      this.sb = this.ms!.addSourceBuffer(type)
      this.sb.mode = 'segments'
      this.sb.addEventListener('error', () => this.onError(new Error('SourceBuffer error event')))
      this.codec = codec
    } else if (codec !== this.codec && this.sb.changeType) {
      this.sb.changeType(type)
      this.codec = codec
    }
    return this.sb
  }

  private async appendFrames(a: number, b: number): Promise<void> {
    const { want } = this.program
    const entries: FrameEntry[] = []
    const ids: number[] = []
    for (let k = a; k < b; k++) {
      const w = want[k]
      if (w === GAP) {
        if (this.writer.currentInitKey === null) {
          // nothing appended yet: stand in the nearest loaded clip frame
          const id = this.nearestSampleId(k)
          const s = id === null ? null : this.program.sample(id)
          if (!s) return
          entries.push(s)
        } else {
          entries.push(null)
        }
        ids.push(FILLER)
      } else {
        const s = this.program.sample(w)
        if (!s) return
        entries.push(s)
        ids.push(w)
      }
    }
    const jump = this.lastEnd >= 0 && a !== this.lastEnd && !!this.sb?.abort && !this.sb.updating
    if (jump) {
      // A batch that does not continue the previous one. WebKit treats a
      // FORWARD jump inside one coded frame group as continuous and removes
      // every frame between the previous batch's end and this one (measured
      // in WK: one-frame overwrites at 578 then 584 left a hole [579, 584),
      // and a hole stalls the element). A new coded frame group
      // (resetParserState) removes only what the new frames overlap; WebKit
      // then drops media until an init segment, so the writer re-sends it.
      this.sb!.abort!()
      this.writer.reset()
      this.stats.jumps++
    }
    let segs: Segment[]
    try {
      segs = this.writer.write(a, entries)
    } catch {
      this.writer.reset()
      return
    }
    const codecOf = new Map<string, string>()
    for (const e of entries) if (e) codecOf.set(e.format.initKey, e.format.codec)
    const wasEmpty = this.bufB <= this.bufA
    // The codec string comes only from parseInitSegment (milestone 1: the
    // real proxies are avc1.641029); with none known there is nothing to guess.
    const firstCodec = codecOf.get(segs[0]?.initKey ?? '') ?? this.codec
    if (!firstCodec) {
      this.writer.reset()
      this.stats.noCodec++
      return
    }
    try {
      this.ensureSourceBuffer(firstCodec)
    } catch (e) {
      // NotSupportedError: this engine cannot decode the proxies at all
      this.stall(60_000)
      this.events.fatal?.(`codec-unsupported: ${String((e as Error)?.message ?? e)}`)
      return
    }
    /** Frames [a, done) are appended, in order. */
    let done = a
    try {
      for (const seg of segs) {
        const sb = this.ensureSourceBuffer(codecOf.get(seg.initKey) ?? this.codec ?? firstCodec)
        const finished = this.waitUpdate(sb)
        try {
          sb.appendBuffer(seg.bytes)
        } catch (e) {
          finished.catch(() => undefined)
          this.writer.reset()
          if (isQuota(e)) await this.onQuota()
          else this.onError(e)
          return
        }
        try {
          await finished
        } catch (e) {
          this.writer.reset()
          this.onFailure(e)
          return
        }
        if (seg.kind === 'init') {
          this.stats.inits++
          continue
        }
        this.stats.appends++
        for (let i = 0; i < seg.frames; i++) this.have[seg.firstFrame + i] = ids[seg.firstFrame + i - a]
        done = seg.firstFrame + seg.frames
        this.lastEnd = done
        this.stats.appendedFrames += seg.frames
        this.stats.fillerFrames += seg.fillers
        this.quotaRetry = false
        this.events.appended?.(seg.firstFrame, done)
      }
    } finally {
      this.mergeInterval(a, done, wasEmpty)
      this.reconcile()
    }
  }

  /** Frames of the interval the SourceBuffer no longer holds (an engine
   *  that removed more than an append overlapped) go back to NONE, so the
   *  plan re-appends them instead of seeking into a hole. */
  private reconcile(): void {
    const sb = this.sb
    if (!sb || this.bufB <= this.bufA) return
    let tr: TimeRangesLike
    try {
      tr = sb.buffered
    } catch {
      return
    }
    const r = this.R.num / this.R.den
    const ranges: Array<[number, number]> = []
    for (let i = 0; i < tr.length; i++) ranges.push([tr.start(i) * r, tr.end(i) * r])
    if (ranges.length === 1 && ranges[0][0] <= this.bufA + 0.5 && ranges[0][1] >= this.bufB - 0.5) return
    for (let k = this.bufA; k < this.bufB; k++) {
      if (this.have[k] === NONE) continue
      const mid = k + 0.5
      if (!ranges.some(([s0, e0]) => mid >= s0 && mid <= e0)) {
        this.have[k] = NONE
        this.stats.lost++
      }
    }
  }

  /** The interval after frames [a, done) were appended: it grows only at an
   *  end. A prefix that stopped short of the interval (a failed backward
   *  batch) is an orphan MSE range: forgotten here, overwritten later. */
  private mergeInterval(a: number, done: number, wasEmpty: boolean): void {
    if (done <= a) return
    if (wasEmpty || this.bufB <= this.bufA) {
      this.bufA = a
      this.bufB = done
      return
    }
    if (a <= this.bufB && done > this.bufB) this.bufB = done
    if (a < this.bufA) {
      if (done >= this.bufA) this.bufA = a
      else for (let k = a; k < done; k++) this.have[k] = NONE
    }
  }

  private async onQuota(): Promise<void> {
    this.stats.quotaHits++
    const R = this.R.num / this.R.den
    if (this.quotaRetry) {
      this.quotaRetry = false
      this.stall(1000)
      this.events.degraded?.('quota')
      return
    }
    this.quotaRetry = true
    // keep only 2 s behind from now on (or the window refills what we free)
    this.back = Math.min(this.back, Math.round(2 * R))
    const keep = Math.max(0, this.playhead - this.back)
    if (this.bufA < keep) await this.removeFrames(this.bufA, keep)
    this.ahead = Math.max(Math.round(2 * R), Math.floor(this.ahead / 2))
  }

  /** A failed remove/append: a synchronous throw counts as one error; an
   *  'error' EVENT was already counted by the SourceBuffer's listener, and
   *  only the append loop's back-off is still owed. */
  private onFailure(e: unknown): void {
    if (e instanceof SourceBufferEventError) {
      this.writer.reset()
      this.stall(50)
      return
    }
    this.onError(e)
  }

  private onError(e: unknown): void {
    void e
    this.stats.errors++
    const t = this.now()
    this.errorTimes = this.errorTimes.filter((x) => t - x < ERROR_WINDOW_MS)
    this.errorTimes.push(t)
    this.writer.reset()
    if (this.errorTimes.length >= FATAL_ERRORS) this.events.fatal?.('sourcebuffer-errors')
    this.stall(50)
  }

  /** Pause the append loop for `ms`, then resume it. */
  private stall(ms: number): void {
    this.stalledUntil = this.now() + ms
    if (this.stallTimer) clearTimeout(this.stallTimer)
    this.stallTimer = setTimeout(() => { this.stallTimer = null; this.poke() }, ms + 1)
  }

  /** Restores the full look-ahead (after the quota pressure is gone). */
  resetLookAhead(): void {
    this.ahead = this.aheadMax
    this.back = this.backMax
  }

  destroy(): void {
    this.destroyed = true
    if (this.stallTimer) clearTimeout(this.stallTimer)
    const v = this.media.video
    try {
      v.pause()
      v.removeAttribute('src')
      v.load()
    } catch {
      // the element may already be gone
    }
    if (this.url) this.media.revokeObjectURL(this.url)
    this.sb = null
    this.ms = null
  }
}
