// WK acceptance page (tests/wk/pages/mse.html → this bundle). One scenario per
// load, chosen by `?scenario=`; the page posts its measurements as JSON to
// `/__result/<token>` and the pytest side asserts on them. Every fragment the
// page appends comes from the real media/fmp4Writer over proxy-shaped span
// packs, so these are the INSTANT_PREVIEW_SPEC §2 WebKit facts re-measured on
// the code that ships (§13).
import type { Rate } from '../media/fmp4Writer'
import { BarReader, NO_PICTURE } from './barcode'
import {
  Lane, SeekTimeout, each, gap, midFrame, mulberry32, nextFrame, now, playTrace, ranges, secondsOf, seekTo, seg,
  sleep, summarize, type Plan,
} from './mseKit'
import { loadProxy, type ProxySource } from './proxySource'

const q = new URLSearchParams(location.search)
const token = q.get('token') ?? 'none'
const scenario = q.get('scenario') ?? ''
const rate: Rate = { num: Number(q.get('num') ?? 30), den: Number(q.get('den') ?? 1) }

type Sources = Record<'A' | 'B' | 'C', ProxySource>
type Result = Record<string, unknown>

/** Every cut in `plan`: k where the source changes or the frame does not follow. */
function cutsOf(plan: Plan): number[] {
  const cuts: number[] = []
  for (let k = 1; k < plan.length; k++) {
    const a = plan[k - 1], b = plan[k]
    if (!a || !b || a[0] !== b[0] || b[1] !== a[1] + 1) cuts.push(k)
  }
  return cuts
}

/** Paused seek to (k+0.5)/R; the bar at `seeked`, and after one rVFC if wrong. */
async function probeSeek(lane: Lane, reader: BarReader, k: number) {
  let ms: number
  try {
    ms = await seekTo(lane.video, midFrame(k, lane.rate))
  } catch (e) {
    // A seek that never completes (a hole) is a wrong frame AT k, reported
    // with the buffered ranges — not a page that hangs to the deadline.
    if (!(e instanceof SeekTimeout)) throw e
    return { k, exp: lane.codes[k], atSeeked: -2, after: -2, ms: -1, timedOut: true, buffered: e.buffered }
  }
  const atSeeked = reader.read(lane.video)
  let after = atSeeked
  if (atSeeked !== lane.codes[k]) {
    await nextFrame(lane.video, 150)
    after = reader.read(lane.video)
  }
  return { k, exp: lane.codes[k], atSeeked, after, ms: +ms.toFixed(2) }
}

// The spec's F1 fixture shape: cuts over 3 sources of 2 size classes, reverse,
// 2x, a gap, and 2x duplicates (slow motion / conform-up).
const mixedPlan = (S: Sources): Plan => [
  ...seg(S.A, 100, 160), ...seg(S.B, 50, 110), ...seg(S.A, 229, 199, -1), ...seg(S.A, 60, 120, 2),
  ...gap(10), ...seg(S.C, 0, 30), ...each(seg(S.B, 0, 20), 2),
]

