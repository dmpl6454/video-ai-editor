// The audio plan: every clip's placement and gains, as the render's sound
// graph has them (compositor._audio_only_graph / audio_mix.build_audio_mix).
// The v1 length is checked against the 500 dispatch-made EDLs of
// tests/goldens/frame_plan_cases.json; the rest against hand-computed
// placements of the Python rules (the WK/Chromium parity test renders them
// against real ffmpeg: tests/wk/test_wk_audio.py).
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import type { EdlLike } from '../timeline/framePlan'
import type { SourceInfo, SourceInfoJson } from '../timeline/frameMap'
import { buildProgramMap, clipSample0 } from '../timeline/programMap'
import { samplesForFrames } from '../timeline/timebase'
import {
  buildAudioPlan, clipGainAt, diffPlans, mergeIntervals, planFromProgram, renderWindow, soundPulls, soundWindow,
  soundWindows, totalSamples,
  MIX_LIMIT, type AudioPlan, type ClipAudio,
} from './audioPlan'
import { afadeGainAt } from './curves'

const DOC: { cases: Array<{ edl: EdlLike; model: { audio_total: number } }> } = JSON.parse(readFileSync(
  fileURLToPath(new URL('../../../../../tests/goldens/frame_plan_cases.json', import.meta.url)), 'utf8'))

const SRC: SourceInfo = { rate: { num: 30, den: 1 }, tb: { num: 1, den: 15360 }, frames: 30 * 600, startTicks: 0, w: 64, h: 36 }
const lookup = () => SRC

interface ClipSpec { id: string; src?: string; start: number; in?: number; out: number; speed?: number | null; reverse?: boolean; freeze?: number; audio?: Record<string, unknown> }

function edl(parts: { v1?: ClipSpec[]; music?: ClipSpec[]; vo?: ClipSpec[]; a1?: ClipSpec[]; v2?: ClipSpec[];
  transitions?: Array<{ at: number; duration: number }>; fps?: number; tracks?: Record<string, Record<string, unknown>>
  loudness?: number | null }): EdlLike {
  const clip = (c: ClipSpec) => ({ src: c.src ?? '/m/a.mp4', in: c.in ?? 0, speed: null, reverse: false, audio: {}, ...c })
  const track = (id: string, type: string, clips: ClipSpec[] = [], extra: Record<string, unknown> = {}) =>
    ({ id, type, clips: clips.map(clip), transitions: [], muted: false, solo: false, ...extra, ...(parts.tracks?.[id] ?? {}) })
  const e: EdlLike = {
    canvas: { fps: parts.fps ?? 30, w: 64, h: 36, loudness_lufs: parts.loudness ?? null },
    tracks: [
      { ...track('v1', 'video', parts.v1), transitions: (parts.transitions ?? []).map((t) => ({ type: 'fade', ...t })) },
      track('v2', 'video', parts.v2), track('a1', 'audio', parts.a1), track('music', 'music', parts.music),
      track('vo', 'vo', parts.vo),
    ],
  }
  let d = 0
  for (const t of e.tracks!) for (const c of t.clips as ClipSpec[]) d = Math.max(d, c.start + ((c.out - (c.in ?? 0)) / (c.speed || 1)))
  e.duration = d
  return e
}

const plan = (e: EdlLike, opts = {}): AudioPlan => planFromProgram(e, buildProgramMap(e, lookup), lookup, opts)
const one = (p: AudioPlan, id: string): ClipAudio => p.clips.find((c) => c.id === id)!

describe('v1 length (the assembly sum)', () => {
  it('equals audio_total_samples over the 500-EDL corpus', () => {
    let n = 0
    for (const c of DOC.cases) {
      const fps = c.edl.canvas?.fps ?? 30
      expect(totalSamples(c.edl, fps)).toBe(c.model.audio_total)
      n++
    }
    expect(n).toBe(500)
  })

  it('is the plan total', () => {
    const e = edl({ v1: [{ id: 'a', start: 0, out: 1 }, { id: 'b', start: 1.5, out: 2 }] })
    expect(plan(e).total).toBe(totalSamples(e, 30))
    expect(plan(e).total).toBe(samplesForFrames(30 + 15 + 60, 30))
  })
})

