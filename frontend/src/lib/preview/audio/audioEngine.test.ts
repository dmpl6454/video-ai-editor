// The AudioSink on a recording fake context (the live sound itself is tapped
// in WebKit by tests/wk/test_wk_audio.py): start in the gesture (resume),
// the 4 s window refilled every second, stop's 5 ms ramp and the suspend
// 20 ms after, edits while playing (default point, reschedule's point,
// setParams now), a pause the engine did not issue, the output clock.
import { describe, expect, it } from 'vitest'
import type { AudioProgramInfo } from '../engine'
import type { EdlLike } from '../timeline/framePlan'
import type { SourceInfo } from '../timeline/frameMap'
import { audioPlacements, buildProgramMap } from '../timeline/programMap'
import { AudioChunks } from './audioChunks'
import { AudioEngine, REFILL_S, WINDOW_S } from './audioEngine'
import { asCtx, FakeContext, type FakeParam } from './fakeAudio'
import { RAMP_S } from './mixGraph'

const SR = 48000
const SRC: SourceInfo = { rate: { num: 30, den: 1 }, tb: { num: 1, den: 15360 }, frames: 30 * 600, startTicks: 0, w: 64, h: 36 }
const CS = 48000

function fakeChunks() {
  const fetch = async (url: string): Promise<Response> => {
    if (url.endsWith('index.json')) return new Response(JSON.stringify({ audio: { chunk_samples: CS, samples: 60 * CS, chunks: 60 } }))
    const n = Number(/a\/(\d+)\.flac$/.exec(url)![1])
    return new Response(new Uint32Array([n]).buffer)
  }
  const decode = async (bytes: ArrayBuffer): Promise<AudioBuffer> => {
    const [n] = new Uint32Array(bytes)
    const L = Float32Array.from({ length: CS }, (_v, i) => (n * CS + i) / 1e7)
    return { sampleRate: SR, numberOfChannels: 2, length: CS, getChannelData: () => L } as unknown as AudioBuffer
  }
  return new AudioChunks({ fetch, decode })
}

function edl(v1: Array<Record<string, unknown>>, music: Array<Record<string, unknown>> = []): EdlLike {
  const clip = (c: Record<string, unknown>) => ({ src: '/a.mp4', in: 0, speed: null, reverse: false, audio: {}, ...c })
  const e: EdlLike = { canvas: { fps: 30 }, tracks: [
    { id: 'v1', type: 'video', clips: v1.map(clip), transitions: [] },
    { id: 'music', type: 'music', clips: music.map(clip), transitions: [] }] }
  let d = 0
  for (const t of e.tracks!) for (const c of t.clips as Array<{ start: number; in?: number; out: number }>) d = Math.max(d, c.start + c.out - (c.in ?? 0))
  e.duration = d
  return e
}

const info: AudioProgramInfo = {
  R: { num: 30, den: 1 }, totalFrames: 0, renderHash: 'h',
  lookup: (src) => ({ info: SRC, proxy: { key: src === '/bed.m4a' ? 'bbbb' : 'aaaa' } }),
}

function setup(opts: { interrupted?: (s: string) => void } = {}) {
  const ctx = new FakeContext()
  const timers: Array<{ fn: () => void; ms: number }> = []
  const intervals: Array<{ fn: () => void; ms: number; cleared: boolean }> = []
  const engine = new AudioEngine({
    chunks: fakeChunks(), createContext: () => asCtx(ctx), now: () => 1000 * ctx.currentTime,
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length },
    setInterval: (fn, ms) => { const h = { fn, ms, cleared: false }; intervals.push(h); return h },
    clearInterval: (h) => { (h as { cleared: boolean }).cleared = true },
    onInterrupted: opts.interrupted,
  })
  const prepare = (e: EdlLike) => engine.prepare(e, audioPlacements(buildProgramMap(e, () => SRC), () => SRC), info)
  return { ctx, engine, timers, intervals, prepare }
}

async function loaded(engine: AudioEngine, ...srcs: string[]) {
  for (const s of srcs.length ? srcs : ['/a.mp4']) await engine.reader.load(s, 0, 30 * CS)
}

const base = edl([{ id: 'a', start: 0, out: 20 }])

