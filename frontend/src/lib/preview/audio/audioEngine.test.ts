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
import { asCtx, FakeContext, type FakeParam, FakeWorkletNode, installFakeWorklet } from './fakeAudio'
import { RAMP_S } from './mixGraph'

const SR = 48000
const SRC: SourceInfo = { rate: { num: 30, den: 1 }, tb: { num: 1, den: 15360 }, frames: 30 * 600, startTicks: 0, w: 64, h: 36 }
const CS = 48000

function fakeIO(peak?: number) {
  const fetch = async (url: string): Promise<Response> => {
    if (url.endsWith('index.json')) {
      return new Response(JSON.stringify({ audio: { chunk_samples: CS, samples: 60 * CS, chunks: 60,
        ...(peak === undefined ? {} : { chunk_peak: new Array(60).fill(peak) }) } }))
    }
    const n = Number(/a\/(\d+)\.flac$/.exec(url)![1])
    return new Response(new Uint32Array([n]).buffer)
  }
  const decode = async (bytes: ArrayBuffer): Promise<AudioBuffer> => {
    const [n] = new Uint32Array(bytes)
    const L = Float32Array.from({ length: CS }, (_v, i) => (n * CS + i) / 1e7)
    return { sampleRate: SR, numberOfChannels: 2, length: CS, getChannelData: () => L } as unknown as AudioBuffer
  }
  return { fetch, decode }
}

