// The gain shapes against REAL ffmpeg (tests/goldens/audio_curve_cases.json,
// written by tests/gen_audio_curve_goldens.py): every afade curve, the fades
// the export's chains actually emit (their text and where ffmpeg put them),
// acrossfade at real seams, and the gain envelope expression. Within 1e-5.
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import {
  acrossfadeGains, acrossfadeSamples, afadeGainAt, afadeWindow, clipFadeWindows, clipGainLinear,
  compileEnv, envDbAt, envGainAt, fadeGain, FADE_CURVES, laneFadeWindows, pyFixed, pyRound, sampleTime,
  type FadeCurve, type FadeWindow, type Interp,
} from './curves'

type Pair = [number, number]
interface CurveCase { filter: string; type: 'in' | 'out'; curve: FadeCurve; st: string; d: string; start: number; range: number; samples: Pair[] }
interface EmittedFade { type: 'in' | 'out'; st: string; d: string; start: number; range: number }
interface EmittedCase {
  lane: 'v1' | 'lane'; fade_in: number; fade_out: number; effective_duration: number
  window: [number, number] | null; fades: EmittedFade[]
}
interface XfadeCase { fps: number | null; cost: number; d: string; m: number; first: number; g0: Pair[]; g1: Pair[] }
interface EnvCase { interp: Interp; keyframes: Array<[number, number]>; samples: Pair[] }

const DOC: { sample_rate: number; curves: CurveCase[]; emitted: EmittedCase[]; acrossfade: XfadeCase[]; gain_env: EnvCase[] } =
  JSON.parse(readFileSync(fileURLToPath(new URL('../../../../../tests/goldens/audio_curve_cases.json', import.meta.url)), 'utf8'))

const TOL = 1e-5

describe('afade curves (every ffmpeg curve, in and out)', () => {
  it('covers every curve both ways', () => {
    const seen = new Set(DOC.curves.map((c) => `${c.curve}/${c.type}`))
    for (const c of FADE_CURVES) for (const t of ['in', 'out']) expect(seen.has(`${c}/${t}`)).toBe(true)
  })

  it.each(DOC.curves.map((c) => [c.filter, c] as const))('%s', (_f, c) => {
    const w = afadeWindow(c.type, Number(c.st), Number(c.d), c.curve)
    expect([w.start, w.range]).toEqual([c.start, c.range])
    let worst = 0
    for (const [i, g] of c.samples) worst = Math.max(worst, Math.abs(afadeGainAt(w, i) - g))
    expect(worst).toBeLessThanOrEqual(TOL)
  })

  it('pins the sample convention: a fade-in is 0 AT its start, 1 at start + range', () => {
    const w: FadeWindow = { type: 'in', curve: 'tri', start: 100, range: 48 }
    expect([afadeGainAt(w, 99), afadeGainAt(w, 100), afadeGainAt(w, 101), afadeGainAt(w, 148)])
      .toEqual([0, 0, 1 / 48, 1])
    const o: FadeWindow = { ...w, type: 'out' }
    expect([afadeGainAt(o, 99), afadeGainAt(o, 100), afadeGainAt(o, 101), afadeGainAt(o, 148), afadeGainAt(o, 999)])
      .toEqual([1, 1, 47 / 48, 0, 0])
    expect(fadeGain('tri', 5, 0)).toBe(1)
  })
})

