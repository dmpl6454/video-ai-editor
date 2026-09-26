// WK page for the ENGINE's fault and race scenarios (tests/wk/pages/
// engine_faults.html → this bundle; tests/wk/test_wk_engine_faults.py),
// promoted from the RD2 adversarial review:
//
// * flipflop, seek_then_push: two currentTime assignments of the same time in
//   one task make WebKit fire ONE 'seeking'; the paused-seek counters must
//   never drift apart (they used to, and every later paused seek then hung).
// * seek_while_playing: WebKit still presents frames of the OLD position
//   after currentTime is set; the sound must restart at the NEW one.
// * context_loss_playing / context_loss_paused: webglcontextlost while
//   playing stops picture and sound together, never shows a snapshot of
//   another frame, and resumes both on restore when the user was playing.
// * index_500: one 5xx on a proxy's index.json at open is retried, never a
//   proxy degraded for the engine's whole life.
import type { ClientPreviewEngine } from '../engine'
import { KIND_GAP } from '../timeline/programMap'
import { mulberry32, now, sleep } from './mseKit'
import { drawnPaused, expectedCode, fmt, setup, type Fixture, type Probe, type Result } from './engineProbe'

const q = new URLSearchParams(location.search)
const token = q.get('token') ?? 'none'
const scenario = q.get('scenario') ?? ''
const fixtureUrl = q.get('fixture') ?? '/fixture/timeline.json'
/** A settled paused seek must show well inside the engine's 2 s seek
 *  timeout: a wedged seek that the timeout later re-issues is still a wedge. */
const SHOW_MS = 1500

async function post(body: Result): Promise<void> {
  await fetch(`/__result/${token}`, { method: 'POST', body: JSON.stringify(body) })
}

function lagOf(engine: ClientPreviewEngine): number {
  const c = engine.internals.seekCounters()
  return c.issued - c.seen
}

async function windowFilled(p: Probe, frames: number): Promise<void> {
  const lane = p.engine.internals.lane!
  for (let i = 0; i < 400 && lane.buffered[1] < frames; i++) await sleep(25)
}

/** Shown paused, exactly: the right bar on the canvas, the counters in step. */
async function exactPaused(p: Probe, k: number) {
  const pm = p.engine.program!
  const t0 = now()
  const lane0 = { ...p.engine.internals.lane!.stats }
  const shown = await drawnPaused(p.engine, k, 3 * SHOW_MS)
  const ms = +(now() - t0).toFixed(0)
  const exp = expectedCode(pm, k, p.srcIds)
  const got = p.bars.get(k) ?? -9
  const row: Result = { k, ok: shown && ms <= SHOW_MS && got === exp, shown, ms, exp: fmt(exp), got: fmt(got), lag: lagOf(p.engine) }
  if (!row.ok) {
    if (trace.length) row.trace = trace.filter(([t]) => t >= t0 - 50).slice(0, 80)
    const l = p.engine.internals.lane!
    row.diag = { lane0, lane: l.stats, buffered: l.buffered, seeks: p.engine.stats.seeks, timeouts: p.engine.stats.seekTimeouts,
      store: p.engine.internals.store.stats, target: p.engine.targetK, presented: p.engine.presentedK }
  }
  return row
}

/** ?trace=1: every laneA action and element event, for a failing row's diag. */
const trace: Array<[number, string]> = []
function traceLane(p: Probe): void {
  const lane = p.engine.internals.lane as unknown as { plan(): unknown; setPlayhead(k: number, pl: boolean): void }
  const plan = lane.plan.bind(lane)
  lane.plan = () => {
    const a = plan() as { kind: string; a?: number; b?: number; why?: string } | null
    if (a) trace.push([+now().toFixed(1), `${a.kind} ${a.a ?? ''}-${a.b ?? ''} ${a.why ?? ''} buf=${(p.engine.internals.lane!.buffered as number[]).join('-')}`])
    return a
  }
  const sp = lane.setPlayhead.bind(lane)
  lane.setPlayhead = (k: number, pl: boolean) => { trace.push([+now().toFixed(1), `playhead ${k} ${pl}`]); sp(k, pl) }
  const v = p.engine.internals.video!
  for (const ev of ['seeking', 'seeked', 'waiting', 'stalled']) v.addEventListener(ev, () => trace.push([+now().toFixed(1), `${ev} t=${v.currentTime.toFixed(4)}`]))
}

/** Mean brightness of the 2D snapshot canvas (0..255). */
function snapshotLevel(engine: ClientPreviewEngine): number {
  const snap = engine.internals.compositor!.snapshotCanvas!
  const d = snap.getContext('2d')!.getImageData(0, 0, snap.width, snap.height).data
  let sum = 0
  for (let i = 0; i < d.length; i += 16) sum += d[i] + d[i + 1] + d[i + 2]
  return sum / (3 * (d.length / 16))
}

