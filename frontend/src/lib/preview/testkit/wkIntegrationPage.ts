// Instant preview 1c INTEGRATION page (tests/wk/test_wk_integration.py):
// the app's client mode end to end, served same-origin next to a REAL
// FastAPI backend (uvicorn thread, fixture sources, real proxies, real
// /frame_map, real preview renders and bakes). Every edit takes the path the
// store takes in client mode — POST /dispatch?include=edl, then
// PreviewController.applyTimeline(edl, render_hash) — and every assertion
// reads the ENGINE CANVAS (bar code / level, inside onDrawn) or the sound
// the real AudioEngine renders (an AudioWorklet tap on its output).
//
// Scenarios (spec §13): edit_latency (P1-F3), edit_while_playing (P1-F4),
// structural (P1-S1), av_sync (P1-A3), bake_splice (P1-B1), external_pause
// (the CTX requirement: WebKit's own pauses, window hidden and occluded).

import { PreviewController } from '../previewController'
import type { ClientPreviewEngine } from '../engine'
import { AudioEngine } from '../audio/audioEngine'
import { DisplayTimeBase } from '../clock/presentedClock'
import type { EdlLike } from '../timeline/framePlan'
import { KIND_GAP, type ProgramMap } from '../timeline/programMap'
import { MODE_BAKED } from '../timeline/support'
import type { DivergenceEvent } from '../verify/divergence'
import { expectedBar, meanGreen, percentile, readBar } from './canvasProbe'
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
  edits?: number
  seed?: number
  /** flash sources: every `flashEvery`-th source frame is white, with a click */
  flashEvery?: number
}

interface Draw { k: number; bar: number; level: number; playing: boolean; at: number }

interface Rig {
  cfg: Config
  ctl: PreviewController
  engine: ClientPreviewEngine
  srcIds: Map<string, number>
  draws: Draw[]
  canvasEdl: { w: number; h: number }
  playingLog: Array<{ at: number; playing: boolean }>
  /** where meanGreen reads (EDL canvas px) */
  patch: { x: number; y: number; w: number; h: number } | null
  /** every structural-check verdict, in order (the controller's telemetry) */
  verdicts: DivergenceEvent[]
}

const api = (cfg: Config) => `/api/sessions/${cfg.sid}`

async function window_(cmd: 'hide' | 'show' | 'occlude' | 'reveal'): Promise<void> {
  await fetch(`${mailbox}/__window/${token}/${cmd}`, { method: 'POST', mode: 'no-cors' })
}

const fmt = (c: number) => (c < 0 ? String(c) : `${barSrc(c)}:${barFrame(c)}`)

// ------------------------------------------------------------------ rig

async function rig(cfg: Config, opts: { audio?: false | ((onInt: () => void) => AudioEngine); patch?: Rig['patch'] } = {}): Promise<Rig> {
  const host = document.createElement('div')
  host.style.cssText = `position:relative;width:${cfg.canvas[0]}px;height:${cfg.canvas[1]}px;`
  document.body.appendChild(host)
  const playingLog: Rig['playingLog'] = []
  const verdicts: DivergenceEvent[] = []
  const ctl = new PreviewController({
    sessionId: cfg.sid, audio: opts.audio ?? false,
    engineOptions: { canvasSize: { w: cfg.canvas[0], h: cfg.canvas[1] } },
    onPlaying: (p) => playingLog.push({ at: now(), playing: p }),
    telemetry: (e) => verdicts.push(e),
  })
  const engine = ctl.attach(host)
  if (!engine || engine.status.mode !== 'client') throw new Error(`engine did not start: ${JSON.stringify(engine?.status)}`)
  const r: Rig = {
    cfg, ctl, engine, srcIds: new Map(Object.entries(cfg.srcIds)), draws: [], canvasEdl: { w: 1, h: 1 },
    playingLog, patch: opts.patch ?? null, verdicts,
  }
  engine.internals.compositor!.onDrawn = (info, gl) => {
    const pm = engine.program
    if (!pm) return
    const bar = info.black ? NO_PICTURE : readBar(gl, pm, info.k, r.canvasEdl, (src) => ctl.lookup(src)?.info ?? null)
    const level = r.patch ? meanGreen(gl, r.canvasEdl, r.patch) : -1
    r.draws.push({ k: info.k, bar, level, playing: engine.playing, at: now() })
    if (r.draws.length > 20000) r.draws.splice(0, 10000)
  }
  const edl = await (await fetch(`${api(cfg)}/edl`)).json() as EdlLike
  const c = edl.canvas as { w: number; h: number }
  r.canvasEdl = { w: c.w, h: c.h }
  ctl.applyTimeline(edl, null)
  // the hash (get_timeline include=edl), the sources, the first picture
  for (let i = 0; i < 400 && !(ctl.renderHash && engine.program && sourcesKnown(r)); i++) await sleep(25)
  if (!ctl.renderHash) throw new Error('no render hash')
  if (await shown(r, 0, 20000, 0) < 0) throw new Error(`the first frame never showed: ${diag(r, 0)}`)
  return r
}

function sourcesKnown(r: Rig): boolean {
  const pm = r.engine.program
  return !!pm && pm.sources.every((s) => r.ctl.lookup(s)?.proxy?.key)
}

/** Resolves (ms since `since`) once frame k is DRAWN PAUSED with the bar
 *  the current program expects there; -1 on timeout. `since`: the moment
 *  the seek or edit was issued — the engine may draw synchronously inside
 *  it (the texture already holds the frame: a duplicate, a split). */