describe('the fades the export emits', () => {
  it.each(DOC.emitted.map((e, i) => [i, e] as const))('case %i', (_i, e) => {
    const audio = { fade_in: e.fade_in, fade_out: e.fade_out }
    const got = e.lane === 'v1'
      ? clipFadeWindows(audio, e.effective_duration)
      : laneFadeWindows(audio, e.window![0], e.window![1])
    expect(got.map((w) => ({ type: w.type, start: w.start, range: w.range })))
      .toEqual(e.fades.map((f) => ({ type: f.type, start: f.start, range: f.range })))
    // …and the text it came from: Python's %.3f (ties to even on the exact
    // binary value — 0.0625 is "0.062", which toFixed would write "0.063").
    for (const f of e.fades) {
      if (f.st !== '0') expect(pyFixed(Number(f.st), 3)).toBe(f.st)
    }
  })

  it('formats like Python, not like toFixed', () => {
    expect(pyFixed(0.0625, 3)).toBe('0.062')
    expect((0.0625).toFixed(3)).toBe('0.063')
    expect(pyFixed(0.1235, 3)).toBe('0.123')
    expect(pyFixed(0.0015, 3)).toBe('0.002')
    expect(pyFixed(-2.5, 0)).toBe('-2')
    expect(pyFixed(1 / 3, 6)).toBe('0.333333')
    expect(pyRound(2.5)).toBe(2)
    expect(pyRound(3.5)).toBe(4)
    expect(pyRound(1234.5000000001)).toBe(1235)
  })
})

describe('acrossfade (real v1 seams)', () => {
  it.each(DOC.acrossfade.map((a) => [a.d, a] as const))('d=%s', (_d, a) => {
    expect(acrossfadeSamples(a.cost)).toBe(a.m)
    let worst = 0
    for (const [p, g] of a.g0) {
      const i = p - a.first
      const exp = i < 0 ? 1 : i >= a.m ? 0 : acrossfadeGains(a.m, i)[0]
      worst = Math.max(worst, Math.abs(exp - g))
    }
    for (const [p, g] of a.g1) {
      const i = p - a.first
      const exp = i < 0 ? 0 : i >= a.m ? 1 : acrossfadeGains(a.m, i)[1]
      worst = Math.max(worst, Math.abs(exp - g))
    }
    expect(worst).toBeLessThanOrEqual(TOL)
  })

  it('the two sides do not sum to one', () => {
    const [g0, g1] = acrossfadeGains(48, 0)
    expect(g0 + g1).toBeCloseTo(47 / 48, 12)
  })
})

describe('gain envelope (per-sample aeval)', () => {
  it.each(DOC.gain_env.map((e, i) => [`${i} ${e.interp}`, e] as const))('%s', (_n, e) => {
    const env = compileEnv({ keyframes: e.keyframes, interp: e.interp })!
    let worst = 0
    for (const [i, g] of e.samples) worst = Math.max(worst, Math.abs(envGainAt(env, sampleTime(i)) - g))
    expect(worst).toBeLessThanOrEqual(TOL)
  })

  it('switches a step ON the sample its key sits on, whatever the last bit of t', () => {
    const env = compileEnv({ keyframes: [[0, 0], [0.4, -6], [0.8, -2]], interp: 'step' })!
    expect(sampleTime(19200)).toBeLessThan(0.4)
    expect(envDbAt(env, sampleTime(19199))).toBe(0)
    expect(envDbAt(env, sampleTime(19200))).toBe(-6)
    expect(envDbAt(env, 0.4 + 1e-15)).toBe(-6)
  })

  it('uses the constants of the filter text (%.9f times, %.4f values), not the keyframes', () => {
    const env = compileEnv({ keyframes: [[0.12345, -3], [0.67891, -12.34567]], interp: 'linear' })!
    expect(envDbAt(env, 0.12344)).toBe(-3)
    expect(envDbAt(env, 10)).toBe(-12.3457)
    expect(compileEnv({ keyframes: [] })).toBeNull()
  })
})

describe('clip gain', () => {
  it('is the %.2f dB the filter carries, and nothing at |g| <= 0.01', () => {
    expect(clipGainLinear(0.005)).toBe(1)
    expect(clipGainLinear(null)).toBe(1)
    expect(clipGainLinear(-6.004)).toBeCloseTo(Math.pow(10, -6 / 20), 15)
    expect(clipGainLinear(3.335)).toBeCloseTo(Math.pow(10, 3.33 / 20), 15)   // 3.335 is 3.33499…
  })
})
