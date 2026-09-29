// Instant preview SOAK page (tests/wk/test_wk_soak.py; INSTANT_PREVIEW_SPEC
// §11.3, §13 P1-M1 and P1-E1): the app's client mode end to end, served
// same-origin next to a REAL backend (uvicorn thread, 1080p bar-coded
// click-track masters, the app's own proxies and routes). Every edit takes
// the store's client-mode path — POST /dispatch?include=edl, then
// PreviewController.applyTimeline — and the sound is the real AudioEngine.
//
// Scenarios:
// * soak — play the timeline in a loop for `minutes`, an edit every
//   `editEveryS` (split, trim, move, delete, undo in rotation, ahead of the
//   playhead inside laneA's look-ahead), a paused bar check at a random frame
//   every `checkEveryS`; sample laneA's SourceBuffer span, the span and audio
//   LRUs and the media-element count every second. The harness samples the
//   WebContent footprint from outside (FootprintSampler). Every frame drawn
//   while playing is read back (bar code) and judged against the program on
//   the engine at the time, except frames an edit may still legitimately
//   show old (before presentedK + 5 at the answer, §13 P1-F4).
// * edit_script — P1-E1 through the app path: 100 dispatch edits (paused and
//   playing, with seeks) while counting the media elements created and live.
//
// The page keeps only bounded state (counters, a 64-draw ring, one sample
// row per `rowEveryS`) so its own bookkeeping cannot look like a leak.

import { PreviewController } from '../previewController'
import type { ClientPreviewEngine } from '../engine'
import { AudioEngine } from '../audio/audioEngine'
import { AudioChunks } from '../audio/audioChunks'
import type { EdlLike } from '../timeline/framePlan'
import { KIND_GAP, type ProgramMap } from '../timeline/programMap'
import { expectedBar, percentile, readBar } from './canvasProbe'
import { NO_PICTURE, barFrame, barSrc } from './barcode'
import { mulberry32, now, sleep } from './mseKit'

const q = new URLSearchParams(location.search)
const token = q.get('token') ?? 'none'
const mailbox = q.get('mailbox') ?? ''
const scenario = q.get('scenario') ?? ''
type Result = Record<string, unknown>

interface Config {
  sid: string
  /** src (as the EDL stores it) → bar source id */
  srcIds: Record<string, number>
  canvas: [number, number]
  minutes?: number
  editEveryS?: number
  checkEveryS?: number
  /** Where playback starts (s): the short form starts near the end so the
   *  loop is exercised inside its 5 minutes. */
  startAtS?: number
  /** Span LRU / decoded-audio LRU caps (bytes); default: the engine's (§11.3). */
  spanCacheBytes?: number
  audioCapBytes?: number
  rowEveryS?: number
  edits?: number
  seed?: number
  /** Chromium only: it keeps showing frames it already decoded when they are
   *  overwritten ahead of the playhead (3 stale frames, spec §10 "wk-only"),
   *  so its logic run skips this long after an answer. WebKit: 0. */
  staleMs?: number
  /** end_restart: how long the round waits at the end before playing again
   *  (a user presses Space hundreds of ms after playback stopped, past the
   *  engine's own-pause window; the soak's loop plays again within 20 ms). */
  pauseBeforePlayMs?: number
  /** end_restart: keep the main thread busy this long right after play()
   *  (the app's click handler: the store update, React's render of the
   *  transport and the timeline). laneA's remove/append then lands after
   *  the parked element — 16.7 ms before the media duration — has run off
   *  the end: WebKit fires 'ended' (measured in the app: 'ended' 7-27 ms
   *  after 'playing'; here the remove came in 8-16 ms and won the race). */
  busyAfterPlayMs?: number
  /** end_restart: report the element's events for every round, not only
   *  the suspicious ones. */
  traceAll?: boolean
}

interface Draw { k: number; bar: number; playing: boolean; at: number }

const RING = 64
const fmt = (c: number) => (c < 0 ? String(c) : `${barSrc(c)}:${barFrame(c)}`)
const api = (cfg: Config) => `/api/sessions/${cfg.sid}`

// ------------------------------------------------------ element counting

/** Media elements created on this page (P1-E1), counted from before the
 *  engine exists. */
