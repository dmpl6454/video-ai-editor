// Final sweep 3 (the audio-only bed fix exposed it): a live block whose gain
// CURVE (a fade) is written after the context has already passed the
// block's start. Chromium moves such a curve's start to "now" and keeps its
// duration, so its end runs past the next block's first event, and that
// setValueAtTime throws ("overlaps setValueCurveAtTime"). The throw came
// after the block's source had started but before the clip recorded the
// span it holds, so every later refill scheduled the same block again: the
// music bed (default 0.5 s fade-in) played two or three times over for its
// first second or two, up to +6.7 dB (measured in headless Chromium).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { EdlLike } from '../timeline/framePlan'
import type { SourceInfo } from '../timeline/frameMap'
import { buildProgramMap } from '../timeline/programMap'
import type { PcmReader } from './audioChunks'
import { planFromProgram } from './audioPlan'
import { asCtx, FakeContext, FakeParam, type FakeSource } from './fakeAudio'
import { MixGraph } from './mixGraph'

const SR = 48000
const SRC: SourceInfo = { rate: { num: 30, den: 1 }, tb: { num: 1, den: 15360 }, frames: 30 * 600, startTicks: 0, w: 64, h: 36 }

function edl(): EdlLike {
  const clip = (c: Record<string, unknown>) => ({ in: 0, speed: null, reverse: false, ...c })
  return {
    canvas: { fps: 30 }, duration: 8,
    tracks: [
      { id: 'v1', type: 'video', transitions: [], clips: [clip({ id: 'a', src: '/a.mp4', start: 0, out: 8, audio: {} })] },
      { id: 'music', type: 'music', transitions: [], clips: [clip({ id: 'm', src: '/bed.m4a', start: 0, out: 8, audio: { fade_in: 0.5, fade_out: 1 } })] },
    ],
  }
}

/** Chromium's automation rules, on the fake: a curve starting in the past
 *  starts at currentTime (same duration); an event inside a curve throws. */
function chromiumAutomation(ctx: FakeContext) {
  const proto = FakeParam.prototype
  const orig = { set: proto.setValueAtTime, curve: proto.setValueCurveAtTime }
  const spans = new WeakMap<FakeParam, Array<[number, number]>>()
  const inside = (p: FakeParam, t: number) => (spans.get(p) ?? []).some(([a, b]) => t > a && t < b)
  proto.setValueAtTime = function (this: FakeParam, v: number, t: number) {
    if (inside(this, t)) throw new Error(`NotSupportedError: setValueAtTime(${v}, ${t}) overlaps a curve`)
    return orig.set.call(this, v, t)
  }
  proto.setValueCurveAtTime = function (this: FakeParam, values: Float32Array, t: number, d: number) {
    const start = Math.max(t, ctx.currentTime)
    if (inside(this, start) || inside(this, start + d)) throw new Error('NotSupportedError: overlapping curve')
    spans.set(this, [...(spans.get(this) ?? []), [start, start + d]])
    return orig.curve.call(this, values, start, d)
  }
  return () => { proto.setValueAtTime = orig.set; proto.setValueCurveAtTime = orig.curve }
}

/** The fake sources of one lane, as output-sample spans [p0, p1). */
function spans(g: MixGraph, ctx: FakeContext, bed: boolean): Array<[number, number]> {
  return ctx.sources
    .filter((s: FakeSource) => (s.buffer!.data[0].some((x) => x >= 0.5)) === bed && s.buffer!.length > 0)
    .map((s) => { const p0 = Math.round(g.sampleAt(s.startedAt!)); return [p0, p0 + s.buffer!.length] as [number, number] })
    .sort((x, y) => x[0] - y[0])
}

