// currentTime on the laneA element, and WebKit's answers to it
// (INSTANT_PREVIEW_SPEC §4.1 step 5, §3.2): the engine's paused seek holds
// laneA's appends, waits for it to go idle, seeks to (k + 0.5)/R and uploads
// the frame at 'seeked'. A 'seeked' of an EARLIER seek can be dispatched
// after a newer one was issued, so the engine counts assignments against
// 'seeking' events and only a 'seeked' with the two in step is taken.
//
// Measured in WK (review RD2): two assignments of the SAME time in one task,
// the second while the first is still seeking, fire ONE 'seeking'. The
// counters then drifted one apart for good and every later paused seek was
// taken for a stale one (the spinner stayed up until the Preview remounted).
// So: only the latest start() may assign, an identical pending assignment is
// not repeated, and the seek timeout re-syncs the counters.
//
// Also measured (fixer, WK): a paused seek that finds its frame ready can see
// it REMOVED before laneA goes idle (a 'behind' trim of the window already in
// flight). Assigning then seeks into a hole WebKit never completes, with the
// appends held: a 2 s stall until the timeout. Readiness is checked again at
// the moment of the assignment.

import type { LaneA } from './media/laneA'

export const SEEK_TIMEOUT_MS = 2000

export class ElementSeeker {
  /** Frame a paused seek is on its way to (−1: none). */
  inFlight = -1
  /** currentTime assignments, and 'seeking' events seen. */
  issued = 0
  seen = 0
  private token = 0
  private timer: ReturnType<typeof setTimeout> | null = null
  private lastTime = Number.NaN
  private readonly onTimeout: (k: number) => void
  private readonly onNotReady: (k: number) => void
  private readonly timeoutMs: number

  /** `onTimeout(k)`: the paused seek to k never completed (a hole under it).
   *  `onNotReady(k)`: k's frame left the buffer before the seek could be
   *  issued (the seek is over; appends are still held). */
  constructor(onTimeout: (k: number) => void, onNotReady: (k: number) => void = onTimeout, timeoutMs = SEEK_TIMEOUT_MS) {
    this.onTimeout = onTimeout
    this.onNotReady = onNotReady
    this.timeoutMs = timeoutMs
  }

  /** Sets currentTime, counted. The same time again while that seek is still
   *  pending is not re-assigned: WebKit would fire no second 'seeking'. */
  assign(video: HTMLVideoElement, t: number): void {
    if (video.seeking && t === this.lastTime) return
    this.issued++
    this.lastTime = t
    video.currentTime = t
  }

  readonly onSeeking = (): void => {
    this.seen++
  }

  /** The 'seeked' being dispatched is the latest assignment's. */
  isLatest(video: HTMLVideoElement): boolean {
    return !video.seeking && this.seen >= this.issued
  }

  /** The paused seek to k: appends HOLD, and once laneA is idle the element
   *  seeks to its (k + 0.5)/R. Only the latest start() assigns: seek(k) →
   *  seek(k + 1) → seek(k) while laneA is busy queues three idle()
   *  callbacks, and the first and the last would both see inFlight === k. */
  start(k: number, video: HTMLVideoElement, lane: LaneA, stillValid: () => boolean): void {
    this.inFlight = k
    this.arm(k, video)
    lane.hold(true)
    const token = ++this.token
    void lane.idle().then(() => {
      if (token !== this.token || this.inFlight !== k || !stillValid()) return
      if (!lane.isReady(k)) {
        this.finish()
        this.onNotReady(k)
        return
      }
      this.assign(video, lane.seekTime(k))
    })
  }

  /** The paused seek is over (shown, dropped, or superseded by play). */
  finish(): void {
    this.inFlight = -1
    this.token++
    this.clearTimer()
  }

  clearTimer(): void {
    if (this.timer) clearTimeout(this.timer)
    this.timer = null
  }

  private arm(k: number, video: HTMLVideoElement): void {
    this.clearTimer()
    this.timer = setTimeout(() => {
      this.timer = null
      // self-heal: whatever event WebKit swallowed, no seek is pending now
      if (!video.seeking) this.seen = this.issued
      if (this.inFlight !== k) return
      this.inFlight = -1
      this.onTimeout(k)
    }, this.timeoutMs)
  }
}

/** After a seek while PLAYING, WebKit still presents frames of the OLD
 *  position for a while (review RD2: a sound restart 3 ms after the seek
 *  anchored on a frame 319 away). The sound restart waits for a frame of the
 *  NEW position: one within the frames elapsed since the seek (plus 2) of its
 *  target — a stale frame continues the old position — or WAIT_MS at most. */
export class PlayingSeekGate {
  static readonly WAIT_MS = 500
  private k = -1
  private at = 0

  seeked(k: number, now = performance.now()): void {
    this.k = k
    this.at = now
  }

  clear(): void {
    this.k = -1
  }

  /** Presented frame `k` may anchor the sound (and the gate opens for good). */
  admits(k: number, fps: number, now = performance.now()): boolean {
    if (this.k < 0) return true
    const ms = now - this.at
    if (Math.abs(k - this.k) <= (ms * fps) / 1000 + 2 || ms > PlayingSeekGate.WAIT_MS) {
      this.k = -1
      return true
    }
    return false
  }
}
