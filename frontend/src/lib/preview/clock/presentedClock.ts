// The PRESENTED-frame clock (INSTANT_PREVIEW_SPEC §3.5, §6 R8, R11).
//
// * While playing, the timeline authority is the frame WebKit actually
//   presented: every laneA rVFC gives `presentedK = round(mediaTime · R)`;
//   `now()` is presentedK / R, advanced between frames by the time since that
//   frame's expectedDisplayTime (at most one frame), so rAF-driven overlays
//   move smoothly yet never run ahead of the picture.
// * While paused, `now()` is the frame on screen.
// * Audio anchoring: the sound is started so the sample of media time m0 is
//   heard at AudioContext time c0 (the ANCHOR); at the first presented frame
//   the error e = ctxAt(expectedDisplayTime) − (c0 + (mediaTime − m0)) says
//   how far the sound is from the picture; above 4 ms it is re-anchored once
//   (5 ms crossfade, the sink's job). A drift monitor re-checks every 500 ms
//   and re-anchors above 8 ms (DRIFT_THRESHOLD_S; the spec's 20 ms left WK's
//   measured 14 ms render-thread loss in place), at most once per 2 s.
// * Play-start latency: the rolling median of the last 8 measured laneA
//   play-start latencies (seeded at 40 ms) picks when the sound starts.
//
// Pure arithmetic over numbers the engine passes in: unit-tested in vitest,
// exercised for real in the WK suite.

import type { Rational } from '../timeline/timebase'

export const SAMPLE_RATE = 48000
/** |e| above which the first-frame check re-anchors (§3.5). */
export const ANCHOR_TOLERANCE_S = 0.004
/** Drift monitor: check period, threshold and minimum gap (§3.5). */
export const DRIFT_CHECK_MS = 500
/** Deviation from §3.5 (20 ms): measured in WK under load, the output clock
 *  loses up to 14 ms over 15 s (render-thread underruns), which a 20 ms
 *  threshold leaves in place (p95 > 10 ms). 8 ms is well above the check's
 *  own noise (±1 render quantum, 2.7 ms). */
export const DRIFT_THRESHOLD_S = 0.008
export const DRIFT_MIN_GAP_MS = 2000

export class PresentedClock {
  R: Rational
  presentedK = 0
  playing = false
  /** performance.now() ms at which presentedK is (was) displayed. */
  private shownAt = 0

  constructor(R: Rational) {
    this.R = R
  }

  get frameSeconds(): number {
    return this.R.den / this.R.num
  }

  /** A frame presented while playing (rVFC). */
  onPresented(k: number, expectedDisplayTime: number): void {
    this.presentedK = k
    this.shownAt = expectedDisplayTime
    this.playing = true
  }

  /** Paused on frame k. */
  setPaused(k: number): void {
    this.presentedK = k
    this.playing = false
  }

  /** Seconds of the picture on screen at `perfNow` (performance.now() ms). */
  now(perfNow: number = performance.now()): number {
    const base = (this.presentedK * this.R.den) / this.R.num
    if (!this.playing) return base
    const ahead = Math.min(Math.max(0, (perfNow - this.shownAt) / 1000), this.frameSeconds)
    return base + ahead
  }
}

/** Rolling median of the last `size` play-start latencies (ms). */
export class PlayStartLatency {
  private readonly samples: number[]
  private readonly size: number
  constructor(size = 8, seedMs = 40) {
    this.size = size
    this.samples = [seedMs]
  }
  add(ms: number): void {
    if (!Number.isFinite(ms) || ms < 0 || ms > 2000) return
    this.samples.push(ms)
    while (this.samples.length > this.size) this.samples.shift()
  }
  median(): number {
    const s = [...this.samples].sort((a, b) => a - b)
    const m = s.length >> 1
    return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2
  }
  get count(): number {
    return this.samples.length
  }
}

