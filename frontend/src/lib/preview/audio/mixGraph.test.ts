// MixGraph scheduling rules on a recording fake of Web Audio (the sound
// itself is rendered by WebKit and Chromium in tests/wk/test_wk_audio.py):
// integer-sample starts on the 1 s block grid, buffers holding exactly the
// planned source samples, per-sample gain curves that never overlap, the
// channel matrix, the limiter's look-ahead compensation, parameter-only
// rewrites, dirty-lane generations, stop ramps and re-anchoring.
import { describe, expect, it } from 'vitest'
import type { EdlLike } from '../timeline/framePlan'
import type { SourceInfo } from '../timeline/frameMap'
import { buildProgramMap } from '../timeline/programMap'
import type { PcmReader } from './audioChunks'
import { clipGainAt, planFromProgram, type AudioPlan } from './audioPlan'
import { asCtx, FakeContext, FakeGain, FakeOfflineContext, FakeParam, FakeSource, type ParamEvent } from './fakeAudio'
import {
  BLOCK_SAMPLES, channelMatrix, curvePositions, duckValueAt, duckWindows, holdAndRamp, LIMITER_LATENCY, limiterMakeupUndo, MixGraph,
  RAMP_S, sourceRange, trackedValue,
} from './mixGraph'

const SR = 48000
const SRC: SourceInfo = { rate: { num: 30, den: 1 }, tb: { num: 1, den: 15360 }, frames: 30 * 600, startTicks: 0, w: 64, h: 36 }

/** A source whose sample i is i / 1e7 (L) and −i / 1e7 (R); the bed's L is
 *  offset by +0.5 so its buffers are recognisable. */
function reader(opts: { missing?: Set<string> } = {}): PcmReader & { loads: Array<[string, number, number]> } {
  const loads: Array<[string, number, number]> = []
  return {
    loads,
    async load(src, a, b) { loads.push([src, a, b]) },
    ready(src) { return !opts.missing?.has(src) },
    silent() { return false },
    copy(src, first, count, dir, L, R, off) {
      if (opts.missing?.has(src)) return false
      for (let i = 0; i < count; i++) {
        const s = first + dir * i
        if (s < 0) continue
        L[off + i] = s / 1e7 + (src === '/bed.m4a' ? 0.5 : 0)
        R[off + i] = -s / 1e7
      }
      return true
    },
  }
}

function edl(parts: { v1: Array<Record<string, unknown>>; music?: Array<Record<string, unknown>>; musicTrack?: Record<string, unknown> }): EdlLike {
  const clip = (c: Record<string, unknown>) => ({ src: '/a.mp4', in: 0, speed: null, reverse: false, audio: {}, ...c })
  const e: EdlLike = {
    canvas: { fps: 30 },
    tracks: [
      { id: 'v1', type: 'video', clips: parts.v1.map(clip), transitions: [] },
      { id: 'music', type: 'music', clips: (parts.music ?? []).map(clip), transitions: [], ...(parts.musicTrack ?? {}) },
    ],
  }
  let d = 0
  for (const t of e.tracks!) for (const c of t.clips as Array<{ start: number; in?: number; out: number }>) d = Math.max(d, c.start + c.out - (c.in ?? 0))
  e.duration = d
  return e
}

const planOf = (e: EdlLike): AudioPlan => planFromProgram(e, buildProgramMap(e, () => SRC), () => SRC)

const curves = (p: FakeParam) => p.events.filter((e): e is Extract<ParamEvent, { type: 'curve' }> => e.type === 'curve')
/** Each clip's shape gain: the node its channel merger feeds. */
const shapesOf = (ctx: FakeContext) => ctx.nodes.filter((n) => n.kind === 'merger').map((m) => m.outputs[0].to as FakeGain)