function fakeChunks(peak?: number) {
  return new AudioChunks(fakeIO(peak))
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

function setup(opts: {
  interrupted?: (s: string) => void; peak?: number
  loudnessGainDb?: () => number | null; loudnessCurrent?: (renderHash: string) => boolean
} = {}) {
  const ctx = new FakeContext()
  const timers: Array<{ fn: () => void; ms: number }> = []
  const intervals: Array<{ fn: () => void; ms: number; cleared: boolean }> = []
  const engine = new AudioEngine({
    chunks: fakeChunks(opts.peak), createContext: () => asCtx(ctx), now: () => 1000 * ctx.currentTime,
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length },
    setInterval: (fn, ms) => { const h = { fn, ms, cleared: false }; intervals.push(h); return h },
    clearInterval: (h) => { (h as { cleared: boolean }).cleared = true },
    onInterrupted: opts.interrupted,
    loudnessGainDb: opts.loudnessGainDb, loudnessCurrent: opts.loudnessCurrent,
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
  it('a new program applies itself at presented + the edit lead (5 frames at 30 fps) when nobody names a point', async () => {
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
    // heard now = (0.6 − 0.1 − latency) s; + 5 frames (8000 samples).
    const heard = Math.round((0.6 - 0.1 - (ctx.baseLatency + ctx.outputLatency)) * SR)
    expect(Math.min(...fresh)).toBe(heard + 8000)
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

describe('the master limiter\'s APPROX ranges (gate RX)', () => {
  const mixed = edl([{ id: 'a', start: 0, out: 20 }], [{ id: 'm', src: '/bed.m4a', start: 2, out: 4 }])
  // The layouts land through the fake fetch's Response.json(), which takes a
  // different number of turns per Node version (CI's 22 needs more than the
  // ten microtasks that suffice on 25): pump timer turns until the bed's
  // layout is known, then the microtasks that carry Promise.all →
  // refreshLimiting. Bounded, so a real fault still fails instead of hanging.
  const settle = async (engine: AudioEngine) => {
    for (let i = 0; i < 50 && !engine.chunks.layoutNow('bbbb'); i++) await new Promise((r) => setTimeout(r, 0))
    for (let i = 0; i < 10; i++) await Promise.resolve()
  }

  it('are unbounded until the layouts land, then re-derived from the recorded peaks', async () => {
    const { engine, prepare } = setup({ peak: 0.2 })
    let told = 0
    engine.onLimitingChange = () => { told++ }
    prepare(mixed)
    expect(engine.limitingFrames()).toEqual([[0, 601]])                  // no peak known: all of it
    await settle(engine)
    expect(engine.limitingFrames()).toEqual([])                          // (0.2 + 0.2) / 0.97 < 1
    expect(told).toBe(1)
  })

  // K2 (0.8.0 QA): a layout first read while the sound was being built has
  // no peaks; when its re-read lands, the ranges are re-derived and told.
  it('are re-derived when a layout read while the sound was being built lands', async () => {
    const { engine, prepare } = setup({ peak: 0.2 })
    prepare(mixed)
    await settle(engine)
    expect(engine.limitingFrames()).toEqual([])
    let told = 0
    engine.onLimitingChange = () => { told++ }
    // the layout of the bed changes under the engine (a re-read with peaks
    // over the ceiling this time): the engine re-derives and says so
    const layout = engine.chunks.layoutNow('bbbb')!
    ;(engine.chunks as unknown as { known: Map<string, unknown> }).known.set('bbbb', { ...layout, chunkPeak: new Array(60).fill(0.9) })
    engine.chunks.onLayoutChange?.('bbbb')
    expect(engine.limitingFrames()).not.toEqual([])
    expect(told).toBe(1)
  })

  // P2 limiter tail: the engine registers the alimiter worklet on its
  // context; once it is, the graph's limiter moves onto it and the plans
  // drop their ranges (the mix over the ceiling is the server's) — told.
  it('go when the alimiter worklet lands on the context, and the graph moves onto it', async () => {
    const { ctx, engine, prepare } = setup({ peak: 0.7 })
    const undo = installFakeWorklet(ctx)
    try {
      let told = 0
      engine.onLimitingChange = () => { told++ }
      prepare(mixed)
      expect(engine.exactLimiter).toBe(false)                            // the compressor until then
      await settle(engine)
      expect(ctx.audioWorklet!.modules).toHaveLength(1)
      expect(engine.exactLimiter).toBe(true)
      expect(ctx.nodes.some((n) => n instanceof FakeWorkletNode && !n.disconnected)).toBe(true)
      expect(engine.limitingFrames()).toEqual([])
      expect(told).toBeGreaterThanOrEqual(1)
      prepare(mixed)                                                     // a later program is planned for it
      expect(engine.limitingFrames()).toEqual([])
    } finally { undo() }
  })

  it('stay (the compressor fallback) where the worklet module is refused', async () => {
    const { ctx, engine, prepare } = setup({ peak: 0.7 })
    const undo = installFakeWorklet(ctx, true)
    try {
      prepare(mixed)
      await settle(engine)
      expect(engine.exactLimiter).toBe(false)
      expect(engine.limitingFrames()).toEqual([[59 - 3, 180 + 3 + 1]])
    } finally { undo() }
  })

  it('narrow to where the lanes overlap when the peaks say so; a later prepare needs no telling', async () => {
    const { engine, prepare } = setup({ peak: 0.7 })
    let told = 0
    engine.onLimitingChange = () => { told++ }
    prepare(mixed)
    await settle(engine)
    expect(engine.limitingFrames()).toEqual([[59 - 3, 180 + 3 + 1]])    // the bed's 2-6 s ± 100 ms ± a frame
    expect(told).toBe(1)
    prepare(mixed)
    expect(engine.limitingFrames()).toEqual([[56, 184]])
    await settle(engine)
    expect(told).toBe(1)
  })
})

// Final QA r3: the Instant preview played the raw mix — ~11 dB under the
// server preview and the export on a project with the default −16 LUFS
// target — and marked it EXACT. The engine plays the server preview's master
// gain (GET /preview_loudness) and tells the engine whether that gain was
// measured for this render's sound (else the frames are APPROX 'loudness').
describe('the project loudness gain', () => {
  const target: EdlLike = { ...base, canvas: { fps: 30, loudness_lufs: -16 } }

  it('plays the preview gain it is given, re-plans when it changes, and says if it is current', () => {
    let gain: number | null = null
    let measuredFor: string | null = null
    const { engine, prepare } = setup({ loudnessGainDb: () => gain, loudnessCurrent: (h) => h === measuredFor })
    let told = 0
    engine.onLimitingChange = () => { told++ }
    prepare(target)
    expect(engine.plannedMaster()).toEqual({ gain: 1, ceilingDb: null })    // nothing known yet: raw
    // K2 (0.8.0 QA): not measured yet is no verdict (no "≈ Loudness" on
    // every fresh project); the controller measures it promptly instead
    expect(engine.loudnessOffDb()).toBeUndefined()
    gain = 10.8
    measuredFor = info.renderHash
    engine.refreshLoudness()
    expect(engine.plannedMaster()!.gain).toBeCloseTo(10 ** (10.8 / 20), 9)
    expect(engine.plannedMaster()!.ceilingDb).not.toBeNull()              // the limiter follows the lift
    expect(engine.loudnessOffDb()).toBe(0)                                  // plays the measured gain
    expect(told).toBe(1)                                                   // the engine reclassifies
  })

  it('reports how far the gain it plays is from the measured one', () => {
    let gain: number | null = 4
    let measuredFor: string | null = null
    const { engine, prepare } = setup({ loudnessGainDb: () => gain, loudnessCurrent: (h) => h === measuredFor })
    prepare(target)                                                        // plays the last-known 4 dB
    expect(engine.loudnessOffDb()).toBeUndefined()                         // …not measured: no verdict
    gain = 10.8
    measuredFor = info.renderHash                                          // measured, not re-planned yet
    expect(engine.loudnessOffDb()).toBeCloseTo(6.8, 9)                      // audible: APPROX
    engine.refreshLoudness()
    expect(engine.loudnessOffDb()).toBe(0)
  })

  it('says nothing without a target, or without a loudness source (test harnesses)', () => {
    const a = setup({ loudnessGainDb: () => 6, loudnessCurrent: () => true })
    a.prepare(base)                                                        // no loudness_lufs
    expect(a.engine.loudnessOffDb()).toBeUndefined()
    expect(a.engine.plannedMaster()!.gain).toBe(1)
    const b = setup()
    b.prepare(target)
    expect(b.engine.loudnessOffDb()).toBeUndefined()
  })
})

// Final QA (engine): Play right after a seek sometimes stopped by itself five
// frames later in WKWebView. stop()'s suspend timer fired while the context
// was 'running', a start() a few ms later saw 'running' and did not resume,
// and the statechange of OUR OWN suspend then landed while playing — read as
// a system interruption, so the engine paused itself.
class DeferredContext extends FakeContext {
  private queue: Array<() => void> = []
  resume() { this.resumes++; return new Promise<void>((r) => this.queue.push(() => { this.setState('running'); r() })) }
  suspend() { this.suspends++; return new Promise<void>((r) => this.queue.push(() => { this.setState('suspended'); r() })) }
  /** Land the oldest pending state change (as the audio thread would). */
  land(): boolean { const f = this.queue.shift(); if (f) f(); return !!f }
}

describe('our own suspend is never an interruption', () => {
  it('a restart between the suspend call and its statechange keeps playing', async () => {
    const seen: string[] = []
    const ctx = new DeferredContext()
    const timers: Array<{ fn: () => void; ms: number }> = []
    const engine = new AudioEngine({
      chunks: fakeChunks(), createContext: () => asCtx(ctx), now: () => 1000 * ctx.currentTime,
      setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length },
      setInterval: () => 0, clearInterval: () => {},
      onInterrupted: (s) => seen.push(s),
    })
    engine.prepare(base, audioPlacements(buildProgramMap(base, () => SRC), () => SRC), info)
    await loaded(engine)
    engine.start(0.05, 0)              // play: resume() requested
    ctx.land()                         // → running
    engine.stop(5)                     // the redundant seek-after-play
    timers.find((x) => x.ms === 25)!.fn()   // suspend() requested while 'running'
    expect(ctx.state).toBe('running')
    engine.start(0.2, 4800)            // play again before the suspend lands
    ctx.land()                         // our suspend lands while playing
    expect(seen).toEqual([])
    expect(engine.stats.interrupted).toBe(0)
    expect(engine.isRunning).toBe(true)
    // …and the engine resumes the context it suspended
    for (let i = 0; i < 5; i++) await Promise.resolve()
    ctx.land()
    expect(ctx.state).toBe('running')
    expect(engine.isRunning).toBe(true)
  })

  it('a real suspend while playing is still reported', async () => {
    const seen: string[] = []
    const ctx = new DeferredContext()
    const engine = new AudioEngine({
      chunks: fakeChunks(), createContext: () => asCtx(ctx), now: () => 1000 * ctx.currentTime,
      setTimeout: () => 0, setInterval: () => 0, clearInterval: () => {},
      onInterrupted: (s) => seen.push(s),
    })
    engine.prepare(base, audioPlacements(buildProgramMap(base, () => SRC), () => SRC), info)
    await loaded(engine)
    engine.start(0.05, 0)
    ctx.land()
    ctx.setState('suspended')          // the system, not us
    expect(seen).toEqual(['suspended'])
    expect(engine.isRunning).toBe(false)
  })
})

// Final sweep 3, run 3 (round 2): play pressed while a new import's sound
// chunks were still loading dropped the first 0.4 s (1 s with a slow server)
// while picture and playhead ran — and the range read EXACT. The engine now
// holds a start for the chunks under it (soundHold, ≤ 700 ms, §11.1), and
// whatever still could not be played is reported as loading ('audio:pending').
describe('sound whose chunks are not in memory yet', () => {
  /** Chunks whose FLAC answers wait until `open()` (the layout does not). */
  function gated() {
    let open!: () => void
    const gate = new Promise<void>((r) => { open = r })
    const io = fakeIO()
    const fetch = async (url: string): Promise<Response> => {
      if (!url.endsWith('index.json')) await gate
      return io.fetch(url)
    }
    return { chunks: new AudioChunks({ fetch, decode: io.decode }), open }
  }
  const flush = async () => { for (let i = 0; i < 4; i++) await new Promise((r) => setTimeout(r, 0)) }

  function make(chunks: AudioChunks) {
    const ctx = new FakeContext()
    const timers: Array<{ fn: () => void; ms: number }> = []
    const intervals: Array<{ fn: () => void; ms: number; cleared: boolean }> = []
    const engine = new AudioEngine({
      chunks, createContext: () => asCtx(ctx), now: () => 1000 * ctx.currentTime,
      setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length },
      setInterval: (fn, ms) => { const h = { fn, ms, cleared: false }; intervals.push(h); return h },
      clearInterval: (h) => { (h as { cleared: boolean }).cleared = true },
    })
    engine.prepare(base, audioPlacements(buildProgramMap(base, () => SRC), () => SRC), info)
    return { ctx, engine, timers, intervals }
  }

  it('soundHold: none when the chunks are in memory; else resumes the context and waits for them', async () => {
    const g = gated()
    const { ctx, engine } = make(g.chunks)
    await flush()                                       // the layout
    const hold = engine.soundHold(2 * SR, 700)
    expect(hold).not.toBeNull()
    expect(ctx.resumes).toBe(1)                         // inside the user's gesture
    let ok: boolean | null = null
    void hold!.then((v) => { ok = v })
    await flush()
    expect(ok).toBeNull()
    g.open()
    await flush()
    expect(ok).toBe(true)
    // the first block and the one after its first 0.3 s are in memory now
    expect(engine.reader.ready('/a.mp4', 2 * SR, 3 * SR)).toBe(true)
    expect(engine.soundHold(2 * SR, 700)).toBeNull()
  })

  it('soundHold gives up after its timeout', async () => {
    const g = gated()
    const { engine, timers } = make(g.chunks)
    await flush()
    const hold = engine.soundHold(2 * SR, 700)!
    let ok: boolean | null = null
    void hold.then((v) => { ok = v })
    timers.find((t) => t.ms === 700)!.fn()
    await flush()
    expect(ok).toBe(false)
  })

  it('a start before the chunks land: what it cannot play reads as loading, what it lost stays so, a stop clears it', async () => {
    const g = gated()
    const { ctx, engine, intervals } = make(g.chunks)
    await flush()
    let told = 0
    engine.onLimitingChange = () => { told++ }
    ctx.currentTime = 2
    engine.start(2.1, 2 * SR)                           // output sample 96000 heard at 2.1 s
    expect(ctx.sources).toHaveLength(0)                 // nothing to play yet
    const f0 = engine.soundLoadingFrames()
    expect(told).toBe(1)
    expect(f0.some(([a, b]) => a <= 60 && b > 60)).toBe(true)          // frame 60 = 2.0 s
    // a refill a second later, still nothing: the second gone by is lost
    ctx.currentTime = 3
    intervals[0].fn()
    const f1 = engine.soundLoadingFrames()
    expect(f1.some(([a, b]) => a <= 60 && b > 85)).toBe(true)
    // the chunks land at 3.4 s: scheduled from there; [2.0, ~3.3) stays loading
    g.open()
    ctx.currentTime = 3.4
    await flush()
    expect(ctx.sources.length).toBeGreaterThan(0)
    const first = Math.min(...ctx.sources.map((s) => s.startedAt!))
    expect(first).toBeGreaterThanOrEqual(3.4)
    const f2 = engine.soundLoadingFrames()
    const heardAtFirst = Math.floor((2 * SR + (first - 2.1) * SR) / SR * 30)
    expect(f2.some(([a, b]) => a <= 60 && b >= heardAtFirst)).toBe(true)
    expect(f2.every(([, b]) => b <= heardAtFirst + 2)).toBe(true)      // nothing past the sound
    const toldBeforeStop = told
    engine.stop(5)
    expect(engine.soundLoadingFrames()).toEqual([])
    expect(told).toBe(toldBeforeStop + 1)
  })

  it('a start with every chunk in memory reads nothing as loading', async () => {
    const { ctx, engine } = make(fakeChunks())
    await loaded(engine)
    let told = 0
    engine.onLimitingChange = () => { told++ }
    ctx.currentTime = 2
    engine.start(2.1, 2 * SR)
    expect(engine.soundLoadingFrames()).toEqual([])
    expect(told).toBe(0)
  })
})
