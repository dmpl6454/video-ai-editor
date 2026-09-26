// MSE plumbing for the WK acceptance pages: a laneA-shaped SourceBuffer fed by
// the REAL Fmp4Writer, timeline plans over proxy sources, and the seek / rVFC
// / playback-trace primitives the §13 probes measure with. Test-only: the
// product lane is media/laneA.ts.
import { Fmp4Writer, type FrameEntry, type Rate, type Segment } from '../media/fmp4Writer'
import { barCode, type BarReader } from './barcode'
import type { ProxySource } from './proxySource'

export type PlanEntry = readonly [ProxySource, number] | null
export type Plan = PlanEntry[]

/** Frames a..b (exclusive) of `src`, stepping by `step` (negative = reverse). */
export function seg(src: ProxySource, a: number, b: number, step = 1): PlanEntry[] {
  const out: PlanEntry[] = []
  if (step > 0) for (let i = a; i < b; i += step) out.push([src, i])
  else for (let i = a; i > b; i += step) out.push([src, i])
  return out
}
export const gap = (n: number): PlanEntry[] => new Array<PlanEntry>(n).fill(null)
export const each = (entries: PlanEntry[], times: number): PlanEntry[] => entries.flatMap((e) => new Array<PlanEntry>(times).fill(e))

export const codeOfEntry = (e: PlanEntry): number | null => (e ? barCode(e[0].srcId, e[1]) : null)

/** The bar each k shows when the whole plan is written in order: a gap shows
 *  its filler — the previous frame, or the first frame for a leading gap. */
export function expectedCodes(plan: Plan): number[] {
  const first = plan.find((e) => e !== null)
  let last = first ? codeOfEntry(first)! : -1
  return plan.map((e) => {
    const c = codeOfEntry(e)
    if (c !== null) last = c
    return last
  })
}

export const toEntries = (plan: readonly PlanEntry[]): FrameEntry[] => plan.map((e) => (e ? e[0].sample(e[1]) : null))

export const secondsOf = (k: number, R: Rate): number => (k * R.den) / R.num
export const midFrame = (k: number, R: Rate): number => ((k + 0.5) * R.den) / R.num
export const frameAt = (t: number, R: Rate): number => Math.round((t * R.num) / R.den)

export function mulberry32(seed: number): () => number {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

export const now = (): number => performance.now()
export const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms))

// --------------------------------------------------------------------- lane

export function appendOne(sb: SourceBuffer, bytes: Uint8Array): Promise<void> {
  return new Promise((resolve, reject) => {
    const ok = () => { sb.removeEventListener('error', bad); resolve() }
    const bad = () => { sb.removeEventListener('updateend', ok); reject(new Error('SourceBuffer append error')) }
    sb.addEventListener('updateend', ok, { once: true })
    sb.addEventListener('error', bad, { once: true })
    sb.appendBuffer(bytes as Uint8Array<ArrayBuffer>)
  })
}

export function removeRange(sb: SourceBuffer, a: number, b: number): Promise<void> {
  return new Promise((resolve) => {
    sb.addEventListener('updateend', () => resolve(), { once: true })
    sb.remove(a, b)
  })
}

export const ranges = (tr: TimeRanges): [number, number][] => {
  const out: [number, number][] = []
  for (let i = 0; i < tr.length; i++) out.push([+tr.start(i).toFixed(4), +tr.end(i).toFixed(4)])
  return out
}

/** One MediaSource + one `segments` SourceBuffer on a muted <video>, written
 *  through one Fmp4Writer — the laneA shape (§3.2). `codes[k]` is what frame
 *  k must show after everything written so far. */
export class Lane {
  readonly video: HTMLVideoElement
  readonly writer: Fmp4Writer
  readonly codes: number[] = []
  inits = 0
  fragments = 0
  private lastCode: number | null = null
  private ms!: MediaSource
  private sb!: SourceBuffer

  readonly rate: Rate

  private constructor(video: HTMLVideoElement, rate: Rate) {
    this.video = video
    this.rate = rate
    this.writer = new Fmp4Writer({ rate })
  }

