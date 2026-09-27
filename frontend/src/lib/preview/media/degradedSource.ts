// The DEGRADED SOURCE tier (INSTANT_PREVIEW_SPEC §7, §6 R7, §3.2 media
// elements): when a source's proxy is failed (410, `index.json` failed, or a
// transient open failure that outlived its retries) the PAUSED frames of its
// ranges come from ONE paused <video> playing the normalised MASTER.
//
// * Seek to pts(source frame) + PAUSED_SEEK_BIAS_S (1 ms): never an exact
//   boundary (WebKit fails to repaint one, §10), and inside the frame's own
//   display interval.
// * Confirm with requestVideoFrameCallback's `mediaTime` (the frame the
//   element really shows); on a mismatch re-seek ONCE, corrected by the
//   difference. The frame is handed over only when confirmed, or when no
//   rVFC arrives within CONFIRM_MS (then the seek's own target is trusted and
//   the miss is counted).
// * One element for the engine's life, created lazily (the engine adds at
//   most 2 media elements: laneA and this one). A new source is a new `src`.
// * Latest wins: a newer show() abandons an older one.
//
// No GL here: the engine uploads `element` as the frame's texture when
// `onReady` fires, exactly as it uploads laneA's element at 'seeked'.

import { ptsOf, type SourceInfo } from '../timeline/frameMap'
import { presentedTime } from './presentedFrame'

/** Seconds past a frame's pts that a degraded paused seek targets (R7). */
export const PAUSED_SEEK_BIAS_S = 0.001
/** How long a completed seek waits for an rVFC confirming its frame. */
export const CONFIRM_MS = 250
/** A seek (or a load) that never completes is abandoned after this. */
export const DEGRADED_TIMEOUT_MS = 5000

/** Where a degraded frame comes from. */
export interface DegradedRequest {
  /** URL of the normalised master (`/api/sessions/{sid}/files/uploads/…`). */
  url: string
  info: SourceInfo
  /** Source frame index (presentation order after the edit list, R5). */
  frame: number
  /** The engine's content id for this frame (what the texture will hold). */
  contentId: number
}

/** The DOM the tier touches (a fake in degradedSource.test.ts). */
export interface DegradedVideo extends EventTarget {
  src: string
  currentTime: number
  readonly seeking: boolean
  readonly readyState: number
  readonly videoWidth: number
  readonly videoHeight: number
  muted: boolean
  preload: string
  pause(): void
  load(): void
  removeAttribute(name: string): void
  requestVideoFrameCallback?(cb: (now: number, meta: { mediaTime: number }) => void): number
  cancelVideoFrameCallback?(handle: number): void
}

export interface DegradedOptions {
  /** Creates (once) and mounts the element. */
  createVideo: () => DegradedVideo
  /** Output frame `k`'s picture is on the element now: upload it. */
  onReady: (k: number, contentId: number) => void
  /** The master could not be loaded or sought (`k`'s frame stays held). */
  onFailed?: (k: number, why: string) => void
  /** The presented frame's media time, or null (default: a VideoFrame of it). */
  presented?: (el: DegradedVideo) => number | null
  confirmMs?: number
  timeoutMs?: number
}

/** Seconds of source frame `i` on the element's timeline (the master's pts,
 *  relative to its start_time: frameMap's startTicks convention). */
export function frameTime(info: SourceInfo, i: number): number {
  return (ptsOf(info, i) * info.tb.num) / info.tb.den
}

/** The source frame a presented `mediaTime` belongs to: the last frame
 *  whose pts is at or before it (half a microsecond of float slack). */
export function frameAtTime(info: SourceInfo, t: number): number {
  const start = (info.startTicks * info.tb.num) / info.tb.den
  return Math.floor((t - start + 5e-7) * info.rate.num / info.rate.den)
}

interface Job {
  k: number
  req: DegradedRequest
  token: number
  reseeked: boolean
}

export class DegradedSource {
  /** `stamped`: confirmed by the presented frame's VideoFrame timestamp at
   *  'seeked'; `confirmed`: by rVFC mediaTime; `unconfirmed`: neither
   *  answered within CONFIRM_MS (the seek target is trusted). */
  /** `superseded` / `cancelled`: shows a newer show or a cancel replaced
   *  before their frame was handed over (review RD3: a proxy failing at
   *  start now shows the paused frame at once, and the first seek can
   *  replace that show). */
  readonly stats = { shows: 0, loads: 0, seeks: 0, reseeks: 0, stamped: 0, confirmed: 0, unconfirmed: 0, mismatches: 0, failures: 0,
    reused: 0, superseded: 0, cancelled: 0 }
  private readonly opts: DegradedOptions
  private el: DegradedVideo | null = null
  private url: string | null = null
  private job: Job | null = null
  private token = 0
  /** (url, content id) the element shows, confirmed; null: unknown. */
  private showing: { url: string; contentId: number } | null = null
  private timer: ReturnType<typeof setTimeout> | null = null
  private rvfc = 0
  private destroyed = false

  constructor(opts: DegradedOptions) {
    this.opts = opts
  }

  /** The element (null until the first show()). */
  get element(): DegradedVideo | null {
    return this.el
  }

  /** Output frame a show() is on its way to (−1: none). */
  get pending(): number {
    return this.job ? this.job.k : -1
  }

