// WK page for the engine's Phase 1d ROBUSTNESS (tests/wk/pages/robustness.html
// → this bundle; tests/wk/test_wk_robustness.py): INSTANT_PREVIEW_SPEC §7
// fallback ladder, §3.5 visibility, §13 P1-R1.
//
// * degraded: source P's proxy answers 410 (permanently failed); every paused
//   frame of P comes from the degraded <video> on P's MASTER (a long-GOP,
//   B-frame mp4 like ingest's), sought with the +1 ms bias and confirmed by
//   rVFC, exact to the bar; the other sources stay on laneA.
// * hidden: the harness orders the window out while laneA's window is still
//   filling; no append, remove or new span fetch happens while hidden; a seek
//   made while hidden shows exactly once the page is back.
// * decode_errors: garbage sample bytes for one frame make WebKit fail the
//   decode; the engine counts each, rebuilds laneA below 3 in 60 s, and falls
//   back to the server preview at the 3rd (a persistent fault), or recovers
//   and shows the exact frame (a transient one).
// * context_no_restore: webglcontextlost never restored → server mode in 2 s.
// * no_mse: MediaSource (and ManagedMediaSource) deleted → the app decides
//   server mode, and the engine refuses to start (server, no-mse).
import { createPreviewEngine, type ClientPreviewEngine } from '../engine'
import { KIND_GAP } from '../timeline/programMap'
import { MODE_BAKED } from '../timeline/support'
import { parsePreviewSettings, probePreviewCapabilities, resolvePreviewMode } from '../../previewEngineSetting'
import { mulberry32, now, sleep } from './mseKit'
import { drawnPaused, expectedCode, fmt, lookupOf, setup, type Fixture, type Probe, type Result } from './engineProbe'
import type { FrameSample } from '../media/fmp4Writer'
import { api } from '../../../api'
import { isAbort, isEngineOffline, onEngineState } from '../../connection'

const q = new URLSearchParams(location.search)
const token = q.get('token') ?? 'none'
const scenario = q.get('scenario') ?? ''
const fixtureUrl = q.get('fixture') ?? '/fixture/timeline.json'

async function post(body: Result): Promise<void> {
  await fetch(`/__result/${token}`, { method: 'POST', body: JSON.stringify(body) })
}

async function windowCmd(cmd: 'hide' | 'show'): Promise<void> {
  await fetch(`/__window/${token}/${cmd}`, { method: 'POST' })
}

async function until(cond: () => boolean, ms: number, step = 20): Promise<boolean> {
  const t0 = now()
  while (!cond()) {
    if (now() - t0 > ms) return false
    await sleep(step)
  }
  return true
}

/** Every /api/proxies request the page makes, with the visibility at the time. */
const proxyLog: Array<{ at: number; url: string; hidden: boolean }> = []
const realFetch = window.fetch.bind(window)
window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/api/proxies/')) proxyLog.push({ at: now(), url: url.replace(/^https?:\/\/[^/]+/, ''), hidden: document.visibilityState === 'hidden' })
  return realFetch(input, init)
}) as typeof window.fetch

/** Show k paused and read the bar: exact or not, and how long it took. */
async function showExact(p: Probe, k: number, ms = 5000) {
  const pm = p.engine.program!
  const t0 = now()
  p.engine.seek(k)
  const shown = await drawnPaused(p.engine, k, ms)
  const exp = expectedCode(pm, k, p.srcIds)
  const got = p.bars.get(k) ?? -9
  return { k, shown, ok: shown && got === exp, ms: +(now() - t0).toFixed(1), exp: fmt(exp), got: fmt(got) }
}

function framesOf(p: Probe, src: string): number[] {
  const pm = p.engine.program!
  const out: number[] = []
  for (let k = 0; k < pm.total; k++) if (pm.kind[k] !== KIND_GAP && pm.sources[pm.srcKey[k]] === src) out.push(k)
  return out
}