describe('v1 clips', () => {
  it('follow the placements, with clip-local fades and the %.2f gain', () => {
    const e = edl({ v1: [
      { id: 'a', start: 0, in: 1.234, out: 2.234, audio: { gain_db: -6.004, fade_in: 0.25, fade_out: 0.5 } },
      { id: 'b', start: 1, in: 0, out: 1.5, audio: { mute: true, channels: 'left' } },
    ] })
    const p = plan(e)
    const a = one(p, 'a')
    expect(a.out0).toBe(0)
    expect(a.n).toBe(48000)
    expect(a.map).toEqual({ kind: 'runs', runs: [[0, 48000, clipSample0(1.234), 1]] })
    expect(a.gain).toBeCloseTo(Math.pow(10, -6 / 20), 15)
    expect(a.fades.map((f) => [f.type, f.start, f.range])).toEqual([['in', 0, 12000], ['out', 24000, 24000]])
    const b = one(p, 'b')
    expect([b.out0, b.n, b.mute, b.channels, b.exact]).toEqual([48000, 72000, true, 'left', true])
    expect(clipGainAt(b, 50000)).toBe(0)
    expect(p.buses).toEqual([{ id: 'v1', kind: 'v1', gain: 1 }])
    expect(p.master).toEqual({ gain: 1, ceilingDb: null })
    expect(p.approx).toEqual([])
  })

  it('carry the acrossfade overlap at a seam: head i/m, tail (m − 1 − i)/m', () => {
    const e = edl({ v1: [{ id: 'a', start: 0, out: 2 }, { id: 'b', start: 2, out: 2 }], transitions: [{ at: 2, duration: 0.5 }] })
    const p = plan(e)
    const a = one(p, 'a'), b = one(p, 'b')
    expect([a.xOut, b.xIn]).toEqual([24000, 24000])
    expect(b.out0).toBe(96000 - 24000)
    expect(clipGainAt(b, b.out0)).toBe(0)
    expect(clipGainAt(b, b.out0 + 12000)).toBe(0.5)
    const tail0 = a.out0 + a.n - 24000
    expect(clipGainAt(a, tail0 - 1)).toBe(1)
    expect(clipGainAt(a, tail0)).toBeCloseTo(23999 / 24000, 15)
    expect(clipGainAt(a, a.out0 + a.n - 1)).toBe(0)
  })

  it('plays retimed sound as a resample (APPROX), keep_pitch or not', () => {
    const e = edl({ v1: [
      { id: 'v', start: 0, in: 0, out: 2, speed: 2, audio: { keep_pitch: false } },
      { id: 't', start: 1, in: 0, out: 1, speed: 0.5 },
    ] })
    const p = plan(e)
    expect(one(p, 'v').map).toMatchObject({ kind: 'rate', rate: 2, reverse: false })
    expect(one(p, 't').map).toMatchObject({ kind: 'rate', rate: 0.5 })
    expect(one(p, 'v').exact).toBe(false)
    expect(p.approx.sort()).toEqual(['tempo', 'varispeed'])
  })

  it('reverse at 1x is sample-exact runs; the envelope is clip-local', () => {
    const e = edl({ v1: [{ id: 'r', start: 0, in: 1, out: 2, reverse: true,
      audio: { gain_env: { keyframes: [[0, 0], [1, -12]], interp: 'linear' } } }] })
    const r = one(plan(e), 'r')
    expect(r.map.kind).toBe('runs')
    expect(r.exact).toBe(true)
    if (r.map.kind === 'runs') expect(r.map.runs.every((x) => x[3] === -1)).toBe(true)
    expect(clipGainAt(r, 0)).toBe(1)
    expect(20 * Math.log10(clipGainAt(r, 24000))).toBeCloseTo(-6, 3)
  })
})

describe('speed curves and freezes (lane S1)', () => {
  it('plays a curve along its warped source positions (APPROX) and a freeze as silence (exact)', () => {
    const curve = { curve: [[0, 0.5], [1, 2]] }
    const e = edl({ v1: [
      { id: 'c', start: 0, in: 1, out: 3, speed: curve as never },
      { id: 'f', start: 1.6, in: 2, out: 2.5, freeze: 1 } as ClipSpec,
    ], music: [{ id: 'mc', start: 0, in: 0, out: 2, speed: curve as never }] })
    const p = plan(e)
    const c = one(p, 'c')
    expect(c.map).toMatchObject({ kind: 'curve', src0: clipSample0(1), seconds: 2 })
    expect(c.exact).toBe(false)
    const f = one(p, 'f')
    expect([f.map, f.exact]).toEqual([{ kind: 'runs', runs: [] }, true])
    const mc = one(p, 'mc')
    expect(mc.map).toMatchObject({ kind: 'curve', src0: 0, seconds: 2, end: 96000 })
    expect(mc.n).toBe(Math.round((2 / 1.25) * 48000))                   // atrim=end_sample: eff = 2 s / mean 1.25
    expect(p.approx).toContain('speed-curve')
  })
})