async function shown(r: Rig, k: number, ms = 5000, since = now()): Promise<number> {
  const t0 = now()
  let from = r.draws.length
  for (let i = r.draws.length - 1; i >= 0 && r.draws[i].at >= since; i--) from = i
  while (now() - t0 < ms) {
    const pm = r.engine.program
    if (pm) {
      const exp = expectedBar(pm, k, r.srcIds)
      for (let i = from; i < r.draws.length; i++) {
        const d = r.draws[i]
        if (d.k === k && !d.playing && d.bar === exp && d.at >= since) return d.at - since
      }
    }
    await sleep(2)
  }
  return -1
}

/** Why frame k is not on the canvas (a failure message). */
function diag(r: Rig, k: number): string {
  const pm = r.engine.program!
  const draws = r.draws.filter((d) => d.k === k).slice(-3).map((d) => ({ ...d, bar: fmt(d.bar) }))
  const clip = pm.clips[pm.clip[k]] as unknown as V1Clip | undefined
  return JSON.stringify({
    k, exp: fmt(expectedBar(pm, k, r.srcIds)), draws, target: r.engine.targetK, presented: r.engine.presentedK,
    status: { ...r.engine.status, ranges: r.engine.status.ranges.length }, clip, srcFrame: pm.srcFrame[k],
    lane: r.engine.internals.lane?.stats, buffered: r.engine.internals.lane?.buffered,
    info: clip ? r.ctl.lookup(clip.src) : null, lastDraw: r.draws.at(-1),
  })
}

async function waitWindowFilled(r: Rig, ms = 20000): Promise<void> {
  const pm = r.engine.program!
  const lane = r.engine.internals.lane!
  const want = Math.min(pm.total, Math.round(28 * pm.R.num / pm.R.den))
  const t0 = now()
  while (now() - t0 < ms && lane.buffered[1] < want) await sleep(25)
}

interface Edited {
  t0: number
  tAnswer: number
  hash: string
  edl: EdlLike
  /** presentedK when the answer reached the engine */
  peAnswer: number
  /** the program the answer built */
  pm: ProgramMap
}

/** Edits the server refused (tool → first reason), for the report. */
const refusals: Record<string, string> = {}

/** The store's client-mode dispatch: include=edl, then applyTimeline. */
async function edit(r: Rig, tool: string, args: Record<string, unknown>): Promise<Edited | null> {
  const send = () => fetch(`${api(r.cfg)}/dispatch?include=edl`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ tool, args }),
  })
  let t0 = now()
  let res = await send()
  // the per-path rate bucket (60 rps): a scripted loop can outrun it; the
  // latency of an edit is timed from the send that was answered
  for (let i = 0; res.status === 429 && i < 50; i++) {
    const ra = Number.parseFloat(res.headers.get('Retry-After') ?? '')
    await sleep(Number.isFinite(ra) && ra > 0 ? Math.min(2000, ra * 1000) : 100)
    t0 = now()
    res = await send()
  }
  if (!res.ok) {
    refusals[tool] ??= `${res.status} ${(await res.text()).slice(0, 200)}`
    return null
  }
  const body = await res.json() as { edl?: EdlLike; render_hash: string }
  if (!body.edl) throw new Error(`${tool}: answer without an EDL`)
  const tAnswer = now()
  const peAnswer = r.engine.presentedK
  r.ctl.applyTimeline(body.edl, body.render_hash)
  return { t0, tAnswer, hash: body.render_hash, edl: body.edl, peAnswer, pm: r.engine.program! }
}

interface V1Clip { id: string; src: string; start: number; in: number; out: number }
const v1 = (edl: EdlLike): V1Clip[] => [...((edl.tracks ?? []).find((t) => t.id === 'v1')?.clips ?? [])]
  .map((c) => c as unknown as V1Clip).sort((a, b) => a.start - b.start)

/** Where a reorder drops clip `ci`: halfway into the next clip, or 0. */
function moveTarget(pm: ProgramMap, ci: number): number {
  const spans = clipFrames(pm)
  const i = spans.findIndex((sp) => sp.ci === ci)
  const next = spans[i + 1]
  if (!next) return 0
  return +(((next.k0 + next.k1) / 2) * pm.R.den / pm.R.num).toFixed(3)
}

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

// ------------------------------------------------------------- scenarios