describe('offline scheduling', () => {
  const e = edl({ v1: [{ id: 'a', start: 0, in: 1, out: 2.5, audio: { gain_db: -6, fade_in: 0.5 } }, { id: 'b', start: 1.5, in: 3, out: 4, audio: { channels: 'left' } }] })
  const plan = planOf(e)

  function run(p0 = 0, p1 = plan.total) {
    const ctx = new FakeOfflineContext()
    const g = new MixGraph(asCtx(ctx), plan, { reader: reader() })
    g.restart({ ctxTime: 0, sample: p0 }, p1)
    return { ctx, g }
  }

  it('starts every block at an integer sample on the 1 s grid, holding the planned source samples', () => {
    const { ctx } = run()
    const starts = ctx.sources.map((s) => Math.round(s.startedAt! * SR))
    for (const s of ctx.sources) expect(s.startedAt! * SR).toBeCloseTo(Math.round(s.startedAt! * SR), 6)
    // a: [0, 72000) → blocks at 0, 48000; b: [72000, 120000) → 72000, 96000.
    expect(starts).toEqual([0, 48000, 72000, 96000])
    const [a0, a1, b0, b1] = ctx.sources
    expect(a0.buffer!.length).toBe(48000)
    expect(a1.buffer!.length).toBe(24000)
    expect(b0.buffer!.length).toBe(24000)
    expect(b1.buffer!.length).toBe(24000)
    // Source sample of a's first output sample is clipSample0(1 s) = 48000.
    expect(a0.buffer!.data[0][0]).toBeCloseTo(48000 / 1e7, 9)
    expect(a1.buffer!.data[0][0]).toBeCloseTo((48000 + 48000) / 1e7, 9)
    expect(b1.buffer!.data[1][10]).toBeCloseTo(-(144000 + 24000 + 10) / 1e7, 9)
  })

  it('writes the exact per-sample gain as back-to-back value curves (a constant stretch as one value)', () => {
    const { ctx } = run()
    const shapes = shapesOf(ctx)
    expect(shapes).toHaveLength(2)
    // b has no fade or envelope: one setValueAtTime per block, no curve.
    expect(curves(shapes[1].gain)).toHaveLength(0)
    expect(shapes[1].gain.events.map((e) => [e.type, Math.round(e.t * SR), (e as { v: number }).v]))
      .toEqual([['set', 72000, 1], ['set', 96000, 1]])
    const aCurves = curves(shapes[0].gain)
    // a: its 0.5 s fade-in lies in block 0 (a curve); block 1 is constant.
    expect(aCurves.map((c) => [Math.round(c.t * SR), c.values.length, Math.round(c.d * SR)])).toEqual([[0, 48000, 47999]])
    expect(shapes[0].gain.events.at(-1)).toMatchObject({ type: 'set', t: 1 })
    expect((shapes[0].gain.events.at(-1) as { v: number }).v).toBeCloseTo(Math.pow(10, -6 / 20), 7)
    const a = plan.clips[0]
    for (const j of [0, 1, 12000, 23999, 24000, 47999]) expect(aCurves[0].values[j]).toBeCloseTo(clipGainAt(a, j), 7)
    expect(aCurves[0].values[12000]).toBeCloseTo(0.5 * Math.pow(10, -6 / 20), 7)
  })

  it('sets the channel matrix of the clip mode', () => {
    const { ctx } = run()
    const mtx = ctx.nodes.filter((n): n is FakeGain => n instanceof FakeGain && [0, 0.5, 1].includes(n.gain.value) && n.outputs.some((o) => o.to.kind === 'merger'))
    // a (stereo): 1,0,0,1 ; b (left): 1,0,1,0
    expect(mtx.slice(0, 4).map((g) => g.gain.value)).toEqual(channelMatrix('stereo'))
    expect(mtx.slice(4, 8).map((g) => g.gain.value)).toEqual(channelMatrix('left'))
    expect(channelMatrix('mono')).toEqual([0.5, 0.5, 0.5, 0.5])
    expect(channelMatrix('right')).toEqual([0, 1, 0, 1])
  })

  it('never schedules before the start point', () => {
    const { ctx } = run(60000, plan.total)
    expect(ctx.sources.map((s) => Math.round(s.startedAt! * SR) + 60000)).toEqual([60000, 72000, 96000])
  })

  it('reads the source range it needs (runs and resamples)', () => {
    expect(sourceRange(plan.clips[0], 0, 10)).toEqual([48000, 48010])
    const v = planOf(edl({ v1: [{ id: 'v', start: 0, in: 1, out: 3, speed: 2, audio: { keep_pitch: false } }] })).clips[0]
    expect(sourceRange(v, 0, 100)).toEqual([48000 - 2, 48000 + 200 + 3])
  })
})

describe('speed curve blocks', () => {
  it('interpolate the source at the curve\'s warped positions', () => {
    const plan = planOf(edl({ v1: [{ id: 'c', start: 0, in: 1, out: 3, speed: { curve: [[0, 0.5], [1, 2]] } }] }))
    const c = plan.clips[0]
    const xs = curvePositions(c, 0, 5)
    expect(xs[0]).toBe(48000)
    expect(xs[1]).toBeCloseTo(48000 + 0.5, 3)                          // speed 0.5 at the start
    const ctx = new FakeOfflineContext()
    const g = new MixGraph(asCtx(ctx), plan, { reader: reader() })
    g.restart({ ctxTime: 0, sample: 0 }, plan.total)
    const first = ctx.sources[0].buffer!.data[0]
    for (let j = 0; j < 5; j++) expect(first[j]).toBeCloseTo(xs[j] / 1e7, 9)
    const last = curvePositions(c, c.n - 1, c.n)[0]
    expect(last).toBeGreaterThan(48000 + 2 * 48000 - 100)              // the whole 2 s of source consumed
    expect(sourceRange(c, 0, 10)![0]).toBe(48000 - 1)
  })
})