describe('a live block written late', () => {
  let undo: () => void = () => {}
  let ctx: FakeContext
  beforeEach(() => {
    ctx = new FakeContext()
    ctx.state = 'running'
    ctx.currentTime = 1
    undo = chromiumAutomation(ctx)
  })
  afterEach(() => undo())

  it('never schedules a clip\'s block twice, and ends each curve where its block does', () => {
    let slow = true
    const reader: PcmReader = {
      async load() { /* in memory */ },
      ready() { return true },
      silent() { return false },
      copy(src, _first, count, _dir, L, R, off) {
        // the first block's read is slow: the context moves past its start
        if (src === '/bed.m4a' && slow) { slow = false; ctx.currentTime += 0.02 }
        for (let i = 0; i < count; i++) { L[off + i] = (src === '/bed.m4a' ? 0.5 : 0.1); R[off + i] = 0.1 }
        return true
      },
    }
    const e = edl()
    const plan = planFromProgram(e, buildProgramMap(e, () => SRC), () => SRC)
    const g = new MixGraph(asCtx(ctx), plan, { reader, compensateLatency: false })
    expect(() => g.restart({ ctxTime: 1.01, sample: 0 }, 4 * SR)).not.toThrow()
    // the refills of the next seconds
    for (const t of [1.5, 2.0, 2.5]) {
      ctx.currentTime = t
      expect(() => g.schedule(Math.ceil(g.sampleAt(t)), Math.ceil(g.sampleAt(t)) + 4 * SR)).not.toThrow()
    }
    const bed = spans(g, ctx, true)
    expect(bed.length).toBeGreaterThan(2)
    for (let i = 1; i < bed.length; i++) expect(bed[i][0]).toBeGreaterThanOrEqual(bed[i - 1][1])   // no block twice
    expect(bed[bed.length - 1][1]).toBeGreaterThanOrEqual(Math.floor(g.sampleAt(2.5)) + 3 * SR)
    // each shape's automation stays in time order: a curve ends before the next event
    const shapes = ctx.nodes.filter((n) => n.kind === 'merger').map((m) => (m.outputs[0].to as unknown as { gain: FakeParam }).gain)
    for (const p of shapes) {
      const ev = p.events.filter((x) => x.type === 'set' || x.type === 'curve') as Array<{ type: string; t: number; d?: number }>
      for (let i = 1; i < ev.length; i++) expect(ev[i].t).toBeGreaterThanOrEqual(ev[i - 1].t + (ev[i - 1].d ?? 0) - 1e-9)
    }
    expect(shapes.some((p) => p.events.some((x) => x.type === 'curve'))).toBe(true)          // the fade-in was written
  })
})

// Final sweep 3, run 3 (round 2): Chromium's render thread runs up to a few
// quanta PAST currentTime (measured: 256 frames; a 512-frame callback buffer
// allows 512), so a curve trimmed to "now + one quantum" still started in
// the past, was moved to the render position with its duration kept, and
// the next block's automation was refused: a fade's gain froze for a whole
// 1 s block (−5.9 dB, headless Chromium).

const AHEAD = 512

/** Chromium's rules with the render position AHEAD frames past currentTime:
 *  a curve starting before it starts there (duration kept); an event inside
 *  a curve throws; cancelAndHoldAtTime truncates the curve it lands in and
 *  drops what comes after. */
function chromiumAhead(ctx: FakeContext) {
  const proto = FakeParam.prototype
  const orig = { set: proto.setValueAtTime, curve: proto.setValueCurveAtTime, hold: proto.cancelAndHoldAtTime }
  const spans = new WeakMap<FakeParam, Array<[number, number]>>()
  const inside = (p: FakeParam, t: number) => (spans.get(p) ?? []).some(([a, b]) => t > a + 1e-9 && t < b - 1e-9)
  const render = () => ctx.currentTime + AHEAD / SR
  proto.setValueAtTime = function (this: FakeParam, v: number, t: number) {
    if (inside(this, t)) throw new Error(`NotSupportedError: setValueAtTime(${v}, ${t}) overlaps a curve`)
    return orig.set.call(this, v, t)
  }
  proto.setValueCurveAtTime = function (this: FakeParam, values: Float32Array, t: number, d: number) {
    const start = Math.max(t, render())
    const hit = inside(this, start) || inside(this, start + d) ||
      (spans.get(this) ?? []).some(([a]) => a > start + 1e-9 && a < start + d - 1e-9) ||
      this.events.some((e) => e.type === 'set' && e.t > start + 1e-9 && e.t < start + d - 1e-9)
    if (hit) throw new Error(`NotSupportedError: setValueCurveAtTime(..., ${start}, ${d}) overlaps`)
    spans.set(this, [...(spans.get(this) ?? []), [start, start + d]])
    return orig.curve.call(this, values, start, d)
  }
  proto.cancelAndHoldAtTime = function (this: FakeParam, h: number) {
    spans.set(this, (spans.get(this) ?? []).filter(([a]) => a < h).map(([a, b]) => [a, Math.min(b, h)] as [number, number]))
    this.events = this.events
      .filter((e) => e.t < h)
      .map((e) => (e.type === 'curve' && e.t + e.d > h ? { ...e, d: h - e.t } : e))
    return orig.hold.call(this, h)
  }
  return () => { proto.setValueAtTime = orig.set; proto.setValueCurveAtTime = orig.curve; proto.cancelAndHoldAtTime = orig.hold }
}

