// PreviewController (spec §3.5, §4.1, §7, §8.2): the app's side of the
// engine. A fake engine records what it is told and builds a REAL program
// map; a scripted fetch plays the server's routes (proxy lookup, frame_map
// with its sources table, dispatch get_timeline).
import { describe, expect, it, vi } from 'vitest'
import { PreviewController } from './previewController'
import type { ClientPreviewEngine, EngineOptions, EngineSourceLookup, EngineStatus } from './engine'
import type { EdlLike } from './timeline/framePlan'
import { buildProgramMap, toRle, type ProgramMap } from './timeline/programMap'
import { MODE_BAKED, MODE_EXACT, type ModeRange } from './timeline/support'
import { defaultTimeBase } from './timeline/frameMap'

const SID = 's_test'
const H1 = '0123456789abcdef'
const H2 = 'fedcba9876543210'
const INFO = { rate: [30, 1], tb: [1, 15360], frames: 300, start_ticks: 0, w: 1280, h: 720 }

function edl(clips: Array<[string, number, number, number]>): EdlLike {
  const end = Math.max(...clips.map(([, a, b, s]) => s + (b - a)))
  return {
    duration: end, canvas: { w: 1280, h: 720, fps: 30 },
    tracks: [{ id: 'v1', clips: clips.map(([src, a, b, start], i) => ({ id: `c${i}`, src, in: a, out: b, start })) }],
  }
}

type Listener = (e: unknown) => void

/** Records the engine calls; builds a real map from the lookup it gets. */
class FakeEngine {
  readonly calls: Array<[string, ...unknown[]]> = []
  readonly listeners = new Map<string, Set<Listener>>()
  program: ProgramMap | null = null
  playing = false
  targetK = 0
  presentedK = 0
  lastLookup: EngineSourceLookup | null = null
  ranges: ModeRange[] = []
  mode: 'client' | 'server' = 'client'
  baked = new Set<number>()
  clock = { now: () => this.presentedK / 30 }
  readonly opts: EngineOptions
  constructor(opts: EngineOptions) { this.opts = opts }
  on(ev: string, cb: Listener) {
    let s = this.listeners.get(ev)
    if (!s) this.listeners.set(ev, (s = new Set()))
    s.add(cb)
    return () => s!.delete(cb)
  }
  emit(ev: string, e: unknown) { for (const cb of this.listeners.get(ev) ?? []) cb(e) }
  attach(host: unknown) { this.calls.push(['attach', host]) }
  get status(): EngineStatus {
    return { mode: this.mode, reason: this.mode === 'server' ? 'no-mse' : null, playing: this.playing, buffering: false,
      spinner: false, presentedK: this.presentedK, total: this.program?.total ?? 0, ranges: this.ranges }
  }
  setTimeline(e: EdlLike, h: string, lookup: EngineSourceLookup) {
    this.lastLookup = lookup
    this.calls.push(['setTimeline', h])
    this.program = buildProgramMap(e, (src) => lookup(src)?.info ?? {
      rate: { num: 30, den: 1 }, tb: defaultTimeBase({ num: 30, den: 1 }), frames: 1 << 21, startTicks: 0, w: 1280, h: 720,
    })
    return { dirtyFrames: [], dirtyParams: new Set<string>() }
  }
  play() { this.calls.push(['play', this.targetK]); this.playing = true }
  pause() { this.calls.push(['pause']); this.playing = false }
  seek(k: number) { this.calls.push(['seek', k]); this.targetK = k }
  spliceBake(h: string) { this.calls.push(['spliceBake', h]); return true }
  setDemoted(h: string, r: unknown) { this.calls.push(['setDemoted', h, r]) }
  isBakedFrame(k: number) { return this.baked.has(k) }
  pauseExternal() { this.calls.push(['pauseExternal']) }
  destroy() { this.calls.push(['destroy']) }
  named(op: string) { return this.calls.filter((c) => c[0] === op) }
}

interface Reply { status: number; body?: unknown; headers?: Record<string, string> }

function server(routes: Record<string, (url: string, init?: RequestInit) => Reply>) {
  const log: string[] = []
  const f = vi.fn(async (url: string, init?: RequestInit) => {
    log.push(`${init?.method ?? 'GET'} ${url}`)
    const key = Object.keys(routes).find((k) => url.includes(k))
    const r = key ? routes[key](url, init) : { status: 404, body: {} }
    return new Response(JSON.stringify(r.body ?? {}), { status: r.status, headers: r.headers })
  })
  return { fetch: f as unknown as typeof fetch, log }
}

const settle = async (n = 20) => { for (let i = 0; i < n; i++) await new Promise((r) => setTimeout(r, 0)) }

