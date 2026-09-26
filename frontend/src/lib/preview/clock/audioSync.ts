// Sound against the PRESENTED picture (INSTANT_PREVIEW_SPEC §3.5, R11): the
// engine's side of the AudioSink contract. It starts the sink so the sample
// of the media time the picture starts from is heard when that frame is due
// on screen (play-start latency median), corrects the anchor at the first
// presented frame (> 4 ms), restarts after a buffering stall or a seek (on a
// frame of the NEW position: the engine's PlayingSeekGate), and re-anchors on
// drift (> 8 ms, presentedClock.DRIFT_THRESHOLD_S, at most once per 2 s). The maths lives in
// presentedClock.ts; this is the state machine the engine drives.

import type { AudioSink } from '../engine'
import {
  ANCHOR_TOLERANCE_S, DriftMonitor, PlayStartLatency, anchorError, playStartLatencyMs, reanchor, sampleAt,
  type AudioAnchor,
} from './presentedClock'

/** The presented frame at which the anchor is checked a second time (same
 *  4 ms tolerance): by then the display-time estimate has settled
 *  (DisplayTimeBase's median; WebKit's MSE rVFC times need it), while the
 *  first frame's can be a vsync off. After it, only the drift monitor. */
export const SETTLE_FRAMES = 8

export interface FrameTiming {
  /** rVFC mediaTime (s) of the presented frame. */
  mediaTime: number
  /** rVFC expectedDisplayTime (performance.now() ms). */
  expectedDisplayTime: number
}

export class AudioSync {
  anchor: AudioAnchor | null = null
  readonly latency = new PlayStartLatency()
  readonly drift = new DriftMonitor()
  readonly stats = { starts: 0, reanchors: 0, firstFrameErrorMs: [] as number[] }
  private readonly sink: () => AudioSink
  private playCalledAt = 0
  private playFromMedia = 0
  private firstFrame = false
  /** Presented frames since play() (the settle check at SETTLE_FRAMES). */
  private framesSincePlay = 0
  private restart = false

  constructor(sink: () => AudioSink) {
    this.sink = sink
  }

  /** play(), inside the user's gesture: the sound for media time
   *  `fromMedia` is scheduled for when its frame should be on screen. */
  play(perfNow: number, fromMedia: number): void {
    this.playCalledAt = perfNow
    this.playFromMedia = fromMedia
    this.firstFrame = true
    this.framesSincePlay = 0
    this.restart = false
    this.drift.reset()
    this.startAt(perfNow + this.latency.median(), fromMedia)
  }

  /** Silence now (a pause, a stall, a seek), in lockstep with the picture. */
  stop(rampMs: number): void {
    this.sink().stop(rampMs)
    this.anchor = null
  }

  /** Start again from a fresh anchor at the next presented frame. */
  requestRestart(): void {
    this.restart = true
  }

  /** Every presented frame while playing. */
  onFrame(f: FrameTiming, nowMs: number = performance.now()): void {
    const sink = this.sink()
    if (this.restart) {
      this.restart = false
      this.framesSincePlay = 0
      // 50 ms ahead: far enough to be schedulable, near enough to be heard
      // with the next frames
      this.startAt(f.expectedDisplayTime + 50, f.mediaTime + 0.05)
      return
    }
    const first = this.firstFrame
    const settle = ++this.framesSincePlay === SETTLE_FRAMES
    if (first) {
      this.firstFrame = false
      this.latency.add(playStartLatencyMs(this.playCalledAt, f.expectedDisplayTime, this.playFromMedia, f.mediaTime))
    }
    const ctxAt = sink.ctxTimeAt(f.expectedDisplayTime)
    if (ctxAt === null) return
    if (!this.anchor) {
      // the sink had no clock when it started (its AudioContext is created
      // inside that first start): anchor it to the frame on screen now
      this.reanchorAt(ctxAt, f.mediaTime)
      return
    }
    const e = anchorError(this.anchor, ctxAt, f.mediaTime)
    if (first) {
      this.stats.firstFrameErrorMs.push(e * 1000)
      if (Math.abs(e) > ANCHOR_TOLERANCE_S) this.reanchorAt(ctxAt, f.mediaTime)
      return
    }
    if (settle) {
      if (Math.abs(e) > ANCHOR_TOLERANCE_S) this.reanchorAt(ctxAt, f.mediaTime)
      return
    }
    if (this.drift.due(nowMs) && this.drift.check(e, nowMs)) this.reanchorAt(ctxAt, f.mediaTime)
  }

  private startAt(perfMs: number, media: number): void {
    const sink = this.sink()
    const ctx = sink.ctxTimeAt(perfMs)
    this.stats.starts++
    if (ctx === null) {
      // no running context (the null sink, or before the first resume)
      this.anchor = null
      sink.start(0, sampleAt(media))
      return
    }
    this.anchor = { ctxTime: ctx, mediaTime: media }
    sink.start(ctx, sampleAt(media))
  }

  private reanchorAt(ctxAt: number, mediaTime: number): void {
    this.anchor = reanchor(ctxAt, mediaTime)
    this.sink().start(this.anchor.ctxTime, sampleAt(this.anchor.mediaTime))
    this.stats.reanchors++
  }
}