const scenarios: Record<string, (cfg: Config) => Promise<Result>> = {
  /** P1-F3: paused edits, dispatch start → correct bar on the canvas. */
  async edit_latency(cfg) {
    const r = await rig(cfg)
    await waitWindowFilled(r)
    const rnd = mulberry32(cfg.seed ?? 7)
    const kinds = ['split', 'trim', 'move', 'delete', 'ripple'] as const
    const per = cfg.edits ?? 50
    const rows: Array<{ kind: string; ms: number; changed: boolean; k: number; exp: string; got: string }> = []
    const done: Record<string, number> = {}
    let refused = 0
    for (let i = 0; kinds.some((kd) => (done[kd] ?? 0) < per) && i < per * kinds.length * 3; i++) {
      const kind = kinds[i % kinds.length]
      if ((done[kind] ?? 0) >= per) continue
      const pm = r.engine.program!
      const spans = clipFrames(pm).filter((sp) => sp.k1 - sp.k0 >= 12)
      const s = spans[Math.floor(rnd() * spans.length)]
      const k = s.k0 + 3 + Math.floor(rnd() * (s.k1 - s.k0 - 6))
      const ts = now()
      r.engine.seek(k)
      if (await shown(r, k, 8000, ts) < 0) {
        throw new Error(`seek to ${k} never showed: ${diag(r, k)}`)
      }
      await sleep(30)
      const c = pm.clips[s.ci] as unknown as V1Clip
      const t = (k * pm.R.den) / pm.R.num
      let before = expectedBar(pm, k, r.srcIds)
      const op: [string, Record<string, unknown>] =
        kind === 'split' ? ['split_at', { track: 'v1', time: +(t).toFixed(4) }]
        : kind === 'trim' ? ['trim_clip', { clip_id: c.id, in: +(c.in + 0.3).toFixed(3) }]
        // a main-lane move is a reorder (the Timeline drag: close_gap): the
        // clip under the playhead goes after its right neighbour, or to the
        // front when it is the last
        : kind === 'move' ? ['move_clip', { clip_id: c.id, close_gap: true, new_start: moveTarget(pm, s.ci) }]
        : kind === 'delete' ? ['bulk_delete', { clip_ids: [c.id] }]
        : ['ripple_delete', { clip_id: c.id }]
      for (const [name, tool, args] of [[kind, ...op], ['undo', 'undo', {}]] as Array<[string, string, Record<string, unknown>]>) {
        const e = await edit(r, tool, args)
        if (!e) {
          refused++
          break
        }
        const kk = r.engine.targetK
        const ms = await shown(r, kk, 8000, e.t0 - 0.001)
        const pm2 = r.engine.program!
        const exp = expectedBar(pm2, kk, r.srcIds)
        const got = [...r.draws].reverse().find((d) => d.k === kk && !d.playing)?.bar ?? -9
        rows.push({ kind: name, ms: ms < 0 ? -1 : +(ms).toFixed(1), changed: exp !== before, k: kk, exp: fmt(exp), got: fmt(got) })
        before = exp          // the undo is judged against the edited frame
        if (name !== 'undo') done[name] = (done[name] ?? 0) + 1
      }
    }
    const by: Record<string, { n: number; p50: number; p95: number; max: number; changed: number; failed: number }> = {}
    for (const kind of [...kinds, 'undo']) {
      const xs = rows.filter((x) => x.kind === kind)
      const ms = xs.filter((x) => x.ms >= 0).map((x) => x.ms)
      by[kind] = { n: xs.length, p50: percentile(ms, 0.5), p95: percentile(ms, 0.95), max: Math.max(0, ...ms),
        changed: xs.filter((x) => x.changed).length, failed: xs.filter((x) => x.ms < 0 || x.exp !== x.got).length }
    }
    return { by, bad: rows.filter((x) => x.ms < 0 || x.exp !== x.got).slice(0, 10), total: rows.length, refused, refusals,
      divergence: r.ctl.divergence(), engineStats: r.engine.stats }
  },

  /** Every output frame by a paused seek: the bar the program names. */
  async sweep(cfg) {
    const r = await rig(cfg)
    await waitWindowFilled(r)
    const pm = r.engine.program!
    const bad: string[] = []
    const order = Array.from({ length: pm.total }, (_, k) => k)
    if (q.get('shuffle') === '1') {
      const rnd = mulberry32(3)
      for (let i = order.length - 1; i > 0; i--) { const j = Math.floor(rnd() * (i + 1)); [order[i], order[j]] = [order[j], order[i]] }
    }
    for (const k of order) {
      const ts = now()
      r.engine.seek(k)
      if (await shown(r, k, 3000, ts) < 0) bad.push(diag(r, k))
    }
    return { total: pm.total, bad: bad.slice(0, 12), nBad: bad.length, lane: r.engine.internals.lane?.stats }
  },

  /** P1-F4: edits while playing. Every frame presented after an edit's
   *  answer reached the engine, at or after presentedK + 5, must show the
   *  NEW program's frame; and playback never stalls. */
  async edit_while_playing(cfg) {
    const r = await rig(cfg)
    await waitWindowFilled(r)
    const video = r.engine.internals.video!
    let waiting = 0
    video.addEventListener('waiting', () => { waiting++ })
    const edits: Array<Edited & { variant: string; pe0: number; region: number }> = []
    const extPauses: Result[] = []
    r.engine.on('pause-external', (e) => extPauses.push({ ...e, at: now() }))
    r.engine.play()
    await sleep(1200)
    let stopReason = 'done'
    for (let i = 0; i < (cfg.edits ?? 8); i++) {
      if (!r.engine.playing) { stopReason = `not playing at k=${r.engine.presentedK}`; break }
      const pm = r.engine.program!
      const pe0 = r.engine.presentedK
      const clips = clipFrames(pm)
      const variant = i % 2 === 0 ? 'ahead30' : 'underPlayhead'
      let op: [string, Record<string, unknown>]
      let region: number
      if (variant === 'ahead30') {
        // the spec's case: an edit at presentedK + 30 (the next cut after it)
        const next = clips.find((sp) => sp.k0 >= pe0 + 30)
        if (!next) { stopReason = `no clip 30 frames past ${pe0}`; break }
        op = ['ripple_delete', { clip_id: (pm.clips[next.ci] as unknown as V1Clip).id }]
        region = next.k0
      } else {
        // stricter: the clip under the playhead changes from its start on
        const cur = clips.find((sp) => pe0 >= sp.k0 && pe0 < sp.k1)
        if (!cur) { stopReason = `no clip under ${pe0}`; break }
        const c = pm.clips[cur.ci] as unknown as V1Clip
        op = ['trim_clip', { clip_id: c.id, in: +(c.in + 0.25).toFixed(3) }]
        region = cur.k0
      }
      const e = await edit(r, op[0], op[1])
      if (!e) continue
      edits.push({ ...e, variant, pe0, region })
      // watch the edited region play (15 frames past it), then the next edit
      const until = Math.max(region, e.peAnswer + 5) + 15
      const tw = now()
      while (r.engine.playing && r.engine.presentedK < until && now() - tw < 4000) await sleep(20)
      await sleep(200)
    }
    await sleep(400)
    const stillPlaying = r.engine.playing
    r.engine.pause()
    const results = edits.map((e, i) => {
      const until = i + 1 < edits.length ? edits[i + 1].tAnswer : Infinity
      const seen = r.draws.filter((d) => d.playing && d.at > e.tAnswer && d.at < until)
      const judged = seen.filter((d) => d.k >= e.peAnswer + 5)
      const bad = judged.filter((d) => d.bar !== expectedBar(e.pm, d.k, r.srcIds))
        .map((d) => ({ k: d.k, exp: fmt(expectedBar(e.pm, d.k, r.srcIds)), got: fmt(d.bar), afterMs: +(d.at - e.tAnswer).toFixed(1) }))
      const changed = judged.filter((d) => d.k >= e.region).length
      return { variant: e.variant, pe0: e.pe0, peAnswer: e.peAnswer, region: e.region,
        answerMs: +(e.tAnswer - e.t0).toFixed(1), judged: judged.length, changedJudged: changed, bad }
    })
    return { results, waiting, stillPlaying, drawsPlaying: r.draws.filter((d) => d.playing).length, stopReason, extPauses,
      total: r.engine.program?.total }
  },

  /** P1-S1: 200 committed edits; the ENGINE's map equals /frame_map each time. */
  async structural(cfg) {
    const r = await rig(cfg)
    const rnd = mulberry32(cfg.seed ?? 20260926)
    const srcs = Object.keys(cfg.srcIds)
    const byTool: Record<string, number> = {}
    const rejected: Record<string, number> = {}
    const outcomes: Record<string, number> = {}
    const noVerdict: string[] = []
    const checkMs: number[] = []
    let applied = 0
    let attempts = 0
    let clipsMax = 0
    let framesMax = 0
    while (applied < (cfg.edits ?? 200) && attempts < (cfg.edits ?? 200) * 4) {
      attempts++
      const edl = r.ctl.timeline!
      const op = pickEdit(edl, rnd, srcs)
      const since = r.verdicts.length
      const e = await edit(r, op.tool, op.args)
      if (!e) { rejected[op.tool] = (rejected[op.tool] ?? 0) + 1; continue }
      applied++
      byTool[op.tool] = (byTool[op.tool] ?? 0) + 1
      const t0 = now()
      // THIS edit's verdict (an undo can bring back a hash checked before)
      const verdict = () => r.verdicts.slice(since).find((v) => v.render_hash === e.hash && v.type !== 'stale')
      while (!verdict() && now() - t0 < 8000) await sleep(5)
      checkMs.push(now() - t0)
      const last = verdict()
      if (!last) { noVerdict.push(`${op.tool}@${e.hash}`); continue }
      outcomes[last.type] = (outcomes[last.type] ?? 0) + 1
      const pm = r.engine.program!
      clipsMax = Math.max(clipsMax, pm.clips.length)
      framesMax = Math.max(framesMax, pm.total)
    }
    const d = r.ctl.divergence()
    return { applied, byTool, rejected, outcomes, noVerdict: noVerdict.slice(0, 10), noVerdictCount: noVerdict.length,
      checkMs: { p50: percentile(checkMs, 0.5), p95: percentile(checkMs, 0.95), max: Math.max(0, ...checkMs) },
      mismatches: r.verdicts.filter((x) => x.type === 'mismatch').slice(0, 5), checked: d.checked, mismatched: d.mismatched,
      clipsMax, framesMax, controllerStats: r.ctl.stats }
  },

  /** P1-B1: a graded clip is BAKED; after the render lands its bake spans
   *  replace the RAW frames there, in the same SourceBuffer. */
  async bake_splice(cfg) {
    const r = await rig(cfg, { patch: { x: 100, y: 200, w: 300, h: 100 } })
    await waitWindowFilled(r)
    const video = r.engine.internals.video!
    const srcAtStart = video.src
    let emptied = 0
    let loadstarts = 0
    video.addEventListener('emptied', () => { emptied++ })
    video.addEventListener('loadstart', () => { loadstarts++ })
    const pm = r.engine.program!
    const baked = r.engine.status.ranges.filter((x) => x.mode === MODE_BAKED).map((x) => [x.k0, x.k1])
    const readAll = async () => {
      const out: Array<{ k: number; bar: number; level: number; baked: boolean }> = []
      for (let k = 0; k < pm.total; k++) {
        const ts = now()
        r.engine.seek(k)
        if (await shown(r, k, 8000, ts) < 0) throw new Error(`frame ${k} never showed: ${diag(r, k)}`)
        const d = [...r.draws].reverse().find((x) => x.k === k && !x.playing)!
        out.push({ k, bar: d.bar, level: +d.level.toFixed(2), baked: r.engine.isBakedFrame(k) })
      }
      return out
    }
    const before = await readAll()
    const inits0 = r.engine.internals.lane!.stats
    const initsBefore = (inits0 as unknown as { inits?: number }).inits ?? null
    const t0 = now()
    const pr = await fetch(`${api(cfg)}/preview?priority=low`, { method: 'POST' })
    const body = await pr.json() as { edl_hash: string }
    const renderMs = now() - t0
    r.ctl.onPreviewLanded(body.edl_hash)
    const t1 = now()
    while (now() - t1 < 60000 && r.engine.bakeState.waiting > 0) await sleep(25)
    const spliceMs = now() - t1
    const after = await readAll()
    return {
      total: pm.total, baked, previewHash: body.edl_hash, renderHash: r.ctl.renderHash, renderMs, spliceMs,
      bakeState: r.engine.bakeState, expected: Array.from({ length: pm.total }, (_, k) => expectedBar(pm, k, r.srcIds)),
      before, after, srcUnchanged: video.src === srcAtStart, emptied, loadstarts,
      laneStats: r.engine.internals.lane!.stats, initsBefore, controllerStats: r.ctl.stats,
      seekedEarly: r.engine.stats.seekedEarly, seeks: r.engine.stats.seeks,
    }
  },

  /** §11.1 "audio audible (playing) ≤ 250 ms": tone clips A (440 Hz) and B
   *  (1320 Hz) alternate; while playing, the A clip under the playhead is
   *  ripple-deleted, so B slides under it. From the commit (dispatch start)
   *  to the first HEARD 5 ms window where B's tone dominates A's. */
  async audible_edit(cfg) {
    const a = await audioRig(cfg)
    const { r, ctx, blocks } = a
    await waitWindowFilled(r)
    await sleep(300)
    const src = (c: V1Clip) => cfg.srcIds[c.src] ?? 0
    r.ctl.play(0)
    const trials: Result[] = []
    const sr = ctx.sampleRate
    const win = Math.round(0.005 * sr)
    const goertzel = (x: Float32Array, f: number) => {
      const w = (2 * Math.PI * f) / sr
      const c = 2 * Math.cos(w)
      let s1 = 0
      let s2 = 0
      for (let i = 0; i < x.length; i++) { const s0 = x[i] + c * s1 - s2; s2 = s1; s1 = s0 }
      return s1 * s1 + s2 * s2 - c * s1 * s2
    }
    const t00 = now()
    const events: Result[] = []
    r.engine.on('pause-external', (e) => events.push({ at: +(now() - t00).toFixed(0), ...e, vis: document.visibilityState }))
    r.engine.on('buffering', (e) => events.push({ at: +(now() - t00).toFixed(0), ev: 'buffering', ...e }))
    let restarts = 0
    while (trials.length < (cfg.edits ?? 8) && now() - t00 < 90000) {
      if (!r.engine.playing) {
        // a pause this page did not make (the window hidden by another run
        // on a busy machine): play on; a trial is only ever judged playing
        if (restarts++ > 5) { trials.push({ stopped: r.engine.presentedK }); break }
        await sleep(400)
        if (!r.engine.playing) r.ctl.play()
        await sleep(600)
        continue
      }
      const pm = r.engine.program!
      const fps = pm.R.num / pm.R.den
      const k = r.engine.presentedK
      const clips = v1(r.ctl.timeline!)
      const tNow = k / fps
      const under = clips.find((c) => c.start <= tNow && tNow < c.start + (c.out - c.in))
      // an A clip 0.4 s in, with ≥ 0.8 s of it left
      if (!under || src(under) !== 1 || tNow - under.start < 0.4 || under.start + (under.out - under.in) - tNow < 0.8) {
        await sleep(20)
        continue
      }
      const e = await edit(r, 'ripple_delete', { clip_id: under.id })
      if (!e) { trials.push({ refused: refusals.ripple_delete }); break }
      await sleep(700)
      // heard time of context time c, on performance.now()
      const tRef = now()
      const cRef = heardCtxAt(ctx, tRef)
      if (cRef === null) { trials.push({ noTimestamp: true }); continue }
      const perfOf = (c: number) => tRef + (c - cRef) * 1000
      let heardAt: number | null = null
      let run = 0
      const flat: number[] = []
      for (const b of blocks) {
        const bEnd = perfOf((b.frame + b.L.length) / sr)
        if (bEnd < e.t0) continue
        for (let i = 0; i + win <= b.L.length; i += win) {
          const at = perfOf((b.frame + i) / sr)
          if (at < e.t0) continue
          const x = b.L.subarray(i, i + win)
          const pa = goertzel(x, 440)
          const pb = goertzel(x, 1320)
          const bDominates = pb > 4 * pa && pb > 1e-3
          run = bDominates ? run + 1 : 0
          if (flat.length < 400) flat.push(+(pb / Math.max(1e-9, pa)).toFixed(1))
          if (run >= 3 && heardAt === null) heardAt = at - 2 * (win / sr) * 1000
        }
        if (heardAt !== null) break
      }
      // the picture: the first frame of B drawn after the commit (the bar's
      // source id), on the same performance.now() clock
      const pic = r.draws.find((d) => d.playing && d.at >= e.t0 && d.bar >= 0 && barSrc(d.bar) === 2)
      trials.push({ k, rttMs: +(e.tAnswer - e.t0).toFixed(1), heardMs: heardAt === null ? null : +(heardAt - e.t0).toFixed(1),
        pictureMs: pic ? +(pic.at - e.t0).toFixed(1) : null, frameMs: +(1000 / fps).toFixed(2),
        ratios: heardAt === null ? flat.slice(0, 80) : undefined })
    }
    r.ctl.pause()
    return { trials, events, restarts, outputLatency: (ctx as { outputLatency?: number }).outputLatency ?? null, audioStats: a.audio.stats }
  },

  /** P1-A3: flash frames vs clicks through the real AudioEngine. */
  async av_sync(cfg) {
    const a = await audioRig(cfg)
    await waitWindowFilled(a.r)
    await sleep(500)
    const events: Result[] = []
    const t00 = now()
    a.r.engine.on('pause-external', (e) => events.push({ at: +(now() - t00).toFixed(0), ev: 'pause-external', ...e }))
    a.r.engine.on('buffering', (e) => events.push({ at: +(now() - t00).toFixed(0), ev: 'buffering', ...e }))
    document.addEventListener('visibilitychange', () => events.push({ at: +(now() - t00).toFixed(0), ev: document.visibilityState }))
    a.startTrace()
    a.r.ctl.play(0)
    const pm = a.r.engine.program!
    const dur = (pm.total * pm.R.den) / pm.R.num
    const t0 = now()
    // to the end of the program; a pause the page did not make (the window
    // hidden by something else on the machine) resumes by itself
    while (a.r.engine.presentedK < pm.total - 2 && now() - t0 < dur * 1000 + 15000) {
      if (!a.r.engine.playing && !events.some((e) => e.ev === 'pause-external')) break
      await sleep(50)
    }
    await sleep(300)
    return { ...a.finish(), playStartAt: t0, audioSync: a.r.engine.audioStats, events, total: pm.total }
  },

  /** The CTX requirement end to end: WebKit pauses the muted <video> on its
   *  own when the window is hidden or occluded; picture and sound stop
   *  together, and come back together with no drift. */
  async external_pause(cfg) {
    const a = await audioRig(cfg)
    await waitWindowFilled(a.r)
    await sleep(400)
    const log: Result[] = []
    const t0 = now()
    const at = () => +(now() - t0).toFixed(0)
    document.addEventListener('visibilitychange', () => log.push({ at: at(), ev: 'visibility', state: document.visibilityState }))
    a.r.engine.on('pause-external', (e) => log.push({ at: at(), ev: 'pause-external', ...e }))
    const video = a.r.engine.internals.video!
    video.addEventListener('pause', () => log.push({ at: at(), ev: 'video-pause' }))
    video.addEventListener('play', () => log.push({ at: at(), ev: 'video-play' }))
    a.startTrace()
    a.r.ctl.play(0)
    const phases: Result[] = []
    for (const [off, on] of [['hide', 'show'], ['occlude', 'reveal']] as const) {
      await sleep(1600)
      const kBefore = a.r.engine.presentedK
      const tOff = now()
      await window_(off)
      for (let i = 0; i < 80 && a.r.engine.playing; i++) await sleep(25)
      const stoppedAt = now()
      await sleep(1200)
      const hiddenState = {
        visibility: document.visibilityState, playing: a.r.engine.playing, videoPaused: video.paused,
        presentedK: a.r.engine.presentedK, kBefore, audioRunning: a.audio.isRunning, ctxState: a.audio.context?.state,
        drawsWhileOff: a.r.draws.filter((d) => d.playing && d.at > stoppedAt + 50).length,
        storePlaying: a.r.playingLog.at(-1)?.playing,
      }
      await window_(on)
      for (let i = 0; i < 80 && !a.r.engine.playing; i++) await sleep(25)
      const resumedAt = now()
      await sleep(2500)
      // the first frame drawn playing after the stop: where picture and
      // sound came back (none is drawn while away: drawsWhileOff)
      const resumedK = a.r.draws.find((d) => d.playing && d.at > stoppedAt + 50)?.k ?? null
      phases.push({
        off, on, tOff, stoppedAt, resumedAt, endAt: now(), resumedK, hiddenState, stopMs: +(stoppedAt - tOff).toFixed(0),
        after: { visibility: document.visibilityState, playing: a.r.engine.playing, storePlaying: a.r.playingLog.at(-1)?.playing },
      })
    }
    // an AudioContext interrupted by the system: the picture stops with it
    // and stays paused (the user did not ask to stop; the sound could not go on)
    await sleep(800)
    const kInt = a.r.engine.presentedK
    const tInt = now()
    await a.audio.context!.suspend()
    for (let i = 0; i < 40 && a.r.engine.playing; i++) await sleep(25)
    await sleep(600)
    const interrupted = { playing: a.r.engine.playing, kInt, presentedK: a.r.engine.presentedK, ms: +(now() - tInt).toFixed(0),
      storePlaying: a.r.playingLog.at(-1)?.playing, audioRunning: a.audio.isRunning }
    a.r.ctl.pause()
    await sleep(300)
    return { ...a.finish(), phases, log, interrupted, playingLog: a.r.playingLog, externalPauses: a.r.engine.stats.externalPauses }
  },
}