const media = { created: 0 }
{
  const orig = document.createElement.bind(document)
  document.createElement = ((tag: string, o?: ElementCreationOptions) => {
    const t = tag.toLowerCase()
    if (t === 'video' || t === 'audio') media.created++
    return orig(tag, o)
  }) as typeof document.createElement
}
const liveMedia = () => document.querySelectorAll('video,audio').length

// ------------------------------------------------- unhandled page errors

const unhandled = { quota: 0, other: 0, samples: [] as string[] }
function noteUnhandled(err: unknown): void {
  const name = (err as { name?: string } | null)?.name ?? ''
  const msg = String((err as { message?: string } | null)?.message ?? err)
  if (name === 'QuotaExceededError' || /quota/i.test(msg)) unhandled.quota++
  else unhandled.other++
  if (unhandled.samples.length < 10) unhandled.samples.push(`${name}: ${msg}`.slice(0, 300))
}
window.addEventListener('error', (e) => noteUnhandled(e.error ?? e.message))
window.addEventListener('unhandledrejection', (e) => noteUnhandled(e.reason))

// ------------------------------------------------------------------ rig

interface Rig {
  cfg: Config
  ctl: PreviewController
  engine: ClientPreviewEngine
  audio: AudioEngine | null
  srcIds: Map<string, number>
  canvasEdl: { w: number; h: number }
  ring: Draw[]
  judge: Judge
}

/** Frames drawn while playing, judged against the engine's program. */
class Judge {
  drawn = 0
  judged = 0
  skipped = 0
  bad = 0
  badSamples: Array<Record<string, unknown>> = []
  /** The last edit's answer: frames before peAnswer + 5 may still be old. */
  gate: { tAnswer: number; peAnswer: number } | null = null
  staleMs = 0

  see(pm: ProgramMap, d: Draw, srcIds: ReadonlyMap<string, number>): void {
    if (!d.playing) return
    this.drawn++
    const g = this.gate
    if (g && d.at > g.tAnswer && (d.k < g.peAnswer + 5 || d.at - g.tAnswer < this.staleMs)) { this.skipped++; return }
    this.judged++
    const exp = expectedBar(pm, d.k, srcIds)
    if (d.bar === exp) return
    this.bad++
    if (this.badSamples.length < 20) this.badSamples.push({ k: d.k, exp: fmt(exp), got: fmt(d.bar), at: +d.at.toFixed(0) })
  }
}

async function rig(cfg: Config, withAudio: boolean): Promise<Rig> {
  const host = document.createElement('div')
  host.style.cssText = `position:relative;width:${cfg.canvas[0]}px;height:${cfg.canvas[1]}px;`
  document.body.appendChild(host)
  let audio: AudioEngine | null = null
  const ctl = new PreviewController({
    sessionId: cfg.sid,
    audio: withAudio
      ? (onInt) => (audio = new AudioEngine({
        chunks: new AudioChunks(cfg.audioCapBytes ? { capBytes: cfg.audioCapBytes } : {}), onInterrupted: onInt,
      }))
      : false,
    engineOptions: { canvasSize: { w: cfg.canvas[0], h: cfg.canvas[1] }, spanCacheBytes: cfg.spanCacheBytes },
  })
  const engine = ctl.attach(host)
  if (!engine || engine.status.mode !== 'client') throw new Error(`engine did not start: ${JSON.stringify(engine?.status)}`)
  const r: Rig = {
    cfg, ctl, engine, audio, srcIds: new Map(Object.entries(cfg.srcIds)), canvasEdl: { w: 1, h: 1 }, ring: [],
    judge: new Judge(),
  }
  r.judge.staleMs = cfg.staleMs ?? 0
  engine.internals.compositor!.onDrawn = (info, gl) => {
    const pm = engine.program
    if (!pm) return
    const bar = info.black ? NO_PICTURE : readBar(gl, pm, info.k, r.canvasEdl, (src) => ctl.lookup(src)?.info ?? null)
    const d: Draw = { k: info.k, bar, playing: engine.playing, at: now() }
    r.ring.push(d)
    if (r.ring.length > RING) r.ring.shift()
    r.judge.see(pm, d, r.srcIds)
  }
  const edl = await (await fetch(`${api(cfg)}/edl`)).json() as EdlLike
  const c = edl.canvas as { w: number; h: number }
  r.canvasEdl = { w: c.w, h: c.h }
  ctl.applyTimeline(edl, null)
  for (let i = 0; i < 800 && !(ctl.renderHash && engine.program && sourcesKnown(r)); i++) await sleep(25)
  if (!ctl.renderHash) throw new Error('no render hash')
  if (await shown(r, 0, 30000, 0) < 0) throw new Error(`the first frame never showed: ${diag(r, 0)}`)
  return r
}