describe('master', () => {
  it('limits a mixed programme (auto-level 1/0.97, 0 dBFS), compensating the look-ahead live', () => {
    const plan = planOf(edl({ v1: [{ id: 'a', start: 0, out: 1 }], music: [{ id: 'm', start: 0, out: 1 }] }))
    const ctx = new FakeContext()
    ctx.currentTime = 1
    const g = new MixGraph(asCtx(ctx), plan, { reader: reader(), compensateLatency: true })
    expect(ctx.nodes.some((n) => n.kind === 'compressor')).toBe(true)
    expect(g.latency).toBe(LIMITER_LATENCY)
    g.anchor = { ctxTime: 2, sample: 0 }
    expect(g.when(0) * SR).toBeCloseTo(2 * SR - LIMITER_LATENCY, 6)
    expect(g.sampleAt(g.when(12345))).toBeCloseTo(12345, 6)
    expect(limiterMakeupUndo(0)).toBe(1)
    expect(20 * Math.log10(limiterMakeupUndo(-1))).toBeCloseTo(-0.57, 6)
  })
})

describe('live edits', () => {
  const base = { v1: [{ id: 'a', start: 0, out: 3 }, { id: 'b', start: 3, in: 5, out: 6 }], music: [{ id: 'm', src: '/bed.m4a', start: 0, out: 4 }] }

  function live() {
    const ctx = new FakeContext()
    ctx.state = 'running'
    const plan = planOf(edl(base))
    const g = new MixGraph(asCtx(ctx), plan, { reader: reader(), compensateLatency: false })
    g.restart({ ctxTime: 1, sample: 0 }, 4 * SR)
    return { ctx, g, plan }
  }

  it('a gain edit rewrites automation from the edit, restarting nothing', () => {
    const { ctx, g } = live()
    const before = ctx.sources.length
    const next = planOf(edl({ ...base, v1: [{ id: 'a', start: 0, out: 3, audio: { gain_db: -12 } }, base.v1[1]] }))
    const p = 30000
    const d = g.setPlan(next, p, 4 * SR)
    expect([...d.dirtyBuses]).toEqual([])
    expect(ctx.sources.length).toBe(before)
    expect(ctx.sources.every((s) => s.stoppedAt === null)).toBe(true)
    const shape = shapesOf(ctx)[0].gain
    const t = g.when(p)
    const i = shape.events.findIndex((e) => e.type === 'hold')
    expect(shape.events[i]).toEqual({ type: 'hold', t })
    expect(shape.events[i + 1]).toEqual({ type: 'set', v: 1, t })                          // pinned at the old value
    expect(shape.events[i + 2]).toMatchObject({ type: 'ramp', t: t + RAMP_S })
    expect((shape.events[i + 2] as { v: number }).v).toBeCloseTo(Math.pow(10, -12 / 20), 6)
    const rewritten = shape.events[i + 3] as { type: string; t: number; v: number }
    expect(rewritten.type).toBe('set')                                  // constant gain: one value per block
    expect(Math.round(rewritten.t * SR - 1 * SR)).toBe(p + 240 + 1)
    expect(rewritten.v).toBeCloseTo(Math.pow(10, -12 / 20), 6)
  })

  it('a music move reschedules the music lane only, cross-fading its generation', () => {
    const { ctx, g } = live()
    const isBed = (s: FakeSource) => s.buffer!.data[0][0] >= 0.5
    const v1Sources = ctx.sources.filter((s) => !isBed(s))
    expect(v1Sources.length).toBeGreaterThan(0)
    const next = planOf(edl({ ...base, music: [{ id: 'm', src: '/bed.m4a', start: 0.5, out: 4 }] }))
    const p = 60000
    const n0 = ctx.sources.length
    const d = g.setPlan(next, p, 4 * SR)
    expect([...d.dirtyBuses]).toEqual(['music'])
    const stopped = ctx.sources.slice(0, n0).filter((s) => s.stoppedAt !== null)
    expect(stopped.length).toBeGreaterThan(0)
    expect(stopped.every(isBed)).toBe(true)
    for (const s of stopped) expect(s.stoppedAt).toBeCloseTo(g.when(p) + RAMP_S + 0.002, 9)
    expect(v1Sources.every((s) => s.stoppedAt === null)).toBe(true)
    const fresh = ctx.sources.slice(n0)
    expect(fresh.length).toBeGreaterThan(0)
    expect(Math.min(...fresh.map((s) => Math.round((s.startedAt! - 1) * SR)))).toBe(p)
  })

  it('stop ramps the output to 0 over 5 ms and stops every source', () => {
    const { ctx, g } = live()
    ctx.currentTime = 1.5
    g.stopAll(1.5)
    const out = g.out.gain as unknown as FakeParam
    expect(out.events.slice(-2)).toEqual([{ type: 'set', v: 1, t: 1.5 }, { type: 'ramp', v: 0, t: 1.5 + RAMP_S }])
    expect(ctx.sources.every((s) => s.stoppedAt !== null && Math.abs(s.stoppedAt - (1.5 + RAMP_S + 0.002)) < 1e-9)).toBe(true)
    expect(g.liveClips).toBe(0)
  })

  it('re-anchors every lane onto a new generation', () => {
    const { ctx, g } = live()
    ctx.currentTime = 1.2
    const n0 = ctx.sources.length
    g.reanchor({ ctxTime: 1.3, sample: 30000 }, 30000 + 4 * SR)
    const t0 = 1.2 + 0.01
    expect(ctx.sources.slice(0, n0).every((s) => s.stoppedAt !== null)).toBe(true)
    const first = ctx.sources.slice(n0).map((s) => g.sampleAt(s.startedAt!))
    expect(Math.min(...first)).toBe(Math.ceil(30000 + (t0 - 1.3) * SR))
  })

  it('a late block starts at the first schedulable sample, and a missing chunk waits', () => {
    const ctx = new FakeContext()
    ctx.state = 'running'
    ctx.currentTime = 5
    const plan = planOf(edl({ v1: base.v1, music: base.music }))
    const g = new MixGraph(asCtx(ctx), plan, { reader: reader({ missing: new Set(['/bed.m4a']) }) })
    const missing = g.restart({ ctxTime: 4.9, sample: 0 }, 2 * SR)
    expect(missing).toBeGreaterThan(0)
    const starts = ctx.sources.map((s) => s.startedAt!)
    expect(Math.min(...starts)).toBeGreaterThanOrEqual(5 + 0.01 - 1e-9)
    expect(ctx.sources.every((s) => s.buffer!.data[1][0] <= 0)).toBe(true)          // only v1 (the bed waits)
  })
})

