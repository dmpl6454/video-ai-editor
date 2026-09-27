// WK acceptance page for the ENGINE (tests/wk/pages/engine.html → this
// bundle; tests/wk/test_wk_phase1_video.py). Unlike wkMsePage, nothing here
// writes MSE itself: the page builds the real PreviewEngine (laneA, the
// WebGL2 compositor, the presented clock, ProxyStore over the proxy routes)
// from a fixture EDL and reads back what the ENGINE CANVAS shows.
//
// Frame identity: the fixture sources burn a 15-cell bar (testkit/barcode);
// engineProbe.ts maps each cell's source centre through geometry.ts to the
// canvas and reads the pixels inside the compositor's onDrawn hook.
import { createPreviewEngine } from '../engine'
import { KIND_GAP } from '../timeline/programMap'
import { NO_PICTURE } from './barcode'
import { mulberry32, now, sleep } from './mseKit'
import {
  RecordingSink, cutsOf, drawnPaused, expectedCode, fmt, lookupOf, setup, type Fixture, type Result,
} from './engineProbe'

const q = new URLSearchParams(location.search)
const token = q.get('token') ?? 'none'
const scenario = q.get('scenario') ?? ''
const fixtureUrl = q.get('fixture') ?? '/fixture/timeline.json'

async function post(body: Result): Promise<void> {
  await fetch(`/__result/${token}`, { method: 'POST', body: JSON.stringify(body) })
}


// ------------------------------------------------------------ scenarios