describe('transport', () => {
  it('start resumes the context in the same call and schedules from the anchor through +4 s', async () => {
    const { ctx, engine, prepare, intervals } = setup()
    prepare(base)
    await loaded(engine)
    expect(ctx.sources).toHaveLength(0)
    ctx.currentTime = 2
    engine.start(2.1, 24000)
    expect(ctx.resumes).toBe(1)
    const starts = ctx.sources.map((s) => s.startedAt!)
    expect(Math.min(...starts)).toBeCloseTo(2.1, 9)
    const covered = ctx.sources.reduce((n, s) => n + s.buffer!.length, 0)
    expect(covered).toBeGreaterThanOrEqual(WINDOW_S * SR - 24000)
    expect(intervals.map((i) => i.ms)).toEqual([REFILL_S * 1000])
    expect(engine.isRunning).toBe(true)
  })

  it('refills the window every second as the context advances', async () => {
    const { ctx, engine, prepare, intervals } = setup()
    prepare(base)
    await loaded(engine)
    engine.start(0.05, 0)
    const until = () => Math.max(...ctx.sources.map((s) => Math.round((s.startedAt! - 0.05) * SR) + s.buffer!.length))
    const u0 = until()
    ctx.currentTime = 1.05
    intervals[0].fn()
    expect(until()).toBeGreaterThanOrEqual(u0 + SR)
    expect(until()).toBeGreaterThanOrEqual(Math.round(1.0 * SR) + WINDOW_S * SR)
  })

  it('stop ramps to silence over its ramp, stops the sources, and suspends 20 ms later', async () => {
    const { ctx, engine, prepare, timers, intervals } = setup()
    prepare(base)
    await loaded(engine)
    engine.start(0.05, 0)
    ctx.currentTime = 0.5
    engine.stop(5)
    expect(engine.isRunning).toBe(false)
    expect(intervals[0].cleared).toBe(true)
    expect(ctx.sources.every((s) => s.stoppedAt !== null)).toBe(true)
    const t = timers.find((x) => x.ms === 25)!
    expect(t).toBeDefined()
    t.fn()
    expect(ctx.suspends).toBe(1)
  })

  it('a start before the suspend lands keeps the context running', async () => {
    const { ctx, engine, prepare, timers } = setup()
    prepare(base)
    await loaded(engine)
    engine.start(0.05, 0)
    engine.stop(5)
    engine.start(0.2, 4800)
    timers.find((x) => x.ms === 25)!.fn()
    expect(ctx.suspends).toBe(0)
  })
})

describe('edits while playing', () => {
  it('a new program applies itself at presented + 6 frames when nobody names a point', async () => {
    const { ctx, engine, prepare } = setup()
    prepare(base)
    await loaded(engine)
    ctx.state = 'running'
    engine.start(0.1, 0)
    ctx.currentTime = 0.6
    const n0 = ctx.sources.length
    prepare(edl([{ id: 'a', start: 0, in: 2, out: 20 }]))
    expect(engine.stats.applied).toBe(0)
    await Promise.resolve()
    expect(engine.stats.applied).toBe(1)
    const fresh = ctx.sources.slice(n0).map((s) => Math.round((s.startedAt! - 0.1) * SR))
    // heard now = (0.6 − 0.1 − latency) s; + 6 frames (9600 samples).
    const heard = Math.round((0.6 - 0.1 - (ctx.baseLatency + ctx.outputLatency)) * SR)
    expect(Math.min(...fresh)).toBe(heard + 9600)
  })

  it('reschedule() names the point; setParams() applies a gain edit now', async () => {
    const { ctx, engine, prepare } = setup()
    prepare(base)
    await loaded(engine)
    ctx.state = 'running'
    engine.start(0.1, 0)
    ctx.currentTime = 0.6
    const n0 = ctx.sources.length
    prepare(edl([{ id: 'a', start: 0, in: 2, out: 20 }]))
    engine.reschedule({ dirtyFrames: [[0, 10]], dirtyParams: new Set() }, 40000)
    await Promise.resolve()
    expect(engine.stats.applied).toBe(1)
    expect(Math.min(...ctx.sources.slice(n0).map((s) => Math.round((s.startedAt! - 0.1) * SR)))).toBe(40000)
    const n1 = ctx.sources.length
    prepare(edl([{ id: 'a', start: 0, in: 2, out: 20, audio: { gain_db: -20 } }]))
    engine.setParams('a')
    expect(engine.stats.applied).toBe(2)
    expect(ctx.sources.length).toBe(n1)                                   // nothing restarted
  })

  it('a program prepared while paused is taken as it is', async () => {
    const { ctx, engine, prepare } = setup()
    prepare(base)
    prepare(edl([{ id: 'a', start: 0, out: 5 }]))
    expect(engine.plan!.total).toBe(5 * SR)
    expect(ctx.sources).toHaveLength(0)
  })
})