  static async open(rate: Rate, codec: string, size = { w: 320, h: 180 }): Promise<Lane> {
    const video = document.createElement('video')
    video.muted = true
    video.playsInline = true
    video.disableRemotePlayback = true
    video.style.width = `${size.w}px`
    video.style.height = `${size.h}px`
    document.body.appendChild(video)
    const lane = new Lane(video, rate)
    const MS = (window.MediaSource ?? (window as unknown as { ManagedMediaSource: typeof MediaSource }).ManagedMediaSource)
    lane.ms = new MS()
    video.src = URL.createObjectURL(lane.ms)
    await new Promise((r) => lane.ms.addEventListener('sourceopen', r, { once: true }))
    lane.sb = lane.ms.addSourceBuffer(`video/mp4; codecs="${codec}"`)
    lane.sb.mode = 'segments'
    return lane
  }

  get sourceBuffer(): SourceBuffer { return this.sb }
  get mediaSource(): MediaSource { return this.ms }

  async appendSegments(segs: readonly Segment[]): Promise<void> {
    for (const s of segs) {
      if (s.kind === 'init') this.inits++
      else this.fragments++
      await appendOne(this.sb, s.bytes)
    }
  }

  /** Writes `plan` at output frames `k0…`, and records the codes it shows
   *  (a gap shows the writer's filler: the last sample appended). */
  async write(k0: number, plan: readonly PlanEntry[]): Promise<number> {
    const t0 = now()
    const segs = this.writer.write(k0, toEntries(plan))
    let last = this.lastCode ?? codeOfEntry(plan.find((e) => e !== null) ?? null) ?? -1
    plan.forEach((e, i) => {
      const c = codeOfEntry(e)
      if (c !== null) last = c
      this.codes[k0 + i] = last
    })
    this.lastCode = last
    await this.appendSegments(segs)
    return now() - t0
  }

  setDuration(frames: number): void {
    this.ms.duration = secondsOf(frames, this.rate)
  }

  dispose(): void {
    this.video.pause()
    this.video.removeAttribute('src')
    this.video.load()
    this.video.remove()
  }
}

// ----------------------------------------------------------- seek and frames

/** How long a paused seek may take before it is a failure. Real seeks take
 *  < 250 ms (§2); a seek into a buffered HOLE never fires `seeked`, and
 *  without a bound the page would hang until the harness deadline and say
 *  only "posted no result". */
export const SEEK_TIMEOUT_MS = 2000

export class SeekTimeout extends Error {
  readonly t: number
  readonly buffered: [number, number][]
  readonly ms: number
  constructor(t: number, buffered: [number, number][], ms: number) {
    super(`seek to t=${t} got no 'seeked' within ${ms} ms; buffered=${JSON.stringify(buffered)}`)
    this.name = 'SeekTimeout'
    this.t = t
    this.buffered = buffered
    this.ms = ms
  }
}

/** Seeks and resolves with the ms until `seeked`; rejects with SeekTimeout
 *  (naming the target and the buffered ranges) after `timeoutMs`. */
export function seekTo(video: HTMLVideoElement, t: number, timeoutMs = SEEK_TIMEOUT_MS): Promise<number> {
  return new Promise((resolve, reject) => {
    const t0 = now()
    const onSeeked = () => { clearTimeout(timer); resolve(now() - t0) }
    const timer = setTimeout(() => {
      video.removeEventListener('seeked', onSeeked)
      reject(new SeekTimeout(t, ranges(video.buffered), timeoutMs))
    }, timeoutMs)
    video.addEventListener('seeked', onSeeked, { once: true })
    video.currentTime = t
  })
}

export function nextFrame(video: HTMLVideoElement, timeoutMs = 300): Promise<VideoFrameCallbackMetadata | null> {
  return new Promise((resolve) => {
    let done = false
    video.requestVideoFrameCallback((_n, m) => { if (!done) { done = true; resolve(m) } })
    setTimeout(() => { if (!done) { done = true; resolve(null) } }, timeoutMs)
  })
}

export interface TraceRow {
  k: number
  got: number
  exp: number | null
  mediaTime: number
  expectedDisplayTime: number
  presentedFrames: number
  width: number
  height: number
  /** The element's size at the callback, for comparison with the metadata. */
  videoWidth: number
  videoHeight: number
}