/** A random structural edit (the P1-S1 corpus). */
function pickEdit(edl: EdlLike, rnd: () => number, srcs: string[]): { tool: string; args: Record<string, unknown> } {
  const clips = v1(edl)
  const r3 = (x: number) => Math.round(x * 1000) / 1000
  const pick = <T>(xs: T[]) => xs[Math.floor(rnd() * xs.length)]
  const r = rnd()
  const eff = (c: V1Clip & { speed?: unknown }) => (c.out - c.in) / (typeof c.speed === 'number' && c.speed > 0 ? c.speed : 1)
  if (!clips.length || r < 0.12) {
    const end = clips.length ? Math.max(...clips.map((c) => c.start + eff(c))) : 0
    const a = r3(rnd() * 4)
    return { tool: 'add_clip', args: { src: pick(srcs), track: 'v1', in: a, out: r3(a + 0.4 + rnd() * 2), start: r3(end) } }
  }
  const c = pick(clips) as V1Clip & { reverse?: boolean }
  if (r < 0.26) return { tool: 'split_at', args: { track: 'v1', time: r3(c.start + eff(c) * (0.2 + 0.6 * rnd())) } }
  if (r < 0.40) {
    return rnd() < 0.5
      ? { tool: 'trim_clip', args: { clip_id: c.id, in: r3(Math.max(0, c.in + (rnd() - 0.4) * (c.out - c.in) * 0.4)) } }
      : { tool: 'trim_clip', args: { clip_id: c.id, out: r3(c.in + (c.out - c.in) * (0.5 + rnd() * 0.6)) } }
  }
  if (r < 0.52) return { tool: 'move_clip', args: { clip_id: c.id, new_start: r3(Math.max(0, c.start + (rnd() - 0.5) * 3)), close_gap: rnd() < 0.3 } }
  if (r < 0.60) return { tool: 'ripple_delete', args: { clip_id: c.id } }
  if (r < 0.66) return { tool: 'bulk_delete', args: { clip_ids: [c.id] } }
  if (r < 0.71) return { tool: 'duplicate_clip', args: { clip_id: c.id } }
  if (r < 0.79) return { tool: 'set_speed', args: { clip_id: c.id, factor: pick([0.5, 1, 1.5, 2, 0.75]) } }
  if (r < 0.85) return { tool: 'set_clip_reverse', args: { clip_id: c.id, reverse: !c.reverse } }
  if (r < 0.93) return { tool: 'undo', args: {} }
  return { tool: 'redo', args: {} }
}