describe('the clock and pauses nobody issued', () => {
  it('reports a system suspend and stops in lockstep', async () => {
    const seen: string[] = []
    const { ctx, engine, prepare } = setup({ interrupted: (s) => seen.push(s) })
    prepare(base)
    await loaded(engine)
    engine.start(0.1, 0)
    ctx.currentTime = 0.3
    ctx.setState('interrupted')
    expect(seen).toEqual(['interrupted'])
    expect(engine.isRunning).toBe(false)
    expect(engine.stats.interrupted).toBe(1)
    const out = ctx.nodes.find((n) => n.outputs.some((o) => o.to === ctx.destination)) as unknown as { gain: FakeParam }
    expect(out.gain.events.at(-1)).toEqual({ type: 'ramp', v: 0, t: 0.3 + RAMP_S })
  })

  it('maps a display time to the context time heard then', async () => {
    const { ctx, engine, prepare } = setup()
    prepare(base)
    ctx.state = 'running'
    ctx.currentTime = 3
    // No output timestamp yet: currentTime − (base + output latency).
    expect(engine.ctxTimeAt(3000 + 50)).toBeCloseTo(3 - 0.02 + 0.05, 9)
    ctx.getOutputTimestamp = () => ({ contextTime: 2.98, performanceTime: 2990 })
    expect(engine.ctxTimeAt(3000)).toBeCloseTo(2.99, 9)
  })

  it('adds the output latency a timestamp of the RENDERED time leaves out (WebKit)', async () => {
    const { ctx, engine, prepare } = setup()
    prepare(base)
    ctx.state = 'running'
    ctx.currentTime = 1
    // measured in WKWebView: contextTime = currentTime − one render quantum,
    // performanceTime = now; outputLatency 15.6 ms is not in it
    ctx.outputLatency = 0.015625
    ctx.getOutputTimestamp = () => ({ contextTime: 1 - 128 / SR, performanceTime: 1000 })
    expect(engine.ctxTimeAt(1000)).toBeCloseTo(1 - 128 / SR - 0.015625, 9)
    expect(engine.ctxTimeAt(1040)).toBeCloseTo(1 - 128 / SR - 0.015625 + 0.04, 9)
    // a stale timestamp later (updated per IO buffer) does not flip it back
    ctx.getOutputTimestamp = () => ({ contextTime: 1 - 0.012, performanceTime: 1000 })
    expect(engine.ctxTimeAt(1000)).toBeCloseTo(1 - 0.012 - 0.015625, 9)
  })

  it('whenRunning waits for the context clock to MOVE, not for the running state', async () => {
    const { ctx, engine, prepare, timers } = setup()
    prepare(base)
    await ctx.suspend()
    let out: boolean | null = null
    void engine.whenRunning(1000).then((v) => { out = v })
    expect(ctx.state).toBe('running')      // resume() was asked for at once
    const poll = async () => { const t = timers.pop()!; t.fn(); await Promise.resolve() }
    // 'running', clock frozen (WebKit after a hidden page): not yet
    for (let i = 0; i < 5; i++) await poll()
    expect(out).toBeNull()
    ctx.currentTime += 0.02
    await poll()
    await Promise.resolve()
    expect(out).toBe(true)
  })

  it('whenRunning gives up after its timeout', async () => {
    const { ctx, engine, prepare, timers } = setup()
    prepare(base)
    await ctx.suspend()
    let out: boolean | null = null
    void engine.whenRunning(100).then((v) => { out = v })
    for (let i = 0; i < 10 && out === null; i++) { timers.pop()!.fn(); await Promise.resolve(); await Promise.resolve() }
    expect(out).toBe(false)
  })

  it('warms a new limiter while paused (resume, then suspend)', async () => {
    const { ctx, prepare, timers } = setup()
    prepare(edl([{ id: 'a', start: 0, out: 2 }], [{ id: 'm', src: '/bed.m4a', start: 0, out: 2 }]))
    expect(ctx.resumes).toBe(1)
    timers.find((t) => t.ms === 150)!.fn()
    expect(ctx.suspends).toBe(1)
  })
})