describe('lanes (music / voice-over / audio)', () => {
  it('sit at round(rs·1000) ms on the render clock with the input seek [S(in), S(out))', () => {
    const e = edl({ v1: [{ id: 'a', start: 0, out: 4 }],
      music: [{ id: 'm', src: '/m/bed.m4a', start: 1.23456, in: 2.5, out: 4, audio: { fade_in: 0.2, fade_out: 0.3 } }] })
    const p = plan(e)
    const m = one(p, 'm')
    expect(m.out0).toBe(1235 * 48)
    expect(m.n).toBe(4 * 48000 - 2.5 * 48000)
    expect(m.map).toEqual({ kind: 'runs', runs: [[0, 72000, 120000, 1]] })
    // afade st=%.3f of the render start, d=%.3f; out from re − fade_out.
    expect(m.fades.map((f) => [f.type, f.start, f.range])).toEqual([['in', 1235 * 48, 9600], ['out', 2435 * 48, 14400]])
    expect(m.t0).toBe(m.out0)
    expect(p.buses.map((b) => b.id)).toEqual(['v1', 'music'])
    expect(p.master.gain).toBeCloseTo(1 / MIX_LIMIT, 15)
    expect(p.master.ceilingDb).toBe(0)
  })

  it('are pulled left by a v1 cross-fade before them, and keep their whole length across one', () => {
    // Final QA (round 3): a sound lane plays whole (`clock.sound_window`);
    // the picture rule (`renderWindow`) shrank a voiceover by every seam it
    // crossed and cut its last words.
    const e = edl({ v1: [{ id: 'a', start: 0, out: 2 }, { id: 'b', start: 2, out: 2 }],
      transitions: [{ at: 2, duration: 0.5 }],
      vo: [{ id: 'v', start: 1.8, in: 0, out: 1 }, { id: 'late', start: 3, in: 0, out: 1 }] })
    const p = plan(e)
    const seams: Array<[number, number]> = [[2, 0.5]]
    expect(renderWindow(seams, 1.8, 2.8)).toEqual([1.8, 2.3])   // a picture window shrinks
    expect(soundWindow(seams, 1.8, 1)).toEqual([1.8, 2.8])      // a sound window does not
    const v = one(p, 'v')
    expect(v.out0).toBe(1800 * 48)
    expect(v.n).toBe(48000)                                  // all of its 1 s
    expect(one(p, 'late').out0).toBe(2500 * 48)
  })

  it('a split voice-over across a seam stays back to back (one run, one pull)', () => {
    const e = edl({ v1: [{ id: 'a', start: 0, out: 2 }, { id: 'b', start: 2, out: 2 }],
      transitions: [{ at: 2, duration: 0.5 }],
      vo: [{ id: 'p1', start: 1, in: 0, out: 1.5 }, { id: 'p2', start: 2.5, in: 1.5, out: 3 }] })
    const p = plan(e)
    expect(one(p, 'p1').out0).toBe(1000 * 48)
    expect(one(p, 'p2').out0).toBe(one(p, 'p1').out0 + one(p, 'p1').n)
  })

  it('a voiceover across two seams is heard whole, its fade-out at its own end', () => {
    const e = edl({ v1: [0, 2, 4, 6].map((s, i) => ({ id: `c${i}`, start: s, out: 2 })),
      transitions: [{ at: 2, duration: 0.5 }, { at: 4, duration: 0.4 }],
      vo: [{ id: 'v', start: 1, in: 0, out: 3.5, audio: { fade_out: 0.1 } }] })
    const v = one(plan(e), 'v')
    expect(v.out0).toBe(1000 * 48)
    expect(v.n).toBe(3.5 * 48000)
    expect(v.fades.map((f) => [f.type, f.start])).toEqual([['out', 4400 * 48]])
  })

  it('K1: a run laid to or past v1\'s end is cut where its layout end maps, its fade-out at the cut', () => {
    // v1 renders 3.5 s (layout 4, one 0.5 s seam). `schema.sound_render_windows`.
    const base = { v1: [{ id: 'a', start: 0, out: 2 }, { id: 'b', start: 2, out: 2 }],
      transitions: [{ at: 2, duration: 0.5 }] }
    const toEnd = one(plan(edl({ ...base, music: [{ id: 'm', start: 0, in: 0, out: 4, audio: { fade_out: 0.5 } }] })), 'm')
    expect([toEnd.out0, toEnd.n]).toEqual([0, 3.5 * 48000])
    expect(toEnd.fades.map((f) => [f.type, f.start])).toEqual([['out', 3000 * 48]])
    const past = one(plan(edl({ ...base, music: [{ id: 'm', start: 0, in: 0, out: 5 }] })), 'm')
    expect(past.n).toBe(4.5 * 48000)                          // render_time(5.0)
    const late = one(plan(edl({ ...base, vo: [{ id: 'v', start: 4.5, in: 0, out: 1 }] })), 'v')
    expect([late.out0, late.n]).toEqual([4000 * 48, 48000])     // placed past the end: whole
    // final QA (run 2, round 2): a run ending INSIDE v1's layout (3.9 < 4)
    // plays whole, even past the picture's 3.5 — its last words are heard
    const inside = one(plan(edl({ ...base, vo: [{ id: 'v', start: 1, in: 0, out: 2.9 }] })), 'v')
    expect(inside.n).toBe(2.9 * 48000)
    const run = plan(edl({ ...base, music: [{ id: 'p', start: 0, in: 0, out: 3.8 },
      { id: 'q', start: 3.8, in: 3.8, out: 4 }] }))
    expect(one(run, 'p').n).toBe(3.5 * 48000)
    expect(run.clips.some((c) => c.id === 'q')).toBe(false)   // wholly past the run's cut
  })

  it('retimed lanes resample; muted lanes are not mixed at all', () => {
    const e = edl({ v1: [{ id: 'a', start: 0, out: 4 }],
      a1: [{ id: 's', start: 0, in: 1, out: 3, speed: 2, audio: { keep_pitch: false } }],
      music: [{ id: 'm', start: 0, out: 1 }], tracks: { music: { muted: true } } })
    const p = plan(e)
    const s = one(p, 's')
    expect(s.map).toEqual({ kind: 'rate', src0: 48000, rate: 2, reverse: false, end: 144000 })
    expect(s.n).toBe(48000)
    expect(p.clips.some((c) => c.id === 'm')).toBe(false)
    expect(p.master.gain).toBeCloseTo(1 / MIX_LIMIT, 15)       // a1 alone still mixes
  })

  it('solo mutes every other audio-bearing lane (bus gain 0), v1 mute only v1', () => {
    const e = edl({ v1: [{ id: 'a', start: 0, out: 2 }], music: [{ id: 'm', start: 0, out: 1 }],
      vo: [{ id: 'v', start: 0, out: 1 }], tracks: { vo: { solo: true } } })
    expect(plan(e).buses.map((b) => [b.id, b.gain])).toEqual([['v1', 0], ['music', 0], ['vo', 1]])
    const m = edl({ v1: [{ id: 'a', start: 0, out: 2 }], tracks: { v1: { muted: true } } })
    expect(plan(m).buses).toEqual([{ id: 'v1', kind: 'v1', gain: 0 }])
    expect(plan(m).clips).toHaveLength(1)
  })
})