function sourcesKnown(r: Rig): boolean {
  const pm = r.engine.program
  return !!pm && pm.sources.every((s) => r.ctl.lookup(s)?.proxy?.key)
}

/** ms (since `since`) until frame k is drawn PAUSED with the bar the current
 *  program names there; -1 on timeout. */
async function shown(r: Rig, k: number, ms: number, since = now()): Promise<number> {
  const t0 = now()
  while (now() - t0 < ms) {
    const pm = r.engine.program
    if (pm) {
      const exp = expectedBar(pm, k, r.srcIds)
      const d = r.ring.find((x) => x.k === k && !x.playing && x.bar === exp && x.at >= since)
      if (d) return d.at - since
    }
    await sleep(4)
  }
  return -1
}

function diag(r: Rig, k: number): string {
  const pm = r.engine.program
  return JSON.stringify({
    k, exp: pm ? fmt(expectedBar(pm, k, r.srcIds)) : null, ring: r.ring.slice(-4).map((d) => ({ ...d, bar: fmt(d.bar) })),
    target: r.engine.targetK, presented: r.engine.presentedK, status: { ...r.engine.status, ranges: r.engine.status.ranges.length },
    lane: r.engine.internals.lane?.stats, buffered: r.engine.internals.lane?.buffered,
  })
}

// ---------------------------------------------------------------- edits

interface V1Clip { id: string; src: string; start: number; in: number; out: number }

function clipFrames(pm: ProgramMap): Array<{ ci: number; k0: number; k1: number }> {
  const out: Array<{ ci: number; k0: number; k1: number }> = []
  for (let k = 0; k < pm.total; k++) {
    if (pm.kind[k] === KIND_GAP) continue
    const last = out[out.length - 1]
    if (last && last.ci === pm.clip[k] && last.k1 === k) last.k1 = k + 1
    else out.push({ ci: pm.clip[k], k0: k, k1: k + 1 })
  }
  return out
}

const refusals: Record<string, string> = {}

/** The store's client-mode dispatch: include=edl, then applyTimeline. */
async function edit(r: Rig, tool: string, args: Record<string, unknown>): Promise<{ ms: number } | null> {
  const send = () => fetch(`${api(r.cfg)}/dispatch?include=edl`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ tool, args }),
  })
  const t0 = now()
  let res = await send()
  for (let i = 0; res.status === 429 && i < 50; i++) {
    await sleep(200)
    res = await send()
  }
  if (!res.ok) {
    refusals[tool] ??= `${res.status} ${(await res.text()).slice(0, 200)}`
    return null
  }
  const body = await res.json() as { edl?: EdlLike; render_hash: string }
  if (!body.edl) throw new Error(`${tool}: answer without an EDL`)
  r.judge.gate = { tAnswer: now(), peAnswer: r.engine.presentedK }
  r.ctl.applyTimeline(body.edl, body.render_hash)
  return { ms: now() - t0 }
}

const ROTATION = ['split', 'trim', 'move', 'delete', 'undo'] as const
type Kind = (typeof ROTATION)[number]

/** The edit of `kind` near frame `k0`: on the first clip that starts at
 *  least `lead` frames later (inside laneA's look-ahead while playing). */
function editNear(pm: ProgramMap, kind: Kind, k0: number, lead: number): [string, Record<string, unknown>] | null {
  const R = pm.R
  const t = (k: number) => +((k * R.den) / R.num).toFixed(4)
  if (kind === 'undo') return ['undo', {}]
  if (kind === 'split') return ['split_at', { track: 'v1', time: t(k0 + lead + 7) }]
  const spans = clipFrames(pm)
  const i = spans.findIndex((sp) => sp.k0 >= k0 + lead && sp.k1 - sp.k0 >= 45)
  if (i < 0) return null
  const c = pm.clips[spans[i].ci] as unknown as V1Clip
  if (kind === 'trim') return ['trim_clip', { clip_id: c.id, in: +(c.in + 0.3).toFixed(3) }]
  if (kind === 'delete') return ['bulk_delete', { clip_ids: [c.id] }]
  // a main-lane move is a reorder (the Timeline drag): after its right neighbour
  const next = spans[i + 1]
  return next ? ['move_clip', { clip_id: c.id, close_gap: true, new_start: t((next.k0 + next.k1) / 2) }] : null
}