/** Sound is scheduled so media time `mediaTime` is heard at `ctxTime`. */
export interface AudioAnchor {
  ctxTime: number
  mediaTime: number
}

/** Output sample (48 kHz) of media time t (R9: S(k) at the frame start). */
export const sampleAt = (t: number): number => Math.round(t * SAMPLE_RATE)

/** Signed seconds the sound lags (+) or leads (−) the frame presented at
 *  `ctxAtDisplay` (the AudioContext time of its expectedDisplayTime). */
export function anchorError(anchor: AudioAnchor, ctxAtDisplay: number, mediaTime: number): number {
  return ctxAtDisplay - (anchor.ctxTime + (mediaTime - anchor.mediaTime))
}

/** The anchor that puts media time `mediaTime + leadS` at `ctxAtDisplay +
 *  leadS` (a re-anchor a little in the future, so it can be scheduled). */
export function reanchor(ctxAtDisplay: number, mediaTime: number, leadS = 0.05): AudioAnchor {
  return { ctxTime: ctxAtDisplay + leadS, mediaTime: mediaTime + leadS }
}

/** Measured play-start latency (ms): when the first presented frame was due
 *  on screen, minus when play() was called, minus the media time it moved. */
export function playStartLatencyMs(playCalledAt: number, expectedDisplayTime: number, fromMedia: number, mediaTime: number): number {
  return expectedDisplayTime - playCalledAt - (mediaTime - fromMedia) * 1000
}

export class DriftMonitor {
  private lastCheck = -Infinity
  private lastReanchor = -Infinity
  reanchors = 0
  maxAbsErrorS = 0

  /** Is a check due at `nowMs`? */
  due(nowMs: number): boolean {
    return nowMs - this.lastCheck >= DRIFT_CHECK_MS
  }

  /** Record a measured error; true when the engine should re-anchor now. */
  check(errorS: number, nowMs: number): boolean {
    this.lastCheck = nowMs
    this.maxAbsErrorS = Math.max(this.maxAbsErrorS, Math.abs(errorS))
    if (Math.abs(errorS) <= DRIFT_THRESHOLD_S) return false
    if (nowMs - this.lastReanchor < DRIFT_MIN_GAP_MS) return false
    this.lastReanchor = nowMs
    this.reanchors++
    return true
  }

  reset(): void {
    this.lastCheck = -Infinity
  }
}

/** An expectedDisplayTime further than this from the callback's own `now`
 *  is on another clock. */
const SANE_DISPLAY_MS = 1000

/**
 * rVFC `metadata.expectedDisplayTime` on the performance.now() timeline.
 *
 * Measured in WKWebView (macOS 27, this repo's harness): for a <video> fed
 * by MediaSource, `expectedDisplayTime` and `presentationTime` come on a
 * different clock (≈ −70.3e6 ms while the callback's `now` is ~700 ms; a
 * file-backed <video> reports them sanely, 13-26 ms before `now`). Every use
 * of the value (the overlay clock, the audio anchor) needs performance.now()
 * ms. The two clocks differ by a constant, and a frame's callback never runs
 * before the frame is presented, so `now − expectedDisplayTime` is that
 * constant plus a lag ≥ 0: the smallest one seen since the last reset() is
 * the best estimate of the constant. A median would follow the vsync phase
 * (30 fps frames on a 60 Hz display) and step by several ms mid-play.
 */
export class DisplayTimeBase {
  private offset = Infinity
  repaired = 0

  /** A new play: WebKit's MSE display clock is re-based across a pause. */
  reset(): void {
    this.offset = Infinity
  }

  fix(cbNow: number, expectedDisplayTime: number): number {
    const d = cbNow - expectedDisplayTime
    if (!Number.isFinite(d)) return cbNow
    if (Math.abs(d) <= SANE_DISPLAY_MS) return expectedDisplayTime
    this.repaired++
    if (d < this.offset) this.offset = d
    return expectedDisplayTime + this.offset
  }
}