describe('PiP sound', () => {
  it('is the v1 seek rule, samples_for_frames long, delayed S(f0)', () => {
    const e = edl({ v1: [{ id: 'a', start: 0, out: 4 }], v2: [{ id: 'p', start: 1.01, in: 0.51, out: 1.51 }] })
    const p = plan(e)
    const x = one(p, 'p')
    expect(x.bus).toBe('pip:v2')
    expect(x.out0).toBe(samplesForFrames(30, 30))
    expect(x.n).toBe(samplesForFrames(30, 30))
    expect(x.map).toEqual({ kind: 'runs', runs: [[0, 48000, clipSample0(0.51), 1]] })
    expect(p.master.ceilingDb).toBeNull()                    // folded into the main sound: not "mixed"
  })

  // Wave D3 (E2): a retimed PIP's sound follows its picture (render/pip.py
  // `pip_audio_chain`, v1's rules; decoded clicks: test_b5_pip_frame_exact).
  it('a 2x PIP fills its footprint and resamples at 2x', () => {
    const e = edl({ v1: [{ id: 'a', start: 0, out: 4 }],
      v2: [{ id: 'p', start: 1, in: 0.5, out: 2.5, speed: 2, audio: { keep_pitch: false } }] })
    const p = plan(e)
    const x = one(p, 'p')
    expect(x.n).toBe(samplesForFrames(30, 30))              // 2 s of source → 1 s
    expect(x.map).toEqual({ kind: 'rate', src0: clipSample0(0.5), rate: 2, reverse: false, end: Number.MAX_SAFE_INTEGER })
    expect(x.exact).toBe(false)
    expect(p.approx).toContain('varispeed')
  })

  it('a curve PIP reads its curve map over its integral', () => {
    const curve = { curve: [[0, 1], [0.5, 0.25], [1, 1]] }
    const e = edl({ v1: [{ id: 'a', start: 0, out: 4 }],
      v2: [{ id: 'p', start: 0, in: 0, out: 2, speed: curve as unknown as number }] })
    const x = one(plan(e), 'p')
    expect(x.n).toBe(samplesForFrames(96, 30))              // 2 / 0.625 = 3.2 s
    expect(x.map).toMatchObject({ kind: 'curve', src0: 0, seconds: 2 })
  })

  it('a frozen PIP is silent for its hold, a reversed one runs backwards', () => {
    const e = edl({ v1: [{ id: 'a', start: 0, out: 4 }],
      v2: [{ id: 'f', start: 0, in: 1, out: 1 + 1 / 30, freeze: 2 },
           { id: 'r', start: 2.5, in: 0, out: 1, reverse: true }] })
    const p = plan(e)
    const f = one(p, 'f')
    expect(f.n).toBe(samplesForFrames(60, 30))
    expect(f.map).toEqual({ kind: 'runs', runs: [] })
    const r = one(p, 'r')
    expect(r.map).toMatchObject({ kind: 'rate', rate: 1, reverse: true, src0: samplesForFrames(30, 30) - 1 })
  })
})