export interface Trace {
  rows: TraceRow[]
  mismatches: [number, number | null, number][]
  missingK: number[]
  waiting: number
  timedOut: boolean
  /** The element fired `ended` before the trace reached `untilK - 1`. */
  ended: boolean
  /** On a timeout: the element's state then, to tell a starved display
   *  pipeline (currentTime still advancing, no rVFC) from a stalled media
   *  pipeline (currentTime frozen). Null when the trace did not time out. */
  stall: null | { lastCbMsAgo: number; currentTimeK: number; currentTimeKAfter1s: number; paused: boolean;
    readyState: number; visibility: string; hasFocus: boolean; totalFrames: number; droppedFrames: number }
}

/** Plays from frame `fromK` and reads the bar on every rVFC until `untilK`.
 *  `presentedK = round(mediaTime · R)` (R8), never currentTime. */
export async function playTrace(
  lane: Lane, reader: BarReader, fromK: number, untilK: number,
  onFrame?: (k: number) => void,
): Promise<Trace> {
  const { video, rate } = lane
  await seekTo(video, midFrame(fromK, rate))
  const rows: TraceRow[] = []
  let waiting = 0
  const onWaiting = () => { waiting++ }
  video.addEventListener('waiting', onWaiting)
  let timedOut = false
  let ended = false
  let lastCb = now()
  let stall: Trace['stall'] = null
  // End-of-stream ends the trace too, reported as `ended` rather than as a
  // timeout, so a clean ending is never mistaken for a stall. A real stall
  // (no frames and no `ended`) still times out.
  const onEnded = () => { ended = true; finish() }
  let finish = () => {}
  video.addEventListener('ended', onEnded)
  await new Promise<void>((resolve) => {
    let finished = false
    finish = () => { if (!finished) { finished = true; resolve() } }
    const cb: VideoFrameRequestCallback = (_n, m) => {
      if (finished) return
      lastCb = now()
      const k = frameAt(m.mediaTime, rate)
      rows.push({
        k, got: reader.read(video), exp: lane.codes[k] ?? null, mediaTime: m.mediaTime,
        expectedDisplayTime: m.expectedDisplayTime, presentedFrames: m.presentedFrames, width: m.width, height: m.height,
        videoWidth: video.videoWidth, videoHeight: video.videoHeight,
      })
      onFrame?.(k)
      if (k >= untilK - 1) return finish()
      video.requestVideoFrameCallback(cb)
    }
    video.requestVideoFrameCallback(cb)
    video.play().catch((e: unknown) => { rows.push({ k: -1, got: -1, exp: null, mediaTime: -1, expectedDisplayTime: 0, presentedFrames: 0, width: 0, height: 0, videoWidth: 0, videoHeight: 0 }); console.error(e); finish() })
    setTimeout(() => {
      timedOut = true
      const q = video.getVideoPlaybackQuality?.()
      const k0 = frameAt(video.currentTime, rate)
      setTimeout(() => {
        stall = { lastCbMsAgo: Math.round(now() - lastCb), currentTimeK: k0, currentTimeKAfter1s: frameAt(video.currentTime, rate),
          paused: video.paused, readyState: video.readyState, visibility: document.visibilityState, hasFocus: document.hasFocus(),
          totalFrames: q?.totalVideoFrames ?? -1, droppedFrames: q?.droppedVideoFrames ?? -1 }
        finish()
      }, 1000)
    }, (secondsOf(untilK - fromK, rate) + 4) * 1000)
  })
  video.pause()
  video.removeEventListener('waiting', onWaiting)
  video.removeEventListener('ended', onEnded)
  const seen = new Set(rows.map((r) => r.k))
  const missingK: number[] = []
  if (rows.length) for (let k = rows[0].k; k <= rows[rows.length - 1].k; k++) if (!seen.has(k)) missingK.push(k)
  return {
    rows,
    mismatches: rows.filter((r) => r.got !== r.exp).map((r) => [r.k, r.exp, r.got]),
    missingK,
    waiting,
    timedOut,
    ended,
    stall,
  }
}

/** A trace without the per-row bulk, for the posted result. */
export function summarize(t: Trace) {
  const first = t.rows[0]
  const last = t.rows[t.rows.length - 1]
  return {
    frames: t.rows.length, mismatches: t.mismatches, missingK: t.missingK, waiting: t.waiting, timedOut: t.timedOut, ended: t.ended, stall: t.stall,
    firstK: first?.k ?? null, lastK: last?.k ?? null,
  }
}