// --------------------------------------------------- sound + picture trace

interface AudioRigOut {
  r: Rig
  audio: AudioEngine
  ctx: AudioContext
  /** the tap's recording: left channel blocks on the context frame clock */
  blocks: Array<{ frame: number; L: Float32Array }>
  startTrace(): void
  finish(): Result
}

/** An element presentation and the engine's draw of it (its rVFC handler)
 *  are the same callback turn; a busy machine delays one, never by this. */
const DRAWN_MATCH_MS = 50

/** The AudioContext time HEARD at `perfMs` (performance.now() ms): the output
 *  timestamp, minus the device latency when the timestamp is of the
 *  RENDERED time (WebKit: contextTime = currentTime − one quantum). A click
 *  the tap sees at context time c is heard when this equals c. */
function heardCtxAt(ctx: AudioContext, perfMs: number): number | null {
  const ts = ctx.getOutputTimestamp?.() as { contextTime?: number; performanceTime?: number } | undefined
  if (!ts || !ts.contextTime || !ts.performanceTime) return null
  const outLat = (ctx as { outputLatency?: number }).outputLatency ?? 0
  const rendered = ctx.currentTime - (ts.contextTime + (performance.now() - ts.performanceTime) / 1000)
  if (outLat > 0 && rendered < outLat / 2) renderedTs.add(ctx)
  const t = ts.contextTime + (perfMs - ts.performanceTime) / 1000
  return renderedTs.has(ctx) ? t - outLat : t
}
const renderedTs = new WeakSet<AudioContext>()