const scenarios: Record<string, (fx: Fixture) => Promise<Result>> = {
  /** seek(K) → seek(K+1) → seek(K) in one task (→ then ←), then a plain seek. */
  async flipflop(fx) {
    const p = await setup(fx)
    if (q.get('trace')) traceLane(p)
    const pm = p.engine.program!
    await drawnPaused(p.engine, 0, 15000)
    await windowFilled(p, pm.total)
    const rnd = mulberry32(5)
    const rows: Result[] = []
    for (let i = 0; i < 80; i++) {
      const K = 10 + Math.floor(rnd() * (pm.total - 20))
      p.engine.seek(K)
      p.engine.seek(K + 1)
      p.engine.seek(K)
      rows.push({ i, step: 'flip', ...(await exactPaused(p, K)) })
      const k2 = Math.floor(rnd() * pm.total)
      p.engine.seek(k2)
      rows.push({ i, step: 'after', ...(await exactPaused(p, k2)) })
    }
    const bad = rows.filter((r) => !r.ok || r.lag !== 0)
    return { n: rows.length, bad: bad.slice(0, 8), nBad: bad.length, seekTimeouts: p.engine.stats.seekTimeouts, finalLag: lagOf(p.engine) }
  },

  /** A paused seek, and the SAME timeline pushed again in the same task (the
   *  controller's pushTimeline when /frame_map or a source lookup lands). */
  async seek_then_push(fx) {
    const p = await setup(fx)
    const pm = p.engine.program!
    await drawnPaused(p.engine, 0, 15000)
    await windowFilled(p, pm.total)
    const rnd = mulberry32(8)
    const rows: Result[] = []
    for (let i = 0; i < 60; i++) {
      const K = Math.floor(rnd() * pm.total)
      p.engine.seek(K)
      p.engine.setTimeline(fx.edl, 'fixture', p.lookup)
      rows.push({ i, ...(await exactPaused(p, K)) })
    }
    const bad = rows.filter((r) => !r.ok || r.lag !== 0)
    return { n: rows.length, bad: bad.slice(0, 8), nBad: bad.length, seekTimeouts: p.engine.stats.seekTimeouts, finalLag: lagOf(p.engine) }
  },

  /** Seeks while playing: every sound (re)start after a seek is at the NEW
   *  position (the frame the picture shows there), never at a stale frame
   *  WebKit presented from the old one. */
  async seek_while_playing(fx) {
    const p = await setup(fx)
    const { engine, sink } = p
    const pm = engine.program!
    const R = pm.R
    await drawnPaused(engine, 0, 15000)
    await windowFilled(p, pm.total)
    engine.play()
    await sleep(700)
    const rnd = mulberry32(2)
    const seeks: Array<{ at: number; k: number; from: number }> = []
    const t0 = now()
    let prev = engine.presentedK
    for (let i = 0; i < 24; i++) {
      // a jump at least 40 frames from where the picture is now
      const far = (k: number) => Math.abs(k - engine.presentedK) >= 40 && Math.abs(k - prev) >= 40
      let k = Math.floor(rnd() * (pm.total - 90)) + 10
      while (!far(k)) k = Math.floor(rnd() * (pm.total - 90)) + 10
      seeks.push({ at: now(), k, from: engine.presentedK })
      engine.seek(k)
      prev = k
      await sleep(150 + Math.floor(rnd() * 150))
      if (!engine.playing) engine.play()
    }
    const end = now()
    engine.pause()
    const perFrameS = R.den / R.num
    const starts = sink.calls.filter((c) => c.op === 'start' && c.at >= seeks[0].at && c.at <= end)
    const rows = seeks.map((s, i) => {
      const until = i + 1 < seeks.length ? seeks[i + 1].at : end
      const mine = starts.filter((c) => c.at > s.at && c.at <= until)
      const frames = mine.map((c) => +((c.sample! / 48000) / perFrameS).toFixed(1))
      return { k: s.k, from: s.from, startFrames: frames, far: frames.filter((f) => Math.abs(f - s.k) > 12) }
    })
    const stale = rows.filter((r) => r.far.length > 0)
    const restarted = rows.filter((r) => r.startFrames.length > 0).length
    const drawn = p.draws.filter((d) => d.playing && d.at >= t0)
    const wrong = drawn.filter((d) => pm.kind[d.k] !== KIND_GAP && d.bar !== expectedCode(pm, d.k, p.srcIds))
    return { seeks: rows.length, restarted, stale: stale.slice(0, 8), nStale: stale.length, drawn: drawn.length, wrong: wrong.length, R }
  },

  /** webglcontextlost while PLAYING: picture and sound stop together at
   *  presentedK, no snapshot of another frame is shown, and on restore both
   *  resume from a fresh anchor (the user never paused). */
  async context_loss_playing(fx) {
    const p = await setup(fx)
    const { engine, sink } = p
    const pm = engine.program!
    await drawnPaused(engine, 0, 15000)
    await windowFilled(p, 400)
    engine.seek(60)
    await drawnPaused(engine, 60, 5000)
    // the pause snapshot now holds k = 60
    engine.play()
    for (let i = 0; i < 100 && engine.presentedK < 110; i++) await sleep(20)
    const comp = engine.internals.compositor!
    const ext = comp.context!.getExtension('WEBGL_lose_context')
    if (!ext) return { skipped: 'no WEBGL_lose_context' }
    sink.calls.length = 0
    const events: Result[] = []
    engine.on('pause-external', (e) => events.push({ ...e }))
    ext.loseContext()
    await sleep(150)
    const kLost = engine.presentedK
    const snap = comp.snapshotCanvas!
    const during = {
      playing: engine.playing, videoPaused: engine.internals.video!.paused, kLost,
      snapshotK: comp.snapshotK, snapshotVisible: snap.style.visibility, level: +snapshotLevel(engine).toFixed(1),
      spinner: engine.status.spinner,
      stops: sink.calls.filter((c) => c.op === 'stop').map((c) => c.k), starts: sink.calls.filter((c) => c.op === 'start').length,
    }
    await sleep(600)
    const later = { presentedK: engine.presentedK, playing: engine.playing, starts: sink.calls.filter((c) => c.op === 'start').length }
    sink.calls.length = 0
    const drawsFrom = p.draws.length
    ext.restoreContext()
    for (let i = 0; i < 100 && !(engine.playing && engine.presentedK > kLost + 10); i++) await sleep(20)
    const after = {
      playing: engine.playing, presentedK: engine.presentedK, mode: engine.status.mode,
      starts: sink.calls.filter((c) => c.op === 'start').map((c) => ({ k: c.k, sample: c.sample })),
      glVisible: engine.internals.canvas!.style.visibility,
    }
    engine.pause()
    const drawn = p.draws.slice(drawsFrom).filter((d) => d.playing)
    const wrong = drawn.filter((d) => d.bar !== expectedCode(pm, d.k, p.srcIds)).map((d) => [d.k, fmt(d.bar)])
    return { events, during, later, after, drawnAfter: drawn.length, wrong, R: pm.R }
  },

  /** webglcontextlost while PAUSED: the snapshot is the paused frame; a seek
   *  while lost shows that target after the restore; the engine stays paused. */
  async context_loss_paused(fx) {
    const p = await setup(fx)
    const { engine } = p
    const pm = engine.program!
    await drawnPaused(engine, 0, 15000)
    engine.seek(75)
    await drawnPaused(engine, 75, 5000)
    const comp = engine.internals.compositor!
    const ext = comp.context!.getExtension('WEBGL_lose_context')
    if (!ext) return { skipped: 'no WEBGL_lose_context' }
    ext.loseContext()
    await sleep(100)
    const lost = { snapshotK: comp.snapshotK, visible: comp.snapshotCanvas!.style.visibility, level: +snapshotLevel(engine).toFixed(1) }
    engine.seek(200)
    await sleep(200)
    ext.restoreContext()
    const shown = await drawnPaused(engine, 200, 4000)
    return { lost, shown, playing: engine.playing, bar: fmt(p.bars.get(200) ?? -9), exp: fmt(expectedCode(pm, 200, p.srcIds)) }
  },

  /** One 5xx on source P's index.json (the Python side scripts it): the
   *  first frame still shows, and nothing of P is demoted to BAKED. */
  async index_500(fx) {
    const t0 = now()
    const p = await setup(fx)
    const { engine } = p
    const pm = engine.program!
    const shown = await drawnPaused(engine, 0, 10000)
    const ms = +(now() - t0).toFixed(0)
    const degraded = engine.status.ranges.filter((r) => r.reasons.includes('proxy:degraded')).map((r) => [r.k0, r.k1])
    return { shown, ms, bar: fmt(p.bars.get(0) ?? -9), exp: fmt(expectedCode(pm, 0, p.srcIds)), degraded,
      retries: engine.internals.store.stats.openRetries, failure: engine.internals.store.failure('P') }
  },
}

;(async () => {
  const result: Result = { scenario, ua: navigator.userAgent }
  try {
    const run = scenarios[scenario]
    if (!run) throw new Error(`unknown scenario ${JSON.stringify(scenario)}`)
    const fx = (await (await fetch(fixtureUrl)).json()) as Fixture
    const t0 = now()
    Object.assign(result, await run(fx))
    result.scenarioMs = +(now() - t0).toFixed(1)
  } catch (e) {
    result.fatal = String((e as Error)?.stack ?? e)
  }
  await post(result)
})()