const scenarios: Record<string, (S: Sources, reader: BarReader) => Promise<Result>> = {
  /** Paused mid-frame seeks are exact at `seeked` (spec §2: 42/42). */
  async seek_exact(S, reader) {
    const lane = await Lane.open(rate, S.A.format.codec)
    const plan = mixedPlan(S)
    await lane.write(0, plan)
    lane.setDuration(plan.length)
    await seekTo(lane.video, midFrame(0, rate))
    await nextFrame(lane.video, 300)
    const ks = new Set<number>()
    for (const c of cutsOf(plan)) for (const k of [c - 1, c, c + 1]) if (k >= 0 && k < plan.length) ks.add(k)
    const rnd = mulberry32(rate.num * 7 + rate.den)
    while (ks.size < 42) ks.add(Math.floor(rnd() * plan.length))
    const seeks = []
    for (const k of ks) seeks.push(await probeSeek(lane, reader, k))
    const bufferedRanges = ranges(lane.sourceBuffer.buffered)
    lane.dispose()
    return { frames: plan.length, inits: lane.inits, fragments: lane.fragments, buffered: bufferedRanges, seeks }
  },

  /** Frame stepping: each (k+0.5)/R seek repaints the NEXT frame, forward and
   *  back; exact-boundary k/R seeks are recorded (the WebKit bug R7 avoids). */
  async seek_step(S, reader) {
    const lane = await Lane.open(rate, S.A.format.codec)
    await lane.write(0, seg(S.A, 0, 240))
    lane.setDuration(240)
    await seekTo(lane.video, midFrame(100, rate))
    await nextFrame(lane.video, 300)
    const steps = []
    let prev = reader.read(lane.video)
    for (const k of [...Array.from({ length: 40 }, (_, i) => 101 + i), ...Array.from({ length: 40 }, (_, i) => 139 - i)]) {
      const r = await probeSeek(lane, reader, k)
      steps.push({ ...r, prev })
      prev = r.atSeeked
    }
    const boundary = []
    for (const k of [30, 31, 32, 50, 61, 62, 90, 121, 122, 180]) {
      await seekTo(lane.video, midFrame(k - 3, rate))
      await seekTo(lane.video, secondsOf(k, rate))
      await nextFrame(lane.video, 150)
      boundary.push({ k, exp: lane.codes[k], got: reader.read(lane.video), prev: lane.codes[k - 1] })
    }
    lane.dispose()
    return { steps, boundary }
  },

  /** Overwrites: at the paused playhead (edit to pixel), and while playing,
   *  4 and 15 frames ahead of the presented frame (spec §2: 0 stale in WK). */
  async overwrite(S, reader) {
    const lane = await Lane.open(rate, S.A.format.codec)
    await lane.write(0, seg(S.A, 0, 210))
    lane.setDuration(210)
    // paused
    await seekTo(lane.video, midFrame(100, rate))
    await nextFrame(lane.video, 300)
    const before = reader.read(lane.video)
    const t0 = now()
    const appendMs = await lane.write(90, seg(S.B, 0, 30))
    const noSeek = reader.read(lane.video)
    const reSeekMs = await seekTo(lane.video, midFrame(100, rate))
    const reSeek = reader.read(lane.video)
    const paused = { before, exp: lane.codes[100], appendMs: +appendMs.toFixed(2), noSeek, reSeek, reSeekMs: +reSeekMs.toFixed(2), editToPixelMs: +(now() - t0).toFixed(2) }
    // playing
    const edits: Result[] = []
    const plan = [[30, 4, 100], [120, 15, 150]] as const
    const fired = [false, false]
    const trace = await playTrace(lane, reader, 0, 210, (k) => {
      plan.forEach(([at, ahead, bFrom], i) => {
        if (fired[i] || k < at) return
        fired[i] = true
        const k0 = k + ahead
        const t = now()
        lane.write(k0, seg(S.B, bFrom, bFrom + 30)).then((ms) => edits.push({ presentedK: k, firstK: k0, ahead, appendMs: +ms.toFixed(2), doneAfterMs: +(now() - t).toFixed(2) }))
      })
    })
    lane.dispose()
    return { paused, playing: { ...summarize(trace), edits } }
  },

  /** Mixed size classes: an init segment before each class change plays and
   *  seeks seamlessly (spec §2). Sizes come from rVFC metadata (§3.4). */
  async init_switch(S, reader) {
    const lane = await Lane.open(rate, S.A.format.codec)
    const plan: Plan = [...seg(S.A, 0, 30), ...seg(S.C, 0, 30), ...seg(S.A, 30, 60), ...seg(S.B, 0, 30), ...seg(S.C, 30, 60)]
    await lane.write(0, plan)
    lane.setDuration(plan.length)
    const trace = await playTrace(lane, reader, 0, plan.length)
    const sizes = [...new Set(trace.rows.map((r) => `${r.width}x${r.height}`))]
    // the size must switch on exactly the frame the class changes
    const wrongSize = trace.rows.filter((r) => {
      const e = plan[r.k]
      return e && (r.width !== e[0].format.width || r.height !== e[0].format.height)
    }).map((r) => [r.k, r.width, r.height, r.videoWidth, r.videoHeight])
    const seeks = []
    for (const k of [29, 30, 31, 59, 60, 61, 89, 90, 91, 119, 120, 121, 5, 45, 100, 75]) seeks.push(await probeSeek(lane, reader, k))
    const bufferedRanges = ranges(lane.sourceBuffer.buffered)
    lane.dispose()
    return { inits: lane.inits, sizes, wrongSize, buffered: bufferedRanges, trace: summarize(trace), seeks }
  },

  /** A hole in the buffered ranges stalls playback; the same timeline gap
   *  written as filler samples plays through (why fillers exist, §3.2). */
  async gap_stall(S, reader) {
    // (a) raw hole: frames 60..89 never appended
    const holed = await Lane.open(rate, S.A.format.codec)
    await holed.write(0, seg(S.A, 0, 60))
    // bypass the writer's filler: write 90..150 as a separate run
    await holed.write(90, seg(S.A, 90, 150))
    holed.setDuration(150)
    const holedBuffered = ranges(holed.sourceBuffer.buffered)
    await seekTo(holed.video, midFrame(0, rate))
    let holedWaiting = 0
    holed.video.addEventListener('waiting', () => { holedWaiting++ })
    await holed.video.play().catch(() => undefined)
    await sleep(secondsOf(60, rate) * 1000 + 2000)
    const hole = {
      buffered: holedBuffered, currentTime: +holed.video.currentTime.toFixed(4), stallAt: +secondsOf(60, rate).toFixed(4),
      waiting: holedWaiting, ended: holed.video.ended, paused: holed.video.paused, readyState: holed.video.readyState,
    }
    holed.dispose()
    // (b) the same content with the gap written through the writer (fillers)
    const filled = await Lane.open(rate, S.A.format.codec)
    await filled.write(0, [...seg(S.A, 0, 60), ...gap(30), ...seg(S.A, 90, 150)])
    filled.setDuration(150)
    const filledBuffered = ranges(filled.sourceBuffer.buffered)
    const trace = await playTrace(filled, reader, 0, 150)
    const gapRows = trace.rows.filter((r) => r.k >= 60 && r.k < 90)
    filled.dispose()
    return { hole, filled: { buffered: filledBuffered, ...summarize(trace), gapFrames: gapRows.length, gapCodes: [...new Set(gapRows.map((r) => r.got))] } }
  },

  /** The presented-frame clock (§3.5, R8): on every rVFC, mediaTime sits on
   *  the k/R grid the writer's tfdt = k·T put it on, and the bar is frame k. */
  async rvfc_clock(S, reader) {
    const lane = await Lane.open(rate, S.A.format.codec)
    // Pad the stream past the traced range, so the trace's last frame is never
    // the stream's final frame and end-of-stream behaviour cannot decide the
    // result. (The ~4% load-dependent timeout the wave D1 gate saw was NOT this:
    // it was App Nap throttling the harness process, fixed in tests/wk/harness.py.
    // The padding stays as a defensive measure.)
    const TRACE = 180
    const total = Math.min(S.A.index.frames, TRACE + 30)
    if (total <= TRACE) throw new Error(`rvfc_clock fixture too short to pad: ${S.A.index.frames} frames`)
    await lane.write(0, seg(S.A, 0, total))
    lane.setDuration(total)
    const trace = await playTrace(lane, reader, 0, TRACE)
    const rows = trace.rows
    const R = rate.num / rate.den
    const offGrid = rows.map((r) => Math.abs(r.mediaTime * R - Math.round(r.mediaTime * R)))
    let monotonic = true
    for (let i = 1; i < rows.length; i++) {
      if (!(rows[i].mediaTime > rows[i - 1].mediaTime && rows[i].presentedFrames > rows[i - 1].presentedFrames
        && rows[i].expectedDisplayTime > rows[i - 1].expectedDisplayTime)) monotonic = false
    }
    // Display-clock rate: expectedDisplayTime advance per presented k ≈ 1000/R ms.
    const span = rows.length > 1 ? rows[rows.length - 1] : null
    const msPerFrame = span ? (span.expectedDisplayTime - rows[0].expectedDisplayTime) / (span.k - rows[0].k) : null
    // The MEDIAN per-frame display interval is the rate check. A few transient
    // stalls under load from other processes inflate the whole-trace mean
    // (measured 39.9 ms at 29.97 fps, load average ~10) while every presented
    // frame was right; the median ignores them yet still catches a systematic
    // error (a 30 fps stream paced at 25 fps moves it from 33.4 to 40 ms).
    const intervals: number[] = []
    for (let i = 1; i < rows.length; i++) {
      const dk = rows[i].k - rows[i - 1].k
      if (dk > 0) intervals.push((rows[i].expectedDisplayTime - rows[i - 1].expectedDisplayTime) / dk)
    }
    intervals.sort((a, b) => a - b)
    const msPerFrameMedian = intervals.length ? intervals[Math.floor(intervals.length / 2)] : null
    lane.dispose()
    return {
      ...summarize(trace), maxOffGrid: Math.max(0, ...offGrid), monotonic, msPerFrame, msPerFrameMedian, expectedMsPerFrame: 1000 / R,
      noPicture: rows.filter((r) => r.got === NO_PICTURE).length,
    }
  },
}

async function post(body: Result): Promise<void> {
  await fetch(`/__result/${token}`, { method: 'POST', body: JSON.stringify(body) })
}

;(async () => {
  const result: Result = { scenario, rate, ua: navigator.userAgent }
  try {
    const run = scenarios[scenario]
    if (!run) throw new Error(`unknown scenario ${JSON.stringify(scenario)}`)
    result.api = {
      MediaSource: 'MediaSource' in window, ManagedMediaSource: 'ManagedMediaSource' in window,
      rvfc: 'requestVideoFrameCallback' in HTMLVideoElement.prototype,
    }
    const ids = (q.get('srcIds') ?? '').split(',').filter(Boolean).map(Number)
    const [A, B, C] = await Promise.all(['A', 'B', 'C'].map((n, i) => loadProxy('/media', n, ids[i])))
    result.codec = A.format.codec
    result.isTypeSupported = MediaSource.isTypeSupported(`video/mp4; codecs="${A.format.codec}"`)
    const t0 = now()
    Object.assign(result, await run({ A, B, C }, new BarReader()))
    result.scenarioMs = +(now() - t0).toFixed(1)
  } catch (e) {
    result.fatal = String((e as Error)?.stack ?? e)
  }
  await post(result)
})()