describe('duck and loudness (APPROX)', () => {
  it('ducks the music bed under the key clips', () => {
    const e = edl({ v1: [{ id: 'a', start: 0, out: 1 }, { id: 'b', start: 3, out: 1 }],
      music: [{ id: 'm', start: 0, out: 6 }], tracks: { music: { duck: { to_db: -12 } } } })
    const p = plan(e)
    expect(p.duck).toEqual({ bus: 'music', floor: Math.pow(10, -12 / 20), key: [[0, 48000], [144000, 192000]] })
    expect(p.approx).toContain('duck')
  })

  it('applies the last-known loudness gain and a −1 dBFS ceiling only with a target', () => {
    const e = edl({ v1: [{ id: 'a', start: 0, out: 1 }], loudness: -14 })
    const p = plan(e, { loudnessGainDb: 3.456 })
    expect(p.master.gain).toBeCloseTo(Math.pow(10, 3.46 / 20), 12)
    expect(p.master.ceilingDb).toBe(-1)
    expect(plan(edl({ v1: [{ id: 'a', start: 0, out: 1 }] }), { loudnessGainDb: 3 }).master.ceilingDb).toBeNull()
  })

  it('merges key intervals', () => {
    expect(mergeIntervals([[5, 9], [0, 3], [2, 4], [20, 30]], 25)).toEqual([[0, 4], [5, 9], [20, 25]])
  })
})

describe('gain at a sample', () => {
  it('multiplies clip gain, envelope, fades and mute', () => {
    const e = edl({ v1: [{ id: 'a', start: 0, out: 2,
      audio: { gain_db: 6, fade_in: 1, gain_env: { keyframes: [[0, -6], [2, -6]], interp: 'linear' } } }] })
    const a = one(plan(e), 'a')
    const g = clipGainAt(a, 24000)
    expect(g).toBeCloseTo(Math.pow(10, 6 / 20) * Math.pow(10, -6 / 20) * afadeGainAt(a.fades[0], 24000), 12)
    expect(g).toBeCloseTo(0.5, 12)
  })
})