function shuffle<T>(xs: T[], seed: number): T[] {
  const rnd = mulberry32(seed)
  for (let i = xs.length - 1; i > 0; i--) { const j = Math.floor(rnd() * (i + 1)); [xs[i], xs[j]] = [xs[j], xs[i]] }
  return xs
}

const pct = (xs: number[], p: number) => { const s = [...xs].sort((a, b) => a - b); return s[Math.min(s.length - 1, Math.floor(p * s.length))] ?? null }

/** Garbage in place of one frame's sample until the element has reported
 *  `times` media errors (a sample is also read to check it is loaded, so
 *  the fault is counted by the errors it causes, not by reads). */
function corrupt(engine: ClientPreviewEngine, key: string, frame: number, times: number, variant: string) {
  const store = engine.internals.store as unknown as { sample(key: string, frame: number): FrameSample | null }
  const real = store.sample.bind(store)
  const state = { errors: 0, served: 0 }
  engine.internals.video!.addEventListener('error', () => { state.errors++ })
  store.sample = (k: string, f: number) => {
    const s = real(k, f)
    if (!s || k !== key || f !== frame || state.errors >= times) return s
    state.served++
    let bytes: Uint8Array
    if (variant === 'length') {
      // an AVCC length prefix far past the sample's end
      bytes = new Uint8Array(s.bytes)
      new DataView(bytes.buffer).setUint32(0, 0x7fffff00)
    } else if (variant === 'noise') {
      const rnd = mulberry32(frame)
      bytes = new Uint8Array(s.bytes.byteLength)
      for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(rnd() * 256)
      new DataView(bytes.buffer).setUint32(0, bytes.length - 4)
      bytes[4] = 0x65 // an IDR slice header over noise
    } else {
      bytes = new Uint8Array(0)
    }
    return { format: s.format, bytes }
  }
  return state
}

/** The lane/store state a stuck paused frame is diagnosed from. */
function laneDiag(p: Probe) {
  const e = p.engine
  const l = e.internals.lane
  return {
    target: e.targetK, presented: e.presentedK, spinner: e.status.spinner, mode: e.status.mode,
    lane: l ? { buffered: l.buffered, stats: l.stats } : null,
    store: { ...e.internals.store.stats, queued: e.internals.store.queued, pending: e.internals.store.pending },
  }
}