const scenarios: Record<string, (fx: Fixture) => Promise<Result>> = {
  /** P1-F1: after each paused seek the canvas bar is the program's frame. */
  async paused_exact(fx) {
    const p = await setup(fx)
    const { engine } = p
    const pm = engine.program!
    const ks: number[] = []
    for (const c of cutsOf(pm)) for (const k of [c - 1, c]) if (k >= 0 && !ks.includes(k)) ks.push(k)
    const rnd = mulberry32(20260926)
    while (ks.length < 200) ks.push(Math.floor(rnd() * pm.total))
    // shuffle so seeks jump around (cold spans, class switches, backward)
    for (let i = ks.length - 1; i > 0; i--) { const j = Math.floor(rnd() * (i + 1)); [ks[i], ks[j]] = [ks[j], ks[i]] }
    await drawnPaused(engine, 0, 15000)
    const rows = []
    for (const k of ks.slice(0, 200)) {
      const t0 = now()
      engine.seek(k)
      const ok = await drawnPaused(engine, k)
      rows.push({ k, ok, ms: +(now() - t0).toFixed(1), exp: expectedCode(pm, k, p.srcIds), got: p.bars.get(k) ?? -9 })
    }
    const bad = rows.filter((r) => !r.ok || r.got !== r.exp).map((r) => ({ ...r, expS: fmt(r.exp), gotS: fmt(r.got) }))
    const ms = rows.map((r) => r.ms).sort((a, b) => a - b)
    return {
      total: pm.total, cuts: cutsOf(pm).length, seeks: rows.length, bad, gaps: rows.filter((r) => r.exp === NO_PICTURE).length,
      p50: ms[ms.length >> 1], p95: ms[Math.floor(ms.length * 0.95)], max: ms[ms.length - 1],
      lane: engine.internals.lane?.stats, store: engine.internals.store.stats, compositor: engine.internals.compositor?.stats,
      engineStats: engine.stats,
    }
  },

  /** P1-F2: play the whole program, reading the bar on every presented frame. */
  async playback(fx) {
    const p = await setup(fx)
    const { engine } = p
    const pm = engine.program!
    // let the window fill before playing (the 30 s window covers the fixture)
    await drawnPaused(engine, 0, 15000)
    const lane = engine.internals.lane!
    for (let i = 0; i < 400 && lane.buffered[1] < pm.total; i++) await sleep(25)
    const buffered = lane.buffered
    p.draws.length = 0
    const frameKs: number[] = []
    const offFrame = engine.on('frame', (f) => { if (f.playing) frameKs.push(f.k) })
    let waiting = 0
    engine.internals.video!.addEventListener('waiting', () => { waiting++ })
    // why playback stopped early, if it did (a pause WebKit made: the window
    // hidden or covered by another run on a busy machine)
    const stopLog: Result[] = []
    engine.on('pause-external', (e) => stopLog.push({ ev: 'pause-external', ...e, vis: document.visibilityState }))
    document.addEventListener('visibilitychange', () => stopLog.push({ ev: 'visibility', vis: document.visibilityState, k: engine.presentedK }))
    const t0 = now()
    engine.play()
    const deadline = t0 + (pm.total * pm.R.den / pm.R.num) * 1000 + 8000
    while (engine.playing && now() < deadline) await sleep(50)
    const elapsed = now() - t0
    offFrame()
    engine.pause()
    const drawn = p.draws.filter((d) => d.playing)
    const mismatches = drawn.filter((d) => d.bar !== expectedCode(pm, d.k, p.srcIds)).map((d) => [d.k, fmt(expectedCode(pm, d.k, p.srcIds)), fmt(d.bar)])
    const seen = new Set(frameKs)
    const first = frameKs[0] ?? 0
    const last = frameKs[frameKs.length - 1] ?? 0
    const missing: number[] = []
    for (let k = first; k <= last; k++) if (!seen.has(k)) missing.push(k)
    let monotonic = true
    for (let i = 1; i < frameKs.length; i++) if (frameKs[i] <= frameKs[i - 1]) monotonic = false
    const hm = [...engine.stats.handlerMs].sort((a, b) => a - b)
    const pct = (f: number) => (hm.length ? +hm[Math.min(hm.length - 1, Math.floor(hm.length * f))].toFixed(2) : null)
    return {
      handler: { p50: pct(0.5), p95: pct(0.95), p99: pct(0.99), max: pct(1), n: hm.length }, canvas: fx.canvas,
      total: pm.total, buffered, frames: frameKs.length, firstK: first, lastK: last, missing, mismatches, monotonic,
      waiting, held: engine.stats.heldFrames, elapsedMs: +elapsed.toFixed(0), sizeMismatches: engine.stats.sizeMismatches,
      maxUploadMs: engine.internals.compositor?.stats.maxUploadMs, maxDrawMs: engine.internals.compositor?.stats.maxDrawMs,
      sinkCalls: p.sink.calls.map((c) => c.op),
      externalPauses: engine.stats.externalPauses, stopLog,
    }
  },

  /** P1-F5: the span under the target is delayed 1 s by the server. */
  async last_good_frame(fx) {
    const p = await setup(fx)
    const { engine } = p
    const pm = engine.program!
    await drawnPaused(engine, 0, 15000)
    const comp = engine.internals.compositor!
    const snap = (engine.internals.canvas!.parentElement!.querySelectorAll('canvas')[1]) as HTMLCanvasElement
    const hashSnap = () => {
      const d = snap.getContext('2d')!.getImageData(0, 0, snap.width, snap.height).data
      let h = 2166136261
      for (let i = 0; i < d.length; i += 97) h = Math.imul(h ^ d[i], 16777619)
      return h >>> 0
    }
    // the delayed source frame's first output frame
    let target = -1
    for (let k = 0; k < pm.total; k++) {
      const d = fx.delayed!
      if (pm.kind[k] !== KIND_GAP && pm.sources[pm.srcKey[k]] === d.src && pm.srcFrame[k] >= d.first && pm.srcFrame[k] <= d.last) { target = k; break }
    }
    if (target < 0) throw new Error('delayed frame not in the program')
    const before = { draws: comp.stats.draws, hash: hashSnap(), k: engine.presentedK }
    const trace: Array<{ t: number; hash: number; draws: number; spinner: boolean; k: number }> = []
    const drawsFrom = p.draws.length
    const statusFrom = p.statuses.length
    const t0 = now()
    engine.seek(target)
    // timers, not rAF: a covered harness window pauses rAF, not timers
    while (now() - t0 < 4000 && engine.presentedK !== target) {
      await sleep(10)
      trace.push({ t: +(now() - t0).toFixed(1), hash: hashSnap(), draws: comp.stats.draws, spinner: engine.status.spinner, k: engine.presentedK })
    }
    const draws = p.draws.slice(drawsFrom)
    const firstDraw = draws.find((d) => d.k === target)
    const drawnAt = firstDraw ? +(firstDraw.at - t0).toFixed(1) : null
    const spinOn = p.statuses.slice(statusFrom).find((st) => st.spinner)
    const spinnerAt = spinOn ? +(spinOn.at - t0).toFixed(1) : null
    // nothing but the target may be drawn after the seek, and the held
    // picture (the pause snapshot) must not change until it is
    const changedEarly = [
      ...draws.filter((d) => d.k !== target).map((d) => ({ draw: d.k, at: d.at - t0 })),
      ...trace.filter((r) => r.k !== target && (r.hash !== before.hash || r.draws !== before.draws)),
    ]
    const after = p.bars.get(target)
    await sleep(50)
    return {
      target, before, spinnerAt, drawnAt, changedEarly: changedEarly.slice(0, 5), traceLen: trace.length,
      spinnerOffAfter: engine.status.spinner === false, bar: after, exp: expectedCode(pm, target, p.srcIds),
      store: engine.internals.store.stats,
    }
  },

  /** §3.5 buffering: playback runs into a span the server has not sent yet.
   *  Sound stops at the frame the picture stalls on, the spinner shows, and
   *  when the span lands picture and sound resume together, exact. */
  async buffering(fx) {
    const p = await setup(fx)
    const { engine, sink } = p
    const pm = engine.program!
    const from = Number(q.get('from') ?? 440)
    engine.seek(from)
    await drawnPaused(engine, from, 8000)
    const log: Result[] = []
    const t0 = now()
    engine.on('buffering', (b) => log.push({ at: +(now() - t0).toFixed(0), ev: 'buffering', ...b }))
    sink.calls.length = 0
    const drawsFrom = p.draws.length
    engine.play()
    const until = Math.min(pm.total - 1, from + 150)
    while (now() - t0 < 12000 && engine.playing && engine.presentedK < until) await sleep(20)
    engine.pause()
    const draws = p.draws.slice(drawsFrom).filter((d) => d.playing)
    const mismatches = draws.filter((d) => d.bar !== expectedCode(pm, d.k, p.srcIds)).map((d) => [d.k, fmt(d.bar)])
    return {
      log, from, reached: engine.presentedK, mismatches, drawn: draws.length,
      calls: sink.calls.map((c) => ({ op: c.op, k: c.k, at: +(c.at - t0).toFixed(0), sample: c.sample })),
      R: pm.R,
    }
  },

  /** P1-E1: media elements the engine creates over 100 edits (≤ 2). */
  async element_budget(fx) {
    let created = 0
    const orig = document.createElement.bind(document)
    document.createElement = ((tag: string, o?: ElementCreationOptions) => {
      const t = tag.toLowerCase()
      if (t === 'video' || t === 'audio') created++
      return orig(tag, o)
    }) as typeof document.createElement
    const p = await setup(fx)
    const { engine } = p
    await drawnPaused(engine, 0, 15000)
    const rnd = mulberry32(7)
    const v1 = (fx.edl.tracks ?? []).find((t) => t.id === 'v1')!
    const base = v1.clips as Array<Record<string, unknown>>
    let maxLive = 0
    const lookup = lookupOf(fx)
    for (let i = 0; i < 100; i++) {
      // a structural edit: drop, move or split one clip (ripple is not needed
      // for the element budget; the program just changes)
      const clips = base.map((c) => ({ ...c }))
      const j = Math.floor(rnd() * clips.length)
      const op = i % 3
      if (op === 0 && clips.length > 2) clips.splice(j, 1)
      else if (op === 1) clips[j].start = (clips[j].start as number) + 0.5
      else {
        const c = clips[j]
        const mid = ((c.in as number) + (c.out as number)) / 2
        clips.splice(j + 1, 0, { ...c, id: `${c.id}_r${i}`, in: mid, start: (c.start as number) + (mid - (c.in as number)) / (typeof c.speed === 'number' ? c.speed : 1) })
        clips[j].out = mid
      }
      const edl = { ...fx.edl, tracks: [{ ...v1, clips }] }
      engine.setTimeline(edl, `edit${i}`, lookup)
      if (i % 10 === 0) engine.seek(Math.floor(rnd() * (engine.program?.total ?? 1)))
      if (i === 50) { engine.play(); await sleep(400); engine.pause() }
      maxLive = Math.max(maxLive, document.querySelectorAll('video,audio').length)
      await sleep(5)
    }
    await sleep(200)
    maxLive = Math.max(maxLive, document.querySelectorAll('video,audio').length)
    document.createElement = orig
    return { created, maxLive, engineCreated: engine.stats.mediaElementsCreated }
  },

  /** Committed edits while PAUSED (the §4.1 path): each new program is
   *  on the canvas, exact, after the playhead frames are re-appended and the
   *  element re-seeked; edit → correct pixel is timed. */
  async paused_edits(fx) {
    const p = await setup(fx)
    const { engine } = p
    await drawnPaused(engine, 0, 15000)
    const lane = engine.internals.lane!
    for (let i = 0; i < 400 && lane.buffered[1] < (engine.program?.total ?? 0); i++) await sleep(25)
    const rnd = mulberry32(99)
    const v1 = (fx.edl.tracks ?? []).find((t) => t.id === 'v1')!
    const base = v1.clips as Array<Record<string, unknown>>
    const lookup = lookupOf(fx)
    const rows: Result[] = []
    let clips = base.map((c) => ({ ...c }))
    for (let i = 0; i < 40; i++) {
      // park on a random frame, then edit the program under it
      const total0 = engine.program!.total
      const k = Math.floor(rnd() * total0)
      engine.seek(k)
      await drawnPaused(engine, k)
      const next = clips.map((c) => ({ ...c }))
      const j = Math.floor(rnd() * next.length)
      const op = ['move', 'trim', 'delete', 'split', 'swap'][i % 5]
      if (op === 'move') next[j].start = Math.max(0, (next[j].start as number) + (rnd() < 0.5 ? -0.4 : 0.4))
      else if (op === 'trim') next[j].in = Math.min((next[j].out as number) - 0.2, (next[j].in as number) + 0.3)
      else if (op === 'delete' && next.length > 3) next.splice(j, 1)
      else if (op === 'split') {
        const c = next[j]
        const mid = ((c.in as number) + (c.out as number)) / 2
        next.splice(j + 1, 0, { ...c, id: `${c.id}s${i}`, in: mid, start: (c.start as number) + (mid - (c.in as number)) / (typeof c.speed === 'number' ? c.speed : 1) })
        next[j].out = mid
      } else {
        const o = next[(j + 1) % next.length]
        next[j] = { ...next[j], src: o.src, in: o.in, out: Math.min(o.out as number, (o.in as number) + ((next[j].out as number) - (next[j].in as number))) }
      }
      clips = next
      const t0 = now()
      const diff = engine.setTimeline({ ...fx.edl, tracks: [{ ...v1, clips }] }, `e${i}`, lookup)
      const pm = engine.program!
      const kk = Math.min(k, pm.total - 1)
      if (kk !== k) engine.seek(kk)
      const ok = await drawnPaused(engine, kk)
      rows.push({ i, op, k: kk, ok, ms: +(now() - t0).toFixed(1), dirty: diff.dirtyFrames.length, exp: expectedCode(pm, kk, p.srcIds), got: p.bars.get(kk) ?? -9 })
    }
    const bad = rows.filter((r) => !r.ok || r.got !== r.exp)
    const ms = rows.map((r) => r.ms as number).sort((a, b) => a - b)
    return { edits: rows.length, bad, p50: ms[ms.length >> 1], p95: ms[Math.floor(ms.length * 0.95)], max: ms[ms.length - 1], lane: lane.stats }
  },

  /** A pause the engine did not issue (WebKit pausing the muted element when
   *  the page is hidden): sound stops at the same k, the clock stops, the
   *  state shows paused, and picture + sound resume TOGETHER on return. */
  async external_pause(fx) {
    const p = await setup(fx)
    const { engine, sink } = p
    await drawnPaused(engine, 0, 15000)
    const lane = engine.internals.lane!
    for (let i = 0; i < 400 && lane.buffered[1] < 300; i++) await sleep(25)
    const video = engine.internals.video!
    const events: Result[] = []
    engine.on('pause-external', (e) => events.push({ ...e, at: now() }))
    // (1) the element paused by someone else (WebKit's own media policy)
    engine.play()
    await sleep(800)
    const kBefore = engine.presentedK
    sink.calls.length = 0
    video.pause()
    await sleep(400)
    const afterElement = {
      playing: engine.playing, presentedK: engine.presentedK, kBefore, clock: engine.clock.now(),
      stops: sink.calls.filter((c) => c.op === 'stop').map((c) => c.k), starts: sink.calls.filter((c) => c.op === 'start').length,
      status: engine.status.playing,
    }
    await sleep(300)
    const clockStill = Math.abs(engine.clock.now() - afterElement.clock) < 1e-9
    // (2) the page goes hidden while playing, then visible again
    engine.play()
    await sleep(600)
    sink.calls.length = 0
    const vis = { state: 'visible' as DocumentVisibilityState }
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => vis.state })
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => vis.state === 'hidden' })
    vis.state = 'hidden'
    document.dispatchEvent(new Event('visibilitychange'))
    const kHidden = engine.presentedK
    await sleep(500)
    const whileHidden = {
      playing: engine.playing, videoPaused: video.paused, presentedK: engine.presentedK, kHidden,
      stops: sink.calls.filter((c) => c.op === 'stop').map((c) => ({ k: c.k, playing: c.playing })),
      startsWhileHidden: sink.calls.filter((c) => c.op === 'start').length,
    }
    sink.calls.length = 0
    vis.state = 'visible'
    document.dispatchEvent(new Event('visibilitychange'))
    await sleep(700)
    const resumed = {
      playing: engine.playing, videoPaused: video.paused,
      starts: sink.calls.filter((c) => c.op === 'start').map((c) => ({ k: c.k, sample: c.sample })),
      presentedK: engine.presentedK, kHidden,
    }
    engine.pause()
    delete (document as unknown as Record<string, unknown>).visibilityState
    delete (document as unknown as Record<string, unknown>).hidden
    return { events, afterElement, clockStill, whileHidden, resumed, R: engine.program!.R }
  },

  /** The REAL thing: the harness orders the WK window out (the page becomes
   *  hidden, as on a Space switch or minimise), then back in. */
  async real_hide(fx) {
    const selfPause = q.get('selfPause') !== '0'
    const host = document.createElement('div')
    host.style.cssText = `position:relative;width:${fx.canvas[0]}px;height:${fx.canvas[1]}px;`
    document.body.appendChild(host)
    const sink = new RecordingSink()
    const engine = createPreviewEngine({ audioSink: sink, canvasSize: { w: fx.canvas[0], h: fx.canvas[1] }, pauseOnHidden: selfPause })
    sink.engine = engine
    engine.attach(host)
    engine.setTimeline(fx.edl, 'fixture', lookupOf(fx))
    await drawnPaused(engine, 0, 15000)
    const lane = engine.internals.lane!
    for (let i = 0; i < 400 && lane.buffered[1] < 300; i++) await sleep(25)
    const video = engine.internals.video!
    const log: Result[] = []
    const t0 = now()
    const at = () => +(now() - t0).toFixed(0)
    document.addEventListener('visibilitychange', () => log.push({ at: at(), ev: 'visibility', state: document.visibilityState, videoPaused: video.paused, playing: engine.playing }))
    video.addEventListener('pause', () => log.push({ at: at(), ev: 'video-pause', visibility: document.visibilityState }))
    video.addEventListener('play', () => log.push({ at: at(), ev: 'video-play', visibility: document.visibilityState }))
    engine.on('pause-external', (e) => log.push({ at: at(), ev: 'pause-external', ...e }))
    engine.play()
    await sleep(800)
    sink.calls.length = 0
    const kBefore = engine.presentedK
    // the environment (another WK run's window landing on our 4 px slot)
    // may hide the page BEFORE our own request: that run proves nothing
    const hideAt = at()
    const playingAtHide = engine.playing && !video.paused && document.visibilityState === 'visible'
    const envBeforeHide = !playingAtHide || log.some((e) => e.ev === 'visibility' || e.ev === 'pause-external' || e.ev === 'video-pause')
    await fetch(`/__window/${token}/hide`, { method: 'POST' })
    for (let i = 0; i < 60 && document.visibilityState !== 'hidden'; i++) await sleep(50)
    await sleep(1200)
    const whileHidden = {
      visibility: document.visibilityState, playing: engine.playing, videoPaused: video.paused, presentedK: engine.presentedK,
      kBefore, currentTimeK: Math.round(video.currentTime * engine.program!.R.num / engine.program!.R.den),
      calls: sink.calls.map((c) => ({ op: c.op, k: c.k, playing: c.playing })),
    }
    sink.calls.length = 0
    await fetch(`/__window/${token}/show`, { method: 'POST' })
    for (let i = 0; i < 60 && document.visibilityState !== 'visible'; i++) await sleep(50)
    await sleep(1000)
    const afterShow = {
      visibility: document.visibilityState, playing: engine.playing, videoPaused: video.paused, presentedK: engine.presentedK,
      calls: sink.calls.map((c) => ({ op: c.op, k: c.k, sample: c.sample })),
    }
    engine.pause()
    return { selfPause, log, whileHidden, afterShow, R: engine.program!.R, hideAt, playingAtHide, envBeforeHide }
  },

  /** webglcontextlost → the 2D snapshot shows the last frame; restored →
   *  rebuilt and redrawn, exact. */
  async context_loss(fx) {
    const p = await setup(fx)
    const { engine } = p
    const pm = engine.program!
    const k = Math.min(pm.total - 1, 75)
    await drawnPaused(engine, 0, 15000)
    engine.seek(k)
    const shown = await drawnPaused(engine, k)
    const comp = engine.internals.compositor!
    const gl = comp.context!
    const ext = gl.getExtension('WEBGL_lose_context')
    if (!ext) return { skipped: 'no WEBGL_lose_context' }
    const drawsBefore = comp.stats.draws
    ext.loseContext()
    await sleep(100)
    const snap = comp.snapshotCanvas!
    const lostState = { lost: comp.lost, snapshotVisible: snap.style.visibility, glVisible: engine.internals.canvas!.style.visibility, mode: engine.status.mode }
    const d = snap.getContext('2d')!.getImageData(0, 0, snap.width, snap.height).data
    let lit = 0
    for (let i = 0; i < d.length; i += 4) if (d[i] + d[i + 1] + d[i + 2] > 60) lit++
    ext.restoreContext()
    const redrawn = await new Promise<boolean>((resolve) => {
      const off = engine.on('frame', (f) => { if (f.k === k) { off(); resolve(true) } })
      setTimeout(() => { off(); resolve(false) }, 3000)
    })
    return {
      k, shown, lostState, snapshotLitFraction: lit / (d.length / 4), redrawn, drawsAfter: comp.stats.draws - drawsBefore,
      bar: p.bars.get(k), exp: expectedCode(pm, k, p.srcIds), glVisibleAfter: engine.internals.canvas!.style.visibility,
      mode: engine.status.mode, restored: comp.stats.restored,
    }
  },

  /** What WebKit's texImage2D(video) really uploads for each proxy size:
   *  GLSL textureSize() of the uploaded texture vs the element's and a
   *  VideoFrame's sizes (coded vs visible). */
  async texprobe(fx) {
    const p = await setup(fx)
    const { engine } = p
    const pm = engine.program!
    await drawnPaused(engine, 0, 15000)
    const video = engine.internals.video!
    const gl = document.createElement('canvas').getContext('webgl2')!
    const tex = gl.createTexture()
    const vs = `#version 300 es
void main(){ vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2)); gl_Position = vec4(p*2.0-1.0,0.0,1.0); }`
    const fs = `#version 300 es
precision highp float; uniform sampler2D t; out vec4 o;
void main(){ ivec2 s = textureSize(t, 0); o = vec4(float(s.x & 255), float(s.x >> 8), float(s.y & 255), float(s.y >> 8)) / 255.0; }`
    const prog = gl.createProgram()!
    for (const [ty, src] of [[gl.VERTEX_SHADER, vs], [gl.FRAGMENT_SHADER, fs]] as const) {
      const sh = gl.createShader(ty)!; gl.shaderSource(sh, src); gl.compileShader(sh); gl.attachShader(prog, sh)
    }
    gl.linkProgram(prog)
    const out: Result[] = []
    const seen = new Set<string>()
    for (let k = 0; k < pm.total; k++) {
      if (pm.kind[k] === KIND_GAP) continue
      const src = pm.sources[pm.srcKey[k]]
      if (seen.has(src)) continue
      seen.add(src)
      engine.seek(k)
      await drawnPaused(engine, k)
      gl.bindTexture(gl.TEXTURE_2D, tex)
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, video)
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST)
      gl.useProgram(prog)
      gl.drawArrays(gl.TRIANGLES, 0, 3)
      const px = new Uint8Array(4)
      gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, px)
      let vf: Result
      try {
        const f = new (window as unknown as { VideoFrame: new (v: HTMLVideoElement) => { codedWidth: number; codedHeight: number; visibleRect: DOMRectReadOnly | null; displayWidth: number; displayHeight: number; close(): void } }).VideoFrame(video)
        vf = { coded: [f.codedWidth, f.codedHeight], visible: f.visibleRect ? [f.visibleRect.x, f.visibleRect.y, f.visibleRect.width, f.visibleRect.height] : null, display: [f.displayWidth, f.displayHeight] }
        f.close()
      } catch (e) { vf = { error: String(e) } }
      out.push({ src, k, element: [video.videoWidth, video.videoHeight], texture: [px[0] + px[1] * 256, px[2] + px[3] * 256], videoFrame: vf })
    }
    return { probes: out }
  },

  /** Geometry parity: the canvas Y plane (BT.709 limited) at each k of each
   *  group, for PSNR against the server render of the same EDL. */
  async geometry(fx) {
    const mip = q.get('mipmaps') !== '0'
    const p = await setup({ ...fx, edl: fx.groups![0].edl }, { mipmaps: mip })
    const { engine } = p
    const [cw, ch] = fx.canvas
    const out: Result[] = []
    let grab: { k: number; y: Uint8Array } | null
    engine.internals.compositor!.onDrawn = (info, gl) => {
      const rgba = new Uint8Array(cw * ch * 4)
      gl.readPixels(0, 0, cw, ch, gl.RGBA, gl.UNSIGNED_BYTE, rgba)
      const y = new Uint8Array(cw * ch)
      for (let r = 0; r < ch; r++) {
        const src = (ch - 1 - r) * cw * 4
        for (let x = 0; x < cw; x++) {
          const i = src + x * 4
          y[r * cw + x] = Math.round(16 + (219 / 255) * (0.2126 * rgba[i] + 0.7152 * rgba[i + 1] + 0.0722 * rgba[i + 2]))
        }
      }
      grab = { k: info.k, y }
    }
    for (const [gi, g] of fx.groups!.entries()) {
      if (gi > 0) engine.setTimeline(g.edl, g.name, lookupOf(fx))
      for (const k of g.ks) {
        grab = null
        engine.seek(k)
        const ok = await drawnPaused(engine, k, 8000)
        // already on screen (drawn before the reset): draw it again to read it
        if (ok && (grab as { k: number } | null)?.k !== k) engine.internals.redraw()
        const g2 = grab as { k: number; y: Uint8Array } | null
        let b64 = ''
        if (g2 && g2.k === k) {
          let s = ''
          for (let i = 0; i < g2.y.length; i += 0x8000) s += String.fromCharCode(...g2.y.subarray(i, i + 0x8000))
          b64 = btoa(s)
        }
        out.push({ group: g.name, k, ok, y: b64 })
      }
    }
    // leave one frame on screen for a screenshot (?hold=group:k)
    const hold = (q.get('hold') ?? '').split(':')
    const hg = fx.groups!.find((g) => g.name === hold[0])
    if (hg) {
      engine.setTimeline(hg.edl, `${hg.name}-hold`, lookupOf(fx))
      engine.seek(Number(hold[1]))
      await drawnPaused(engine, Number(hold[1]), 8000)
    }
    return { frames: out, canvas: fx.canvas, mipmaps: mip }
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