describe('duck (APPROX trapezoid)', () => {
  it('merges key sounds across the hold and dips the bed ahead of speech', () => {
    const d = { floor: 0.25, key: [[48000, 96000], [100000, 110000], [400000, 420000]] as Array<[number, number]> }
    expect(duckWindows(d)).toEqual([[48000, 110000], [400000, 420000]])
    expect(duckValueAt(d, 48000 - 1000)).toBe(0.25)                                     // 60 ms look-ahead
    expect(duckValueAt(d, 48000 - 4000)).toBe(1)
    expect(duckValueAt(d, 110000 + 48000)).toBe(0.25)                                   // hold ≈ 1.47 s
    expect(duckValueAt(d, 110000 + 80000)).toBe(1)
    const plan = planOf(edl({ v1: [{ id: 'a', start: 1, out: 1 }], music: [{ id: 'm', start: 0, out: 4 }], musicTrack: { duck: { to_db: -12 } } }))
    const ctx = new FakeOfflineContext()
    const g = new MixGraph(asCtx(ctx), plan, { reader: reader() })
    g.restart({ ctxTime: 0, sample: 0 }, plan.total)
    const duck = ctx.nodes.filter((n): n is FakeGain => n instanceof FakeGain && n.gain.events.some((e) => e.type === 'target'))
    expect(duck).toHaveLength(1)
    const targets = duck[0].gain.events.filter((e) => e.type === 'target') as Array<{ v: number; t: number }>
    expect(targets[0].v).toBeCloseTo(Math.pow(10, -12 / 20), 9)
    expect(targets[0].t).toBeCloseTo(1 - 0.06, 9)
    expect(targets[1].v).toBe(1)
  })
})

describe('holdAndRamp', () => {
  it('pins the tracked value at t (a param with no automation must not ramp from the call)', () => {
    const p = new FakeParam(1)
    holdAndRamp(p as unknown as AudioParam, 2, 0)
    expect(p.events).toEqual([{ type: 'hold', t: 2 }, { type: 'set', v: 1, t: 2 }, { type: 'ramp', v: 0, t: 2 + RAMP_S }])
    expect(trackedValue(p as unknown as AudioParam, 2 + RAMP_S / 2)).toBeCloseTo(0.5, 12)
    holdAndRamp(p as unknown as AudioParam, 2 + RAMP_S / 2, 1)
    expect(p.events.at(-2)).toEqual({ type: 'set', v: 0.5, t: 2 + RAMP_S / 2 })
  })

  it('exports the block grid', () => {
    expect(BLOCK_SAMPLES).toBe(SR)
    expect(FakeSource).toBeDefined()
  })
})