function setup(routes: Record<string, (url: string, init?: RequestInit) => Reply>, extra = {}) {
  const srv = server(routes)
  let engine: FakeEngine | null = null
  const events = { playing: [] as boolean[], fallback: [] as string[], adopted: [] as string[] }
  const ctl = new PreviewController({
    sessionId: SID, fetch: srv.fetch, audio: false, sourceRetryMs: 1,
    createEngine: (o) => (engine = new FakeEngine(o)) as unknown as ClientPreviewEngine,
    onPlaying: (p) => events.playing.push(p),
    onFallback: (r) => events.fallback.push(r),
    onServerTimeline: (_e, h) => events.adopted.push(h),
    ...extra,
  })
  ctl.attach({} as HTMLElement)
  return { ctl, srv, engine: () => engine!, events }
}

const proxyRoute = () => ({ status: 200, body: { key: 'k'.repeat(24), state: 'ready', frames: 300, w: 1280, h: 720, src_rate: { num: 30, den: 1 } } })

describe('PreviewController', () => {
  it('hands a dispatch answer to the engine at once, with its render hash', () => {
    const { ctl, engine } = setup({ '/proxy?': proxyRoute, '/frame_map': () => ({ status: 202, headers: { 'Retry-After': '5' } }) })
    const e = edl([['A', 0, 2, 0]])
    ctl.applyTimeline(e, H1)
    expect(engine().named('setTimeline')).toEqual([['setTimeline', H1]])
    expect(ctl.renderHash).toBe(H1)
    expect(ctl.appliedEdl).toBe(e)
  })

  it('looks an unknown source up once and re-applies the timeline with its proxy', async () => {
    const { ctl, engine, srv } = setup({ '/proxy?': proxyRoute, '/frame_map': () => ({ status: 409, body: {} }) })
    ctl.applyTimeline(edl([['A', 0, 2, 0], ['A', 3, 4, 2]]), H1)
    expect(engine().lastLookup!('A')).toBeNull()      // PENDING until known
    await settle()
    expect(srv.log.filter((l) => l.includes('/proxy?src=A'))).toHaveLength(1)
    expect(engine().named('setTimeline').length).toBe(2)
    expect(engine().lastLookup!('A')?.proxy?.key).toBe('k'.repeat(24))
    expect(engine().lastLookup!('A')?.info.frames).toBe(300)
  })

  it('names each source\'s master for the degraded tier (§7): the session file URL of its upload', async () => {
    const { ctl, engine } = setup({ '/proxy?': () => ({ status: 200, body: { key: 'k'.repeat(24), state: 'failed' } }), '/frame_map': () => ({ status: 409, body: {} }) })
    const src = `/wd/${SID}/uploads/clip one/clip one.normalized.mp4`
    ctl.applyTimeline(edl([[src, 0, 2, 0]]), H1)
    await settle()
    const s = engine().lastLookup!(src)
    expect(s?.proxy?.state).toBe('failed')
    expect(s?.media).toBe(`/api/sessions/${SID}/files/uploads/clip%20one/clip%20one.normalized.mp4`)
  })

  it('absorbs the frame_map sources BEFORE comparing, so a match is a match', async () => {
    const e = edl([['A', 0.5, 2, 0], ['A', 3, 4, 1.5]])
    const lookup = () => ({ rate: { num: 30, den: 1 }, tb: { num: 1, den: 15360 }, frames: 300, startTicks: 0, w: 1280, h: 720 })
    const runs = toRle(buildProgramMap(e, lookup))
    const pm = buildProgramMap(e, lookup)
    const { ctl, engine, srv } = setup({
      '/proxy?': () => ({ status: 202 }),     // proxy still probing: only frame_map knows the source
      '/frame_map': () => ({ status: 200, body: { version: 1, render_hash: H1, R: [30, 1], T: 8000, total: pm.total, runs, sources: { A: INFO } } }),
    })
    ctl.applyTimeline(e, H1)
    await settle(40)
    expect(srv.log.filter((l) => l.includes('/frame_map'))).toHaveLength(1)   // one fetch serves both
    expect(ctl.divergence()).toMatchObject({ checked: 1, mismatched: 0 })
    expect(engine().named('setDemoted')).toEqual([])
  })

  it('demotes a disagreeing range to BAKED and splices the bake when the render has landed', async () => {
    const e = edl([['A', 0, 2, 0]])
    const lookup = () => ({ rate: { num: 30, den: 1 }, tb: { num: 1, den: 15360 }, frames: 300, startTicks: 0, w: 1280, h: 720 })
    // the server's map: same first 10 frames, then source frames 5 later
    const server = { ...e, tracks: [{ id: 'v1', clips: [
      { id: 'c0', src: 'A', in: 0, out: 1 / 3, start: 0 }, { id: 'c1', src: 'A', in: 1 / 3 + 1 / 6, out: 2 + 1 / 6, start: 1 / 3 },
    ] }] }
    const runs = toRle(buildProgramMap(server, lookup))
    const { ctl, engine } = setup({
      '/proxy?': proxyRoute,
      '/frame_map': () => ({ status: 200, body: { version: 1, render_hash: H1, R: [30, 1], T: 8000, total: 60, runs, sources: { A: INFO } } }),
    })
    ctl.onPreviewLanded(H1)
    ctl.applyTimeline(e, H1)
    await settle(40)
    const dem = engine().named('setDemoted')
    expect(dem).toHaveLength(1)
    expect(dem[0][1]).toBe(H1)
    expect(dem[0][2]).toEqual([[10, 60]])
    expect(engine().named('spliceBake').map((c) => c[1])).toContain(H1)
  })

  it('splices only the bake of the CURRENT hash', () => {
    const { ctl, engine } = setup({ '/proxy?': proxyRoute, '/frame_map': () => ({ status: 409, body: {} }) })
    ctl.applyTimeline(edl([['A', 0, 2, 0]]), H1)
    ctl.onPreviewLanded(H2)
    expect(engine().named('spliceBake')).toEqual([])
    ctl.onPreviewLanded(H1)
    expect(engine().named('spliceBake')).toEqual([['spliceBake', H1]])
  })

  it('learns the hash of an EDL that came from elsewhere (read-only get_timeline, include=edl)', async () => {
    const fresh = edl([['A', 0, 3, 0]])
    let asked: unknown = null
    const { ctl, engine, events } = setup({
      '/dispatch?include=edl': (_u, init) => {
        asked = JSON.parse(String(init?.body))
        return { status: 200, body: { result: {}, edl_hash: 'e1', render_hash: H2, edl: fresh } }
      },
      '/proxy?': proxyRoute,
      '/frame_map': () => ({ status: 409, body: {} }),
    })
    ctl.applyTimeline(edl([['A', 0, 2, 0]]), null)
    expect(engine().named('setTimeline')).toEqual([['setTimeline', '']])   // shown at once, hashless
    await settle()
    expect(asked).toEqual({ tool: 'get_timeline', args: { summary: true } })
    expect(events.adopted).toEqual(['e1'])
    expect(ctl.renderHash).toBe(H2)
    expect(ctl.appliedEdl).toEqual(fresh)
  })

  it('plays synchronously, from the store playhead, inside the caller', () => {
    const { ctl, engine } = setup({ '/proxy?': proxyRoute, '/frame_map': () => ({ status: 409, body: {} }) })
    ctl.applyTimeline(edl([['A', 0, 5, 0]]), H1)
    const ok = ctl.play(2.0)
    // no await: the engine was told in the same task
    expect(engine().calls.slice(-2)).toEqual([['seek', 60], ['play', 60]])
    expect(ok).toBe(true)
    ctl.pause()
    expect(engine().calls.at(-1)).toEqual(['pause'])
  })

  it('reports play-state changes the engine made itself, and falls back when it cannot run', () => {
    const { ctl, engine, events } = setup({ '/proxy?': proxyRoute, '/frame_map': () => ({ status: 409, body: {} }) })
    ctl.applyTimeline(edl([['A', 0, 5, 0]]), H1)
    const eng = engine()
    eng.playing = true
    eng.emit('status', eng.status)
    eng.playing = false
    eng.emit('status', eng.status)          // e.g. WebKit paused the element
    expect(events.playing).toEqual([true, false])
    eng.mode = 'server'
    eng.emit('status', eng.status)
    expect(events.fallback).toEqual(['no-mse'])
  })

  it('says what the corner spinner waits for: a BAKED frame still showing RAW frames', () => {
    const views: string[] = []
    const { ctl, engine } = setup({ '/proxy?': proxyRoute, '/frame_map': () => ({ status: 409, body: {} }) },
      { onView: (v: { wait: string | null }) => views.push(String(v.wait)) })
    ctl.applyTimeline(edl([['A', 0, 5, 0]]), H1)
    const eng = engine()
    eng.ranges = [{ k0: 0, k1: 30, mode: MODE_EXACT, reasons: [] }, { k0: 30, k1: 150, mode: MODE_BAKED, reasons: ['effect:color'] }]
    eng.targetK = 40
    expect(ctl.view().wait).toBe('baking')
    eng.baked.add(40)
    expect(ctl.view().wait).toBeNull()
    eng.targetK = 10
    expect(ctl.view().wait).toBeNull()
    expect(ctl.needsBake()).toBe(true)
  })

  it('hands the engine a bake base URL for its session and routes an interrupted AudioContext to an external pause', () => {
    let interrupt: (() => void) | null = null
    const { engine } = setup({}, {
      audio: (onInt: () => void) => {
        interrupt = onInt
        return { prepare() {}, start() {}, stop() {}, reschedule() {}, setParams() {}, ctxTimeAt: () => null }
      },
    })
    expect(engine().opts.bakeBaseUrl).toBe(`/api/sessions/${SID}/bake`)
    interrupt!()
    expect(engine().named('pauseExternal')).toHaveLength(1)
  })

  // Final QA r3: the Instant preview played ~11 dB under the server preview
  // and the export (the project's −16 LUFS target was never applied) and
  // called it EXACT. The controller asks /preview_loudness for each render
  // hash, hands the gain to the sound, and says whether it is current.
  it('plays the server preview\'s loudness gain for each hash, current once that preview has landed', async () => {
    const loud = edl([['A', 0, 4, 0]])
    loud.canvas = { ...loud.canvas, loudness_lufs: -16 }
    const answers: Record<string, unknown> = { [H1]: { gain_db: 9.5, current: false, target_lufs: -16 } }
    let source: { gainDb(): number | null; current(h: string): boolean } | null = null
    const refresh = vi.fn()
    const { ctl, srv, engine } = setup({
      '/proxy?': proxyRoute, '/frame_map': () => ({ status: 409, body: {} }),
      '/preview_loudness': (url) => ({ status: 200, body: answers[new URL(url, 'http://x').searchParams.get('h')!] }),
    }, {
      audio: (_onInt: () => void, l: typeof source) => {
        source = l
        return { prepare() {}, start() {}, stop() {}, reschedule() {}, setParams() {}, ctxTimeAt: () => null, refreshLoudness: refresh }
      },
    })
    ctl.applyTimeline(loud, H1)
    await settle()
    expect(srv.log).toContain(`GET /api/sessions/${SID}/preview_loudness?h=${H1}`)
    expect(source!.gainDb()).toBe(9.5)                 // the last-known gain: played…
    expect(source!.current(H1)).toBe(false)            // …but APPROX
    expect(refresh).toHaveBeenCalledTimes(1)
    // the server preview of H1 lands: its measured gain is current
    answers[H1] = { gain_db: 10.8, current: true, target_lufs: -16 }
    ctl.onPreviewLanded(H1)
    await settle()
    expect(source!.gainDb()).toBe(10.8)
    expect(source!.current(H1)).toBe(true)
    expect(source!.current(H2)).toBe(false)
    expect(refresh).toHaveBeenCalledTimes(2)
    // a new hash keeps the verdict while its answer is on the way (no ≈
    // flash after a title edit), then takes the answer
    answers[H2] = { gain_db: 10.8, current: true, target_lufs: -16 }
    let seenByEngine: boolean | null = null
    const eng = engine()
    const setTimeline = eng.setTimeline.bind(eng)
    eng.setTimeline = (e, h, l) => { seenByEngine = source!.current(h); return setTimeline(e, h, l) }
    ctl.applyTimeline(loud, H2)
    expect(seenByEngine).toBe(true)                    // what the engine classified the new program with
    expect(source!.current(H2)).toBe(true)
    await settle()
    expect(source!.current(H2)).toBe(true)
    expect(refresh).toHaveBeenCalledTimes(2)           // nothing changed: no re-plan
  })

  it('asks for no loudness gain on a project without a target', async () => {
    const { ctl, srv } = setup({ '/proxy?': proxyRoute, '/frame_map': () => ({ status: 409, body: {} }) })
    ctl.applyTimeline(edl([['A', 0, 4, 0]]), H1)
    await settle()
    expect(srv.log.some((l) => l.includes('/preview_loudness'))).toBe(false)
  })

  it('keeps the timeline across a remount and destroys the engine on detach', () => {
    const { ctl, engine } = setup({ '/proxy?': proxyRoute, '/frame_map': () => ({ status: 409, body: {} }) })
    ctl.applyTimeline(edl([['A', 0, 5, 0]]), H1)
    const first = engine()
    ctl.detach()
    expect(first.named('destroy')).toHaveLength(1)
    ctl.attach({} as HTMLElement)
    expect(engine()).not.toBe(first)
    expect(engine().named('setTimeline')).toEqual([['setTimeline', H1]])
  })
})