/** A rig whose sound is a REAL AudioEngine tapped at its output, plus a
 *  per-presented-frame trace (rVFC metadata + getOutputTimestamp). */
async function audioRig(cfg: Config): Promise<AudioRigOut> {
  const ctx = new AudioContext({ sampleRate: 48000, latencyHint: 'interactive' })
  await ctx.audioWorklet.addModule('/wk/audio_tap.js')
  const tap = new AudioWorkletNode(ctx, 'tap', { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [2] })
  const blocks: Array<{ frame: number; L: Float32Array }> = []
  tap.port.onmessage = (e: MessageEvent) => { blocks.push(e.data as { frame: number; L: Float32Array }) }
  tap.connect(ctx.destination)
  const tsLog: Array<[number, string, number, number | undefined, number | undefined, number | undefined]> = []
  const tsTimer = setInterval(() => {
    const t = ctx.getOutputTimestamp?.() as { contextTime?: number; performanceTime?: number } | undefined
    tsLog.push([+now().toFixed(0), ctx.state, +ctx.currentTime.toFixed(4), t?.contextTime, t?.performanceTime, (ctx as { outputLatency?: number }).outputLatency])
  }, 250)
  let audio: AudioEngine | null = null
  const r = await rig(cfg, {
    audio: (onInt) => (audio = new AudioEngine({ createContext: () => ctx, destination: () => tap, onInterrupted: onInt })),
    patch: { x: cfg.canvas[0] * 0.25, y: cfg.canvas[1] * 0.6, w: cfg.canvas[0] * 0.5, h: cfg.canvas[1] * 0.2 },
  })
  const video = r.engine.internals.video!
  const frames: Array<{ k: number; E: number; ctxAt: number | null; at: number; cbNow?: number; pt?: number; playing: boolean }> = []
  let tracing = false
  const displayBase = new DisplayTimeBase()
  const onFrame: VideoFrameRequestCallback = (cbNow, meta) => {
    if (!tracing) return
    const pm = r.engine.program!
    // the display time on performance.now() (WebKit's MSE rVFC reports it
    // on another clock), mapped to the context time HEARD then
    if (!r.engine.playing || frames.length === 0 || frames[frames.length - 1].at < now() - 200) displayBase.reset()
    const E = displayBase.fix(cbNow, meta.expectedDisplayTime)
    frames.push({ k: Math.round(meta.mediaTime * pm.R.num / pm.R.den), E, ctxAt: heardCtxAt(ctx, E), at: now(), cbNow,
      pt: meta.expectedDisplayTime, playing: r.engine.playing })
    video.requestVideoFrameCallback(onFrame)
  }
  return {
    r, audio: audio!, ctx, blocks,
    startTrace() { tracing = true; video.requestVideoFrameCallback(onFrame) },
    finish() {
      tracing = false
      clearInterval(tsTimer)
      tap.port.postMessage('flush')
      const pm = r.engine.program!
      const every = cfg.flashEvery ?? 15
      // flash frames the program shows (source frame % every == 0) and what was drawn there
      const flashK = new Set<number>()
      for (let k = 0; k < pm.total; k++) if (pm.kind[k] !== KIND_GAP && pm.srcFrame[k] % every === 0) flashK.add(k)
      const cuts = new Set<number>()
      for (let k = 1; k < pm.total; k++) if (pm.clip[k] !== pm.clip[k - 1] && flashK.has(k)) cuts.add(k)
      const drawnLevel = new Map<number, number>()
      for (const d of r.draws) if (d.playing) drawnLevel.set(d.k, d.level)
      // click onsets on the context frame clock
      const sr = ctx.sampleRate
      const onsets: number[] = []
      let quiet = 0
      let lastAbs = 0
      for (const b of blocks) {
        for (let i = 0; i < b.L.length; i++) {
          const v = Math.abs(b.L[i])
          if (v > 0.2 && quiet >= 480) onsets.push((b.frame + i) / sr)
          quiet = v < 0.02 ? quiet + 1 : 0
          if (v > 0.02) lastAbs = (b.frame + i) / sr
        }
      }
      // the frames the ENGINE drew while playing (its rVFC handler, the same
      // presentation): what the user saw. The <video> under the canvas also
      // presents frames the engine never draws — WebKit playing it for a
      // moment by itself on an occluded window's visible flip, before the
      // engine parks it (engineExternal.ts), or re-presenting the stop frame
      // — and those carry no sound by design (review C1)
      const playingDraws = r.draws.filter((d) => d.playing)
      const drewAt = (k: number, at: number) => playingDraws.some((d) => d.k === k && Math.abs(d.at - at) <= DRAWN_MATCH_MS)
      const flashes = frames.filter((f) => flashK.has(f.k) && f.ctxAt !== null).map((f) => {
        let best: number | null = null
        for (const o of onsets) if (best === null || Math.abs(o - f.ctxAt!) < Math.abs(best - f.ctxAt!)) best = o
        return { k: f.k, cut: cuts.has(f.k), at: f.at, offsetMs: best === null ? null : +((f.ctxAt! - best) * 1000).toFixed(2),
          level: drawnLevel.get(f.k) ?? null, playing: f.playing, drawn: drewAt(f.k, f.at) }
      })
      const nonFlash = [...drawnLevel.entries()].filter(([k]) => !flashK.has(k)).map(([, v]) => v)
      // every click must sound where the picture is: the frame on screen at
      // the click (nearest presented frame in context time, ≤ 50 ms away)
      // has a program flash within ±2 frames. A click with no frame on
      // screen near it is sound running on while the picture is frozen.
      const presented = new Set(frames.map((f) => f.k))
      const timed = frames.filter((f) => f.ctxAt !== null)
      const clicks = onsets.map((o) => {
        let near: (typeof frames)[number] | null = null
        for (const f of timed) if (!near || Math.abs(f.ctxAt! - o) < Math.abs(near.ctxAt! - o)) near = f
        const gapMs = near ? Math.abs(near.ctxAt! - o) * 1000 : Infinity
        let flash: number | null = null
        if (near) for (let d = -2; d <= 2; d++) if (flashK.has(near.k + d)) flash = near.k + d
        const kind = gapMs > 50 ? 'frozen' : flash === null ? 'orphan' : presented.has(flash) ? 'paired' : 'dropped'
        return { t: +o.toFixed(5), kind, k: near?.k ?? null, gapMs: Number.isFinite(gapMs) ? +gapMs.toFixed(1) : null }
      })
      return {
        clicks, sampleRate: sr, frames: frames.length, onsets: onsets.length, flashes, cutFlashes: flashes.filter((f) => f.cut).length,
        maxNonFlashLevel: nonFlash.length ? Math.max(...nonFlash) : null, lastSoundCtx: lastAbs,
        frameTrace: frames.map((f) => [f.k, +(f.at).toFixed(1), f.ctxAt === null ? null : +f.ctxAt.toFixed(5), f.E, f.cbNow, f.pt]),
        onsetTimes: onsets.map((o) => +o.toFixed(5)),
        outputLatency: (ctx as { outputLatency?: number }).outputLatency ?? null, baseLatency: ctx.baseLatency,
        tsLog,
        ctxDiag: { blocks: blocks.length, state: ctx.state, currentTime: ctx.currentTime, perf: now(),
          ts: (() => { const t = ctx.getOutputTimestamp?.() as { contextTime?: number; performanceTime?: number } | undefined; return t ? [t.contextTime, t.performanceTime] : null })() },
        audioStats: audio?.stats,
      }
    },
  }
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