// ------------------------------------------------------------- sampling

interface Sample {
  t: number
  /** Seconds the SourceBuffer holds (sum of its ranges) and their extent. */
  bufSec: number
  bufExtentSec: number
  bufRanges: number
  spanBytes: number
  audioBytes: number
  media: number
  clips: number
  total: number
  k: number
}

function bufferedOf(r: Rig): { sum: number; extent: number; n: number } {
  const sb = r.engine.internals.lane?.sourceBuffer
  const b = sb?.buffered
  if (!b || b.length === 0) return { sum: 0, extent: 0, n: 0 }
  let sum = 0
  for (let i = 0; i < b.length; i++) sum += b.end(i) - b.start(i)
  return { sum, extent: b.end(b.length - 1) - b.start(0), n: b.length }
}

function sample(r: Rig, t0: number): Sample {
  const buf = bufferedOf(r)
  const pm = r.engine.program
  return {
    t: +((now() - t0) / 1000).toFixed(1), bufSec: +buf.sum.toFixed(2), bufExtentSec: +buf.extent.toFixed(2), bufRanges: buf.n,
    spanBytes: r.engine.internals.store.bytes, audioBytes: r.audio?.chunks.bytes ?? 0, media: liveMedia(),
    clips: pm?.clips.length ?? 0, total: pm?.total ?? 0, k: r.engine.presentedK,
  }
}

class Peaks {
  bufSec = 0
  bufExtentSec = 0
  bufRanges = 0
  spanBytes = 0
  audioBytes = 0
  media = 0
  add(s: Sample): void {
    this.bufSec = Math.max(this.bufSec, s.bufSec)
    this.bufExtentSec = Math.max(this.bufExtentSec, s.bufExtentSec)
    this.bufRanges = Math.max(this.bufRanges, s.bufRanges)
    this.spanBytes = Math.max(this.spanBytes, s.spanBytes)
    this.audioBytes = Math.max(this.audioBytes, s.audioBytes)
    this.media = Math.max(this.media, s.media)
  }
}

/** The divergence summary without its per-edit list (unbounded in a soak). */
function withoutRecent(d: object): Result {
  const out: Result = { ...d }
  delete out.recent
  return out
}

function engineSummary(r: Rig): Result {
  const st = r.engine.stats
  const h = st.handlerMs
  return {
    framesDrawn: st.framesDrawn, blackFrames: st.blackFrames, heldFrames: st.heldFrames, externalPauses: st.externalPauses,
    mediaElementsCreated: st.mediaElementsCreated, seeks: st.seeks, seekTimeouts: st.seekTimeouts,
    handlerMs: { p50: percentile(h, 0.5), p99: percentile(h, 0.99), max: Math.max(0, ...h) },
    lane: r.engine.internals.lane?.stats, laneLookAhead: r.engine.internals.lane?.lookAheadFrames,
    store: { ...r.engine.internals.store.stats, bytes: r.engine.internals.store.bytes, maxBytes: r.engine.internals.store.maxBytes,
      spans: r.engine.internals.store.cachedSpans },
    audio: r.audio ? { ...r.audio.chunks.stats, bytes: r.audio.chunks.bytes, capBytes: r.audio.chunks.capBytes, chunks: r.audio.chunks.size,
      engine: r.audio.stats } : null,
    audioSync: r.engine.audioStats, status: { ...r.engine.status, ranges: r.engine.status.ranges.length },
    controller: r.ctl.stats, divergence: withoutRecent(r.ctl.divergence()),
  }
}

// ------------------------------------------------------------- scenarios