describe('cost', () => {
  it('plans 300 clips and a bed in a few ms (setTimeline budget, §11.1)', () => {
    const v1 = Array.from({ length: 300 }, (_v, i) => ({ id: `c${i}`, start: i * 2, in: (i % 7) * 0.5, out: (i % 7) * 0.5 + 2,
      audio: { gain_db: -(i % 5), fade_in: 0.1 } }))
    const e = edl({ v1, music: [{ id: 'm', start: 0, out: 600 }] })
    const pm = buildProgramMap(e, lookup)
    const t0 = performance.now()
    let p!: AudioPlan
    for (let i = 0; i < 5; i++) p = planFromProgram(e, pm, lookup)
    const ms = (performance.now() - t0) / 5
    expect(p.clips).toHaveLength(301)
    expect(ms).toBeLessThan(25)                     // informational bound; ~2 ms measured
  })
})

describe('plan diff (what an edit must touch)', () => {
  const base = { v1: [{ id: 'a', start: 0, out: 2 }, { id: 'b', start: 2, out: 2 }], music: [{ id: 'm', start: 0, out: 3 }] }
  const p0 = plan(edl(base))

  it('a gain edit rewrites one clip and reschedules nothing', () => {
    const p1 = plan(edl({ ...base, v1: [{ id: 'a', start: 0, out: 2, audio: { gain_db: -3 } }, base.v1[1]] }))
    const d = diffPlans(p0, p1)
    expect([...d.dirtyBuses]).toEqual([])
    expect([...d.paramClips]).toEqual([one(p1, 'a').key])
  })

  it('a trim reschedules v1 only; a music move the music lane only', () => {
    const trim = diffPlans(p0, plan(edl({ ...base, v1: [{ id: 'a', start: 0, out: 1.5 }, { id: 'b', start: 1.5, out: 2 }] })))
    expect([...trim.dirtyBuses]).toEqual(['v1'])
    const move = diffPlans(p0, plan(edl({ ...base, music: [{ id: 'm', start: 0.5, out: 3 }] })))
    expect([...move.dirtyBuses]).toEqual(['music'])
  })

  it('a mute toggle is a bus gain; a first music clip changes the master', () => {
    const mute = diffPlans(p0, plan(edl({ ...base, tracks: { music: { solo: true } } })))
    expect(mute.busGains).toBe(true)
    expect([...mute.dirtyBuses]).toEqual([])
    const bare = plan(edl({ v1: base.v1 }))
    expect(diffPlans(bare, p0).master).toBe(true)
    expect(buildAudioPlan(edl({ v1: base.v1 }), [], 30).clips).toEqual([])
  })
})

export type { SourceInfoJson }

describe('soundWindows (a detached sound follows its own picture)', () => {
  // Final QA (run 2), `schema.sound_runs`: the sounds of adjacent v1 clips
  // c1 [2, 4) and c2 [4, 6), fades of 0.5 at 2 and 4, detached back to back.
  const seams: Array<[number, number]> = [[2, 0.5], [4, 0.5]]
  const s1 = { id: 's1', src: 'c1.mp4', in: 0, out: 2, start: 2, linked_to: 'c1' }
  const s2 = { id: 's2', src: 'c2.mp4', in: 0, out: 2, start: 4, linked_to: 'c2' }

  it('starts each where its picture plays and plays it whole', () => {
    const w = soundWindows([s1, s2], seams, 6)
    expect(w.get(s1)).toEqual([expect.closeTo(1.5, 9), expect.closeTo(3.5, 9)])
    expect(w.get(s2)).toEqual([expect.closeTo(3.0, 9), expect.closeTo(5.0, 9)])
    const p = soundPulls([s1, s2], seams)
    expect([p.get(s1), p.get(s2)]).toEqual([expect.closeTo(0.5, 9), expect.closeTo(1.0, 9)])
  })

  it('keeps unlinked abutting pieces as one run', () => {
    const a = { ...s1, linked_to: undefined }
    const b = { ...s2, linked_to: undefined }
    const p = soundPulls([a, b], seams)
    expect([p.get(a), p.get(b)]).toEqual([expect.closeTo(0.5, 9), expect.closeTo(0.5, 9)])
  })
})