describe('a live block written while the render thread runs ahead', () => {
  let undo: () => void = () => {}
  let ctx: FakeContext
  beforeEach(() => {
    ctx = new FakeContext()
    ctx.state = 'running'
    ctx.currentTime = 1
    undo = chromiumAhead(ctx)
  })
  afterEach(() => { undo(); vi.restoreAllMocks() })

  /** The first bed block's read is slow: `late` s pass, so its start is
   *  only a few ms ahead of currentTime — behind the render position. */
  function run(late: number) {
    let slow = true
    const reader: PcmReader = {
      async load() { /* in memory */ },
      ready() { return true },
      silent() { return false },
      copy(src, _first, count, _dir, L, R, off) {
        if (src === '/bed.m4a' && slow) { slow = false; ctx.currentTime += late }
        for (let i = 0; i < count; i++) { L[off + i] = (src === '/bed.m4a' ? 0.5 : 0.1); R[off + i] = 0.1 }
        return true
      },
    }
    const e = edl()
    // a 3 s fade-in: every block of the bed's first seconds is a curve
    const bed = e.tracks![1].clips[0] as { audio?: Record<string, unknown> }
    bed.audio = { fade_in: 3, fade_out: 1 }
    const plan = planFromProgram(e, buildProgramMap(e, () => SRC), () => SRC)
    const g = new MixGraph(asCtx(ctx), plan, { reader, compensateLatency: false })
    g.restart({ ctxTime: 1.01, sample: 0 }, 3 * SR)
    const shape = ctx.nodes.filter((n) => n.kind === 'merger')
      .map((m) => (m.outputs[0].to as unknown as { gain: FakeParam }).gain)
      .find((p) => p.events.some((x) => x.type === 'curve'))!
    return { g, shape }
  }

  const curves = (p: FakeParam) => p.events.filter((x) => x.type === 'curve') as Array<{ t: number; d: number }>

  it('trims past the render-ahead (baseLatency): the next block\'s curve is written, nothing refused', () => {
    ctx.baseLatency = AHEAD / SR
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const { shape } = run(0.005)
    expect(warn).not.toHaveBeenCalled()
    // the margin alone kept the curve inside its block: no truncation needed
    expect(shape.events.some((x) => x.type === 'hold')).toBe(false)
    const cs = curves(shape)
    // blocks 0, 1 and 2 of the fade each wrote their curve
    expect(cs.length).toBe(3)
    // the second block's curve starts exactly on its block
    expect(cs[1].t).toBeCloseTo(2.01, 9)
    for (let i = 1; i < cs.length; i++) expect(cs[i].t).toBeGreaterThanOrEqual(cs[i - 1].t + cs[i - 1].d - 1e-9)
  })

  it('an unknown render-ahead: a refused block truncates the overrun and is written anyway', () => {
    ctx.baseLatency = 0
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const { shape } = run(0.005)
    expect(warn).not.toHaveBeenCalled()
    expect(shape.events.some((x) => x.type === 'hold' && Math.abs(x.t - 2.01) < 1e-9)).toBe(true)
    const cs = curves(shape)
    expect(cs.length).toBe(3)
    expect(cs[1].t).toBeCloseTo(2.01, 9)
    for (let i = 1; i < cs.length; i++) expect(cs[i].t).toBeGreaterThanOrEqual(cs[i - 1].t + cs[i - 1].d - 1e-9)
  })
})