const scenarios: Record<string, (cfg: Config) => Promise<Result>> = {
  /** P1-M1: loop the timeline for `minutes` with an edit every 10 s. */
  async soak(cfg) {
    const r = await rig(cfg, true)
    const { engine, ctl } = r
    const R = engine.program!.R
    const minutes = cfg.minutes ?? 30
    const editEvery = (cfg.editEveryS ?? 10) * 1000
    const checkEvery = (cfg.checkEveryS ?? 60) * 1000
    const rowEvery = (cfg.rowEveryS ?? 5) * 1000
    const rnd = mulberry32(cfg.seed ?? 20260926)
    let buffering = 0
    engine.on('buffering', (e) => { if (e.buffering) buffering++ })
    const extPauses: Record<string, number> = {}
    engine.on('pause-external', (e) => { extPauses[e.cause] = (extPauses[e.cause] ?? 0) + 1 })
    const peaks = new Peaks()
    const rows: Sample[] = []
    const edits = { applied: 0, refused: 0, skipped: 0, byKind: {} as Record<string, number>, ms: [] as number[] }
    const checks = { n: 0, ok: 0, bad: [] as string[], ms: [] as number[] }
    let loops = 0
    let resumes = 0
    const t0 = now()
    const startedAtEpochMs = Date.now()
    ctl.play(cfg.startAtS ?? 0)
    let nextEdit = t0 + editEvery
    let nextCheck = t0 + checkEvery
    let nextRow = t0
    let nextSample = t0
    let rot = 0
    const endAt = t0 + minutes * 60_000
    while (now() < endAt) {
      await sleep(100)
      const t = now()
      if (t >= nextSample) {
        nextSample = t + 1000
        const s = sample(r, t0)
        peaks.add(s)
        if (t >= nextRow) { nextRow = t + rowEvery; rows.push(s) }
      }
      const pm = engine.program!
      if (!engine.playing) {
        // the end of the program: loop; anything else (a pause WebKit made
        // that the engine did not resume): resume where it stopped
        if (engine.presentedK >= pm.total - 2) { loops++; ctl.play(0) }
        else { resumes++; ctl.play(engine.presentedK * R.den / R.num) }
        continue
      }
      if (t >= nextCheck) {
        // paused bar check at a random frame anywhere (cold spans included)
        nextCheck = t + checkEvery
        const back = engine.presentedK
        ctl.pause()
        await sleep(50)
        const k = Math.floor(rnd() * engine.program!.total)
        const ts = now()
        engine.seek(k)
        const ms = await shown(r, k, 10000, ts)
        checks.n++
        if (ms >= 0) { checks.ok++; checks.ms.push(ms) } else if (checks.bad.length < 10) checks.bad.push(diag(r, k))
        ctl.play(back * R.den / R.num)
        nextEdit = Math.max(nextEdit, now() + 1000)
        continue
      }
      if (t >= nextEdit) {
        nextEdit = t + editEvery
        const kind = ROTATION[rot++ % ROTATION.length]
        const op = editNear(pm, kind, engine.presentedK, Math.round(2 * R.num / R.den))
        if (!op) { edits.skipped++; continue }
        const e = await edit(r, op[0], op[1])
        if (!e) { edits.refused++; continue }
        edits.applied++
        edits.byKind[kind] = (edits.byKind[kind] ?? 0) + 1
        if (edits.ms.length < 2000) edits.ms.push(e.ms)
      }
    }
    ctl.pause()
    await sleep(300)
    const last = sample(r, t0)
    peaks.add(last)
    rows.push(last)
    return {
      minutes, startedAtEpochMs, elapsedS: +((now() - t0) / 1000).toFixed(1), loops, resumes, buffering, extPauses,
      edits: { ...edits, ms: { p50: percentile(edits.ms, 0.5), p95: percentile(edits.ms, 0.95), max: Math.max(0, ...edits.ms) } },
      refusals,
      checks: { n: checks.n, ok: checks.ok, bad: checks.bad, p95: percentile(checks.ms, 0.95), max: Math.max(0, ...checks.ms) },
      judge: { drawn: r.judge.drawn, judged: r.judge.judged, skipped: r.judge.skipped, bad: r.judge.bad, badSamples: r.judge.badSamples },
      peaks, rows, mediaCreated: media.created, unhandled, engine: engineSummary(r),
      caps: { spanBytes: engine.internals.store.maxBytes, audioBytes: r.audio?.chunks.capBytes ?? null },
      R, fps: R.num / R.den,
    }
  },

  /** Play to the end, then play again (the soak's loop, and the store's
   *  Space at the end: `ctl.play(playhead)` with the playhead on the last
   *  frame, or `ctl.play(0)`): playback must restart from frame 0 and run,
   *  not stop at once on a frame of the OLD position. */
  async end_restart(cfg) {
    const r = await rig(cfg, true)
    const { engine, ctl } = r
    const pm0 = engine.program!
    const R = pm0.R
    const secs = (k: number) => (k * R.den) / R.num
    const rounds: Array<Record<string, unknown>> = []
    const stops: Array<{ at: number; k: number }> = []
    engine.on('status', (st) => { if (!st.playing && stops.length < 2000) stops.push({ at: now(), k: st.presentedK }) })
    // the environment: another window over the 4 px harness window hides the
    // page (WebKit then pauses the muted element and stops rendering, so
    // timers and rVFC stall). A round it touched proves nothing either way.
    let envEvents = 0
    const envLog: string[] = []
    const noteEnv = (what: string) => { envEvents++; if (envLog.length < 40) envLog.push(`${what}@${now().toFixed(0)}`) }
    document.addEventListener('visibilitychange', () => noteEnv(`vis:${document.visibilityState}`))
    // An 'element' pause of an element that ran off the media end is the
    // engine's own doing (final sweep 4: played from the last frame while
    // its run seek waited), never the environment's: it counts as a stop.
    let endedPauses = 0
    const v0 = engine.internals.video!
    engine.on('pause-external', (e) => {
      const dur = engine.internals.lane?.mediaSource?.duration ?? Infinity
      if (e.cause === 'element' && (v0.ended || v0.currentTime >= dur - 0.05)) endedPauses++
      else noteEnv(`ext:${e.cause}`)
    })
    const want = cfg.edits ?? 10
    let clean = 0
    // The element's own story of each round (a stuck round must say what
    // WebKit did with the element: every event with where it stood, every
    // SourceBuffer update with what it held, the engine's frames).
    const v = engine.internals.video!
    let roundT = now()
    const ev: Array<Record<string, unknown>> = []
    const rangesOf = (tr: TimeRanges | undefined) =>
      tr ? Array.from({ length: tr.length }, (_, j) => [+tr.start(j).toFixed(3), +tr.end(j).toFixed(3)]) : null
    const note = (e: string, extra: Record<string, unknown> = {}) => {
      if (ev.length >= 400) ev.shift()
      const sb = engine.internals.lane?.sourceBuffer as SourceBuffer | null | undefined
      let sbr: unknown
      try { sbr = rangesOf(sb?.buffered) } catch { sbr = 'n/a' }
      ev.push({ ms: +(now() - roundT).toFixed(1), e, t: +v.currentTime.toFixed(4), rs: v.readyState, sk: v.seeking, p: v.paused,
        dur: +((engine.internals.lane?.mediaSource?.duration ?? NaN)).toFixed(3), vb: rangesOf(v.buffered), sb: sbr, ...extra })
    }
    for (const e of ['seeking', 'seeked', 'waiting', 'playing', 'play', 'pause', 'ended', 'stalled', 'durationchange',
      'loadeddata', 'canplay', 'canplaythrough', 'suspend', 'emptied', 'error']) v.addEventListener(e, () => note(e))
    let sbHooked: SourceBuffer | null = null
    const hookSb = () => {
      const sb = engine.internals.lane?.sourceBuffer as SourceBuffer | null | undefined
      if (!sb || sb === sbHooked) return
      sbHooked = sb
      sb.addEventListener('updateend', () => note('sb:updateend', { lane: engine.internals.lane?.buffered }))
      sb.addEventListener('abort', () => note('sb:abort'))
      sb.addEventListener('error', () => note('sb:error'))
    }
    engine.on('frame', (f) => note(`frame:${f.k}`, { playing: f.playing, drawn: f.drawn }))
    engine.on('buffering', (b) => note(`buffering:${b.buffering}`, { k: b.k }))
    engine.on('status', (st) => note(`status`, { playing: st.playing, buffering: st.buffering, k: st.presentedK }))
    for (let i = 0; clean < want && i < want + 6; i++) {
      const env0 = envEvents
      const pm = engine.program!
      hookSb()
      ctl.play(secs(pm.total - 45))
      const t0 = now()
      while (now() - t0 < 8000 && (engine.playing || engine.presentedK < pm.total - 3)) {
        if (!engine.playing) ctl.play(secs(engine.presentedK))
        await sleep(20)
      }
      const endK = engine.presentedK
      const reachedEnd = endK >= pm.total - 3
      // the user's pause at the end (final sweep 4: WebKit's 'ended' on the
      // parked element played again from the last frame, see RunSeek)
      if (cfg.pauseBeforePlayMs) await sleep(cfg.pauseBeforePlayMs)
      const how = clean % 2 === 0 ? 'fromEnd' : 'fromZero'
      const stopsBefore = stops.length
      // keep the last 40 events before play (the stop at the end, the park seek)
      ev.splice(0, Math.max(0, ev.length - 40))
      const t1 = now()
      const prevT = roundT
      roundT = t1
      for (const x of ev) x.ms = +((x.ms as number) - (t1 - prevT)).toFixed(1)
      const seek0 = engine.internals.seekCounters()
      const stall0 = engine.stats.stallRestarts
      const ended0 = endedPauses
      const deferred0 = engine.internals.runSeekStats.deferred
      note('play', { how, endK, seek: seek0, elementAt: v.currentTime })
      ctl.play(how === 'fromEnd' ? secs(endK) : 0)
      if (cfg.busyAfterPlayMs) { const s = now(); while (now() - s < cfg.busyAfterPlayMs) { /* the app's render work */ } }
      note('played', { seek: engine.internals.seekCounters(), target: engine.targetK, lane: engine.internals.lane?.buffered })
      // a trace of the element for the round's report (a stuck round must
      // say where the element stood, and whether frames kept coming)
      const trace: Array<Record<string, unknown>> = []
      const drawn0 = engine.stats.framesDrawn
      const held0 = engine.stats.heldFrames
      while (now() - t1 < 2500) {
        await sleep(100)
        const vb = v.buffered
        trace.push({ ms: +(now() - t1).toFixed(0), t: +v.currentTime.toFixed(3), rs: v.readyState, p: v.paused, s: v.seeking,
          k: engine.presentedK, drawn: engine.stats.framesDrawn - drawn0, held: engine.stats.heldFrames - held0,
          dur: +((engine.internals.lane?.mediaSource?.duration ?? NaN)).toFixed(3),
          vb: Array.from({ length: vb.length }, (_, j) => [+vb.start(j).toFixed(2), +vb.end(j).toFixed(2)]) })
      }
      const waitedMs = now() - t1
      const k = engine.presentedK
      const lane = engine.internals.lane!
      const env = envEvents - env0
      const ok = reachedEnd && engine.playing && k >= 20 && k < 200
      // a round the watchdog rescued (a stop after play, a stall restart) is
      // reported like a failed one: it is the same defect, caught in time
      const stallRestarts = engine.stats.stallRestarts - stall0
      const suspicious = !ok || stops.length > stopsBefore || stallRestarts > 0 || !!cfg.traceAll
      const why = !suspicious ? null : {
        buffered: lane.buffered, sb: bufferedOf(r), target: engine.targetK, status: { ...engine.status, ranges: 0 },
        video: { t: +v.currentTime.toFixed(3), paused: v.paused, seeking: v.seeking, readyState: v.readyState },
        laneStats: lane.stats, store: { pending: engine.internals.store.pending, bytes: engine.internals.store.bytes },
        ring: r.ring.slice(-3).map((d) => ({ ...d, bar: fmt(d.bar) })), envLog: envLog.slice(-6),
        stops: stops.slice(-4).map((x) => ({ ms: +(x.at - t0).toFixed(0), k: x.k })),
        engineStats: { ...engine.stats }, seek: engine.internals.seekCounters(), trace, events: ev.slice(),
      }
      ev.length = 0
      if (env === 0) clean++
      rounds.push({ how, endK, reachedEnd, total: pm.total, playing: engine.playing, k, env, waitedMs: +waitedMs.toFixed(0), why,
        stopsAfterPlay: stops.length - stopsBefore, stallRestarts, endedPauses: endedPauses - ended0,
        // run seeks that waited for the start's append (RunSeek): the start
        // is never buffered when the end is reached, so every round defers
        deferred: engine.internals.runSeekStats.deferred - deferred0,
        firstStop: stops[stopsBefore] ? { ms: +(stops[stopsBefore].at - t1).toFixed(0), k: stops[stopsBefore].k } : null, ok })
      ctl.pause()
      await sleep(200)
    }
    const judged = rounds.filter((x) => x.env === 0)
    return {
      rounds, clean: judged.length, failed: judged.filter((x) => !x.ok).length, envRounds: rounds.length - judged.length,
      judge: { judged: r.judge.judged, bad: r.judge.bad, badSamples: r.judge.badSamples },
    }
  },

  /** Play from a paused frame, many times (the soak's start, the store's
   *  Space): every frame drawn while playing is judged, and the first
   *  frames of each run are where a picture of the PREVIOUS frame under the
   *  new k would show (the soak saw k = 2 drawn with source frame 1). */
  async play_start(cfg) {
    const r = await rig(cfg, true)
    const { engine, ctl } = r
    const rnd = mulberry32(cfg.seed ?? 11)
    const R = engine.program!.R
    const rounds = cfg.edits ?? 40
    const bad0 = r.judge.bad
    const perRound: Array<Record<string, unknown>> = []
    for (let i = 0; i < rounds; i++) {
      const pm = engine.program!
      const k0 = i % 2 === 0 ? 0 : Math.floor(rnd() * (pm.total - 200))
      // the first round plays the moment the first frame is up, as the soak
      // does (laneA is still filling the look-ahead around it)
      const ms = i === 0 && engine.presentedK === 0 ? 0 : (engine.seek(k0), await shown(r, k0, 10000))
      const before = r.judge.bad
      ctl.play((k0 * R.den) / R.num)
      await sleep(1200)
      ctl.pause()
      await sleep(150)
      perRound.push({ k0, shownMs: +ms.toFixed(0), bad: r.judge.bad - before })
    }
    return {
      rounds: perRound, bad: r.judge.bad - bad0,
      judge: { drawn: r.judge.drawn, judged: r.judge.judged, bad: r.judge.bad, badSamples: r.judge.badSamples },
      engine: engineSummary(r),
    }
  },

  /** P1-E1 through the app path: 100 dispatch edits, the media elements
   *  created and live counted all along. */
  async edit_script(cfg) {
    const r = await rig(cfg, true)
    const { engine, ctl } = r
    const rnd = mulberry32(cfg.seed ?? 7)
    let maxLive = liveMedia()
    const poll = setInterval(() => { maxLive = Math.max(maxLive, liveMedia()) }, 20)
    const n = cfg.edits ?? 100
    let applied = 0
    const byKind: Record<string, number> = {}
    let playingEdits = 0
    for (let i = 0; i < n; i++) {
      const pm = engine.program!
      if (i === 30 || i === 70) ctl.play(0)
      if (i === 55 || i === 95) ctl.pause()
      if (i % 10 === 5 && !engine.playing) engine.seek(Math.floor(rnd() * pm.total))
      const kind = ROTATION[i % ROTATION.length]
      const k0 = engine.playing ? engine.presentedK : Math.floor(rnd() * Math.max(1, pm.total - 90))
      const op = editNear(pm, kind, k0, 15) ?? editNear(pm, kind, 0, 0) ?? ['undo', {}]
      if (engine.playing) playingEdits++
      const e = await edit(r, op[0], op[1])
      if (e) { applied++; byKind[kind] = (byKind[kind] ?? 0) + 1 }
      maxLive = Math.max(maxLive, liveMedia())
      await sleep(40)
    }
    ctl.pause()
    await sleep(300)
    clearInterval(poll)
    maxLive = Math.max(maxLive, liveMedia())
    return {
      n, applied, byKind, playingEdits, refusals, created: media.created, maxLive,
      engineCreated: engine.stats.mediaElementsCreated, judge: { judged: r.judge.judged, bad: r.judge.bad, badSamples: r.judge.badSamples },
      unhandled,
    }
  },
}

// ------------------------------------------------------------------ run

;(async () => {
  const post = (body: Result) => fetch(`${mailbox}/__result/${token}`, { method: 'POST', mode: 'no-cors', body: JSON.stringify(body) })
  const result: Result = { scenario, ua: navigator.userAgent }
  try {
    const cfg = await (await fetch(`/wk/${q.get('cfg') ?? 'config'}.json`)).json() as Config
    const run = scenarios[scenario]
    if (!run) throw new Error(`unknown scenario ${JSON.stringify(scenario)}`)
    Object.assign(result, await run(cfg))
  } catch (e) {
    const err = e as Error
    result.fatal = `${err?.name ?? 'Error'}: ${err?.message ?? String(e)}\n${err?.stack ?? ''}`
  }
  ;(window as unknown as { __result: Result }).__result = result
  await post(result)
})()