const scenarios: Record<string, (fx: Fixture) => Promise<Result>> = {
  /** The paused frame's proxy fails AFTER the engine asked for it (a spans
   *  or index 410 once the summary said "ready", or 5 transient open
   *  failures): the degraded tier shows it without another seek (review
   *  RD3 — it stayed stale under a spinner until the user sought). */
  async failed_first_frame(fx) {
    const p = await setup(fx)
    const { engine } = p
    const k = Number(q.get('k') ?? '0')
    if (k) engine.seek(k)
    const t0 = now()
    const shown = await drawnPaused(engine, k, Number(q.get('wait') ?? '15000'))
    const ms = +(now() - t0).toFixed(0)
    const degraded = engine.status.ranges.some((r) => r.reasons.includes('proxy:degraded'))
    const exp = expectedCode(engine.program!, k, p.srcIds)
    const got = p.bars.get(k) ?? -9
    const other = framesOf(p, 'P').find((j) => j > k + 30)!
    const next = await showExact(p, other, 8000)
    return { k, shown, ms, degraded, got: fmt(got), exp: fmt(exp), next, diag: laneDiag(p),
      degradedStats: engine.internals.degraded.stats }
  },

  /** A span answers 5xx (the harness decides how often) while the engine is
   *  paused on a frame in it and nothing else is loading: the store retries
   *  it by itself (review RD3 — it waited for a poke that never came). */
  async span_retry(fx) {
    const p = await setup(fx)
    const { engine } = p
    await drawnPaused(engine, 0, 15000)
    const r = await showExact(p, Number(q.get('k') ?? '1500'), Number(q.get('wait') ?? '12000'))
    return { r, diag: laneDiag(p) }
  },

  /** The rVFC chain dies at play start (callbacks swallowed, then the real
   *  method back but nothing re-registered): the element plays on, no
   *  'waiting', no frame reaches the canvas — the silent freeze review RD3
   *  saw. The stuck-play watchdog restarts the run; frames flow, exact. */
  async stall_watchdog(fx) {
    const p = await setup(fx)
    const { engine } = p
    await drawnPaused(engine, 0, 15000)
    const video = engine.internals.video! as HTMLVideoElement & { requestVideoFrameCallback: (cb: VideoFrameRequestCallback) => number }
    const real = video.requestVideoFrameCallback.bind(video)
    let swallowed = 0
    video.requestVideoFrameCallback = () => { swallowed++; return 0 }
    const d0 = p.draws.length
    engine.play()
    await sleep(400)
    video.requestVideoFrameCallback = real          // the lost chain stays lost
    const t0 = now()
    const moved = await until(() => engine.presentedK > 30, Number(q.get('wait') ?? '6000'))
    const ms = +(now() - t0).toFixed(0)
    const at = { playing: engine.playing, buffering: engine.status.buffering, t: video.currentTime, paused: video.paused,
      externalPauses: engine.stats.externalPauses, statuses: p.statuses.slice(-6) }
    engine.pause()
    const wrong = p.draws.slice(d0).filter((d) => d.playing && d.bar !== expectedCode(engine.program!, d.k, p.srcIds))
    return { at, swallowed, moved, ms, presented: engine.presentedK, restarts: engine.stats.stallRestarts,
      drawnPlaying: p.draws.slice(d0).filter((d) => d.playing).length, nWrong: wrong.length, wrong: wrong.slice(0, 6) }
  },

  /** Spans still being encoded (202 + Retry-After) must not hold every
   *  fetch slot: a paused seek to a READY span shows at once (review RD3). */
  async span_pending(fx) {
    const p = await setup(fx)
    const { engine } = p
    await drawnPaused(engine, 0, 15000)
    engine.seek(Number(q.get('k1') ?? '1560'))
    await sleep(Number(q.get('dwell') ?? '800'))
    const stuck = laneDiag(p)
    const r = await showExact(p, Number(q.get('k2') ?? '1000'), Number(q.get('wait') ?? '6000'))
    return { stuck, r, diag: laneDiag(p) }
  },

  /** P1-R1 "proxy failed → degraded tier: paused frames exact to the bar". */
  async degraded(fx) {
    const p = await setup(fx)
    const { engine } = p
    const pm = engine.program!
    const failedSrc = q.get('failed') ?? 'P'
    const isDegraded = () => engine.status.ranges.some((r) => r.reasons.includes('proxy:degraded'))
    const became = await until(isDegraded, 15000)
    const dks = framesOf(p, failedSrc)
    const others = shuffle(Array.from({ length: pm.total }, (_, k) => k).filter((k) => !dks.includes(k)), 3).slice(0, 40)
    const order = shuffle([...dks, ...others], 11)
    const rows = []
    for (const k of order) rows.push({ ...(await showExact(p, k)), degraded: dks.includes(k) })
    const bad = rows.filter((r) => !r.ok)
    const dms = rows.filter((r) => r.degraded).map((r) => r.ms)
    // the classification: exactly the failed source's frames are BAKED 'proxy:degraded'
    const flagged = new Set<number>()
    for (const r of engine.status.ranges) if (r.reasons.includes('proxy:degraded')) for (let k = r.k0; k < r.k1; k++) flagged.add(k)
    const baked = engine.status.ranges.filter((r) => r.mode === MODE_BAKED).reduce((n, r) => n + (r.k1 - r.k0), 0)
    return {
      became, total: pm.total, degradedFrames: dks.length, others: others.length, n: rows.length,
      nBad: bad.length, bad: bad.slice(0, 10), p50: pct(dms, 0.5), p95: pct(dms, 0.95), max: pct(dms, 1),
      flaggedEqual: flagged.size === dks.length && dks.every((k) => flagged.has(k)), bakedFrames: baked,
      degradedStats: engine.internals.degraded.stats, elements: engine.stats.mediaElementsCreated,
      liveVideos: document.querySelectorAll('video').length, failure: engine.internals.store.failure(fx.sources[failedSrc].key),
      mode: engine.status.mode,
    }
  },

  /** §3.5: hidden → no appends, removes or new span fetches; shown → resume. */
  async hidden(fx) {
    const p = await setup(fx)
    const { engine } = p
    const pm = engine.program!
    const vis: Result[] = []
    document.addEventListener('visibilitychange', () => vis.push({ at: +now().toFixed(0), state: document.visibilityState }))
    await drawnPaused(engine, 0, 15000)
    const lane = engine.internals.lane!
    const store = engine.internals.store
    const snap = () => ({ appends: lane.stats.appends, removes: lane.stats.removes, buffered: [...lane.buffered], spanFetches: store.stats.spanFetches, rangeReads: store.stats.rangeReads, queued: store.queued, active: store.pending - store.queued })
    const beforeHide = snap()
    const tHide = now()
    await windowCmd('hide')
    const hid = await until(() => document.visibilityState === 'hidden', 4000, 10)
    const atHide = snap()
    // an append or remove already running finishes; nothing new starts
    await lane.idle()
    const atHideIdle = snap()
    const requestedBefore = new Set(proxyLog.filter((r) => !r.hidden).map((r) => r.url.split('?')[0]))
    const target = Math.min(pm.total - 1, 420)
    engine.seek(target)
    await sleep(1500)
    const whileHidden = snap()
    const hiddenReqs = proxyLog.filter((r) => r.hidden).map((r) => r.url.split('?')[0])
    const newWhileHidden = hiddenReqs.filter((u) => !requestedBefore.has(u))
    const stillHidden = document.visibilityState === 'hidden'
    await windowCmd('show')
    const back = await until(() => document.visibilityState === 'visible', 4000, 10)
    const shown = await showExact(p, target, 8000)
    await until(() => lane.buffered[1] >= Math.min(pm.total, target + 300), 10000)
    const after = snap()
    return {
      hid, back, stillHidden, vis, beforeHide, atHide, atHideIdle, whileHidden, after, shown, hiddenReqs, newWhileHidden,
      hideMs: +(now() - tHide).toFixed(0), total: pm.total,
    }
  },

  /** Decode errors (§7): `times` garbage appends of one frame. */
  async decode_errors(fx) {
    const p = await setup(fx)
    const { engine } = p
    const pm = engine.program!
    const variant = q.get('variant') ?? 'noise'
    const times = Number(q.get('times') ?? '1000')
    await drawnPaused(engine, 0, 15000)
    // a frame of source Q, far from the window around 0 (not appended yet)
    const ks = framesOf(p, 'Q').filter((k) => k > 300)
    const k = ks[0]
    const key = fx.sources.Q.key
    const frame = pm.srcFrame[k]
    const errors: Result[] = []
    const video = engine.internals.video!
    video.addEventListener('error', () => errors.push({ at: +now().toFixed(0), code: video.error?.code ?? 0, msg: video.error?.message ?? '' }))
    const statuses: Result[] = []
    engine.on('status', (st) => { if (st.mode === 'server') statuses.push({ at: +now().toFixed(0), mode: st.mode, reason: st.reason }) })
    const inj = corrupt(engine, key, frame, times, variant)
    const t0 = now()
    engine.seek(k)
    await until(() => engine.status.mode === 'server' || (inj.errors >= times && engine.internals.settled()), 20000)
    const fellBackMs = engine.status.mode === 'server' ? +(now() - t0).toFixed(0) : null
    let after: Result | null = null
    if (engine.status.mode === 'client') {
      // transient: the engine recovered; the frame shows exactly
      after = await showExact(p, k, 8000)
      const other = ks[Math.min(ks.length - 1, 20)]
      after = { ...after, other: await showExact(p, other, 8000) }
    }
    return {
      variant, times, k, frame, served: inj.served, caused: inj.errors, errors, statuses, mode: engine.status.mode, reason: engine.status.reason,
      fellBackMs, recovery: engine.internals.recovery.stats, lane: engine.internals.lane?.stats ?? null, after,
      elements: engine.stats.mediaElementsCreated,
    }
  },

  /** A reload with an app request in flight (api.ts → lib/connection):
   *  WebKit fails the request a few ms before pagehide. The dying page must
   *  not decide the engine is offline, nor hand callers an "engine not
   *  responding" error. sessionStorage carries its log to the reloaded page. */
  async reload_in_flight() {
    const key = 'e4-reload'
    const prev = sessionStorage.getItem(key)
    if (prev) {
      sessionStorage.removeItem(key)
      return { reloaded: true, log: JSON.parse(prev) }
    }
    const log: Result[] = []
    const t0 = now()
    const note = (e: Result) => { log.push({ at: +(now() - t0).toFixed(1), ...e }); sessionStorage.setItem(key, JSON.stringify(log)) }
    note({ ev: 'start' })
    for (const ev of ['pagehide', 'visibilitychange']) window.addEventListener(ev, () => note({ ev }))
    onEngineState((next) => note({ ev: 'engine', state: next }))
    void api.health().then(
      () => note({ ev: 'resolved' }),
      (e: Error) => note({ ev: 'rejected', name: e.name, msg: e.message, offline: isEngineOffline(e), abort: isAbort(e) }))
    await sleep(150)
    location.reload()
    await sleep(60000)
    return { never: true }
  },

  /** webglcontextlost never restored: server mode after 2 s (§7). */
  async context_no_restore(fx) {
    const p = await setup(fx)
    const { engine } = p
    await drawnPaused(engine, 0, 15000)
    const shown = await showExact(p, 75)
    const comp = engine.internals.compositor!
    const ext = comp.context!.getExtension('WEBGL_lose_context')
    if (!ext) return { skipped: 'no WEBGL_lose_context' }
    const t0 = now()
    ext.loseContext()
    await sleep(300)
    const lost = { mode: engine.status.mode, snapshotK: comp.snapshotK, snapVisible: comp.snapshotCanvas!.style.visibility }
    const fell = await until(() => engine.status.mode === 'server', 5000)
    return { shown, lost, fell, fellMs: +(now() - t0).toFixed(0), mode: engine.status.mode, reason: engine.status.reason }
  },

  /** MediaSource gone: the app picks the server preview; the engine refuses. */
  async no_mse(fx) {
    const w = window as unknown as Record<string, unknown>
    const had = { mse: 'MediaSource' in w, managed: 'ManagedMediaSource' in w }
    delete w.MediaSource
    delete w.ManagedMediaSource
    const caps = probePreviewCapabilities()
    const decisions = ['auto', 'client'].map((engine) => ({ engine, ...resolvePreviewMode(parsePreviewSettings({ engine, source: 'settings' }), caps, true) }))
    const host = document.createElement('div')
    host.style.cssText = `position:relative;width:${fx.canvas[0]}px;height:${fx.canvas[1]}px;`
    document.body.appendChild(host)
    const engine = createPreviewEngine({ canvasSize: { w: fx.canvas[0], h: fx.canvas[1] } })
    engine.attach(host)
    engine.setTimeline(fx.edl, 'fixture', lookupOf(fx))
    engine.play()
    await sleep(200)
    return {
      had, gone: !('MediaSource' in w) && !('ManagedMediaSource' in w), caps, decisions,
      status: { mode: engine.status.mode, reason: engine.status.reason, playing: engine.status.playing },
      engineVideos: host.querySelectorAll('video').length, program: !!engine.program,
    }
  },
}

;(async () => {
  const result: Result = { scenario, ua: navigator.userAgent }
  try {
    const run = scenarios[scenario]
    if (!run) throw new Error(`unknown scenario ${JSON.stringify(scenario)}`)
    const fx = (await (await realFetch(fixtureUrl)).json()) as Fixture
    const t0 = now()
    Object.assign(result, await run(fx))
    result.scenarioMs = +(now() - t0).toFixed(1)
  } catch (e) {
    result.fatal = String((e as Error)?.stack ?? e)
  }
  await post(result)
})()