  /** Show `req` for output frame k; `onReady(k, id)` once it is on the element. */
  show(k: number, req: DegradedRequest): void {
    if (this.destroyed) return
    this.stats.shows++
    if (this.job) this.stats.superseded++
    const job: Job = { k, req, token: ++this.token, reseeked: false }
    this.job = job
    this.cancelWaits()
    const el = this.element_()
    if (this.showing && this.showing.url === req.url && this.showing.contentId === req.contentId && !el.seeking) {
      // already on the element (the texture was lost, or another k with the
      // same source frame)
      this.stats.reused++
      this.finish(job)
      return
    }
    if (this.url !== req.url) {
      this.url = req.url
      this.showing = null
      this.stats.loads++
      el.src = req.url
      el.load()
    }
    // before its metadata the element cannot seek: onMeta seeks the job then
    if (el.readyState >= 1) this.seek(job, frameTime(req.info, req.frame) + PAUSED_SEEK_BIAS_S)
    else this.armTimeout(job)
  }

  /** Forget the pending show (a new target on laneA, playback started). */
  cancel(): void {
    if (this.job) this.stats.cancelled++
    this.token++
    this.job = null
    this.cancelWaits()
  }

  private element_(): DegradedVideo {
    if (!this.el) {
      const el = this.opts.createVideo()
      el.muted = true
      el.preload = 'auto'
      el.addEventListener('seeked', this.onSeeked)
      el.addEventListener('loadedmetadata', this.onMeta)
      el.addEventListener('error', this.onError)
      this.el = el
    }
    return this.el
  }

  private seek(job: Job, t: number): void {
    const el = this.el
    if (!el || this.job !== job) return
    this.cancelWaits()
    this.armTimeout(job)
    this.stats.seeks++
    this.showing = null
    el.pause()
    if (Math.abs(el.currentTime - t) < 1e-9 && !el.seeking) {
      // already there: a seek to the same time fires nothing in WebKit
      this.confirm(job)
      return
    }
    el.currentTime = t
  }

  private readonly onMeta = (): void => {
    const job = this.job
    if (job && this.el && !this.el.seeking) this.seek(job, frameTime(job.req.info, job.req.frame) + PAUSED_SEEK_BIAS_S)
  }

  private readonly onSeeked = (): void => {
    const job = this.job
    const el = this.el
    if (!job || !el || el.seeking) return
    this.confirm(job)
  }

  private readonly onError = (): void => {
    const job = this.job
    this.url = null
    this.showing = null
    if (!job) return
    this.stats.failures++
    this.job = null
    this.cancelWaits()
    this.opts.onFailed?.(job.k, 'media-error')
  }

  /** Wait for the frame the element really presents, then hand it over. */
  private confirm(job: Job): void {
    const el = this.el
    if (!el || this.job !== job) return
    const t = (this.opts.presented ?? ((v: DegradedVideo) => presentedTime(v as unknown as HTMLVideoElement)))(el)
    if (t !== null && frameAtTime(job.req.info, t) === job.req.frame) {
      // presented now (WKWebView's file-backed element reports its
      // currentTime here and fires no rVFC for a paused seek: measured
      // 1/255, while 'seeked' uploads were 295/295 exact to the bar)
      this.stats.stamped++
      this.finish(job)
      return
    }
    if (!el.requestVideoFrameCallback) {
      this.stats.unconfirmed++
      this.finish(job)
      return
    }
    this.cancelWaits()
    this.rvfc = el.requestVideoFrameCallback((_now, meta) => {
      this.rvfc = 0
      if (this.job !== job) return
      const got = frameAtTime(job.req.info, meta.mediaTime)
      if (got === job.req.frame) {
        this.stats.confirmed++
        this.finish(job)
        return
      }
      this.stats.mismatches++
      if (job.reseeked) {
        // twice wrong: hold the last good frame rather than show another one
        this.job = null
        this.cancelWaits()
        this.opts.onFailed?.(job.k, `mismatch: wanted ${job.req.frame}, shows ${got}`)
        return
      }
      job.reseeked = true
      this.stats.reseeks++
      const info = job.req.info
      const delta = frameTime(info, job.req.frame) - frameTime(info, got)
      this.seek(job, el.currentTime + delta)
    })
    this.timer = setTimeout(() => {
      this.timer = null
      if (this.job !== job) return
      // no presented-frame callback for this seek: trust the seek target
      this.stats.unconfirmed++
      this.finish(job)
    }, this.opts.confirmMs ?? CONFIRM_MS)
  }

  private finish(job: Job): void {
    if (this.job !== job) return
    this.job = null
    this.cancelWaits()
    this.showing = { url: job.req.url, contentId: job.req.contentId }
    this.opts.onReady(job.k, job.req.contentId)
  }

  private armTimeout(job: Job): void {
    this.timer = setTimeout(() => {
      this.timer = null
      if (this.job !== job) return
      this.job = null
      this.stats.failures++
      this.opts.onFailed?.(job.k, 'timeout')
    }, this.opts.timeoutMs ?? DEGRADED_TIMEOUT_MS)
  }

  /** Drop the rVFC wait and the timer (a new phase arms its own). */
  private cancelWaits(): void {
    if (this.rvfc && this.el?.cancelVideoFrameCallback) this.el.cancelVideoFrameCallback(this.rvfc)
    this.rvfc = 0
    if (this.timer) clearTimeout(this.timer)
    this.timer = null
  }

  destroy(): void {
    this.destroyed = true
    this.cancel()
    const el = this.el
    if (!el) return
    el.removeEventListener('seeked', this.onSeeked)
    el.removeEventListener('loadedmetadata', this.onMeta)
    el.removeEventListener('error', this.onError)
    try {
      el.pause()
      el.removeAttribute('src')
      el.load()
    } catch {
      // already gone
    }
  }
}
