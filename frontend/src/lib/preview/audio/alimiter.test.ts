// The alimiter port against REAL ffmpeg (tests/goldens/alimiter_cases.json,
// written by tests/gen_alimiter_goldens.py): bit for bit, for the two
// limiters the server's preview runs, over a hot step (release), click
// bursts (the peak queue) and a one-sided skew. And the worklet module
// (limiterWorklet.ts), evaluated as a worklet would, is the same core.
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import { ALimiterCore, alimiterRingLatency } from './alimiter'
import { LIMITER_LATENCY } from './mixGraph'
import { ALIMITER_ATTACK_MS, ALIMITER_RELEASE_MS, LIMITER_PROCESSOR, alimiterLimit, limiterWorkletSource } from './limiterWorklet'

interface Case { input: string; seed: number; limit: number; auto_level: boolean; out_f32le_b64: string }
const DOC: { n: number; sample_rate: number; cases: Case[] } = JSON.parse(
  readFileSync(fileURLToPath(new URL('../../../../../tests/goldens/alimiter_cases.json', import.meta.url)), 'utf8'))

/** gen_alimiter_goldens._env. */
function env(kind: string, ch: number, i: number): number {
  if (kind === 'step') return i < 1000 ? 5 : i < 3000 ? 32 : 8
  if (kind === 'clicks') return i % 480 < 24 ? 48 : 8
  const hot = i >= 1500 && i < 3500
  return ch === 0 ? (hot ? 40 : 6) : (hot ? 6 : 20)
}

/** gen_alimiter_goldens.golden_input: xorshift32 int16 steps × envelope. */
function goldenInput(kind: string, seed: number, n: number): [Float32Array, Float32Array] {
  let x = seed >>> 0
  const next = () => {
    x ^= x << 13
    x ^= x >>> 17
    x ^= x << 5
    x >>>= 0
    return (x >>> 16) - 32768
  }
  const L = new Float32Array(n), R = new Float32Array(n)
  for (let i = 0; i < n; i++) {
    L[i] = Math.trunc((next() * env(kind, 0, i)) / 64) * 2 ** -13
    R[i] = Math.trunc((next() * env(kind, 1, i)) / 64) * 2 ** -13
  }
  return [L, R]
}

function golden(c: Case): [Float32Array, Float32Array] {
  const b = Buffer.from(c.out_f32le_b64, 'base64')
  const y = new Float32Array(b.buffer, b.byteOffset, b.byteLength / 4)
  const L = new Float32Array(y.length / 2), R = new Float32Array(y.length / 2)
  for (let i = 0; i < L.length; i++) { L[i] = y[2 * i]; R[i] = y[2 * i + 1] }
  return [L, R]
}

/** The core over [x, zeros(latency)], its latency trimmed (alimiter's
 *  `latency=1`: trims the ring's delay, flushes it with silence). */
function runCore(core: ALimiterCore, L: Float32Array, R: Float32Array, block = L.length): [Float32Array, Float32Array] {
  const lat = core.latency
  const n = L.length + lat
  const inL = new Float32Array(n), inR = new Float32Array(n)
  inL.set(L)
  inR.set(R)
  const oL = new Float32Array(n), oR = new Float32Array(n)
  for (let a = 0; a < n; a += block) {
    const m = Math.min(block, n - a)
    core.process(inL.subarray(a, a + m), inR.subarray(a, a + m), oL.subarray(a, a + m), oR.subarray(a, a + m), m)
  }
  return [oL.slice(lat, lat + L.length), oR.slice(lat, lat + L.length)]
}

const firstDiff = (a: Float32Array, b: Float32Array): number => {
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return i
  return -1
}

describe('ALimiterCore is ffmpeg alimiter', () => {
  it('has alimiter\'s ring latency at 48 kHz (239 frames: 5 ms, less one)', () => {
    expect(alimiterRingLatency(48000, 5)).toBe(239)
    expect(new ALimiterCore(0.97, true, 5, 50, 48000, 0).latency).toBe(239)
    expect(new ALimiterCore(0.97, true, 5, 50, 48000, 49).latency).toBe(288)
  })

  it.each(DOC.cases.map((c) => [`${c.input} limit=${c.limit} level=${c.auto_level ? 1 : 0}`, c] as const))(
    '%s: bit for bit (whole run and per 128-frame quantum)', (_label, c) => {
      const [L, R] = goldenInput(c.input, c.seed, DOC.n)
      const [gL, gR] = golden(c)
      expect(Math.max(...L.map(Math.abs), ...R.map(Math.abs))).toBeGreaterThan(c.limit)   // it limits
      for (const block of [L.length, 128]) {
        const [oL, oR] = runCore(new ALimiterCore(c.limit, c.auto_level, 5, 50, DOC.sample_rate, 0), L, R, block)
        expect(firstDiff(oL, gL)).toBe(-1)
        expect(firstDiff(oR, gR)).toBe(-1)
      }
    })

  it('a padded core is the same stream, later by the pad', () => {
    const c = DOC.cases[0]
    const [L, R] = goldenInput(c.input, c.seed, DOC.n)
    const [gL] = golden(c)
    const core = new ALimiterCore(c.limit, c.auto_level, 5, 50, 48000, 49)
    const [oL] = runCore(core, L, R, 128)
    expect(firstDiff(oL, gL)).toBe(-1)
  })

  it('alimiterLimit writes a ceiling as the server does (6 decimals)', () => {
    expect(alimiterLimit(0)).toBe(1)
    expect(alimiterLimit(-1)).toBe(0.891251)
  })
})

describe('the limiter worklet module', () => {
  /** Evaluate the module text in a stand-in AudioWorkletGlobalScope. */
  function loadModule(rate = 48000) {
    const registered = new Map<string, new (o: unknown) => { process: (i: Float32Array[][], o: Float32Array[][], p: Record<string, Float32Array>) => boolean }>()
    class AudioWorkletProcessor { port = { onmessage: null } }
    const register = (name: string, cls: never) => { registered.set(name, cls) }
    new Function('AudioWorkletProcessor', 'registerProcessor', 'sampleRate', limiterWorkletSource())(
      AudioWorkletProcessor, register, rate)
    return registered
  }

  it('registers the processor, and it plays the core padded to LIMITER_LATENCY, per quantum', () => {
    const reg = loadModule()
    const Proc = reg.get(LIMITER_PROCESSOR)!
    expect(Proc).toBeDefined()
    for (const c of DOC.cases) {
      const [L, R] = goldenInput(c.input, c.seed, DOC.n)
      const [gL, gR] = golden(c)
      const p = new Proc({ processorOptions: {
        limit: c.limit, autoLevel: c.auto_level, attackMs: ALIMITER_ATTACK_MS, releaseMs: ALIMITER_RELEASE_MS, latency: LIMITER_LATENCY,
      } })
      const n = L.length + LIMITER_LATENCY
      const oL = new Float32Array(n), oR = new Float32Array(n)
      const limit = new Float32Array([Math.fround(c.limit)])       // an AudioParam's float32
      for (let a = 0; a < n; a += 128) {
        const inL = new Float32Array(128), inR = new Float32Array(128)
        inL.set(L.subarray(a, a + 128))
        inR.set(R.subarray(a, a + 128))
        const out = [new Float32Array(128), new Float32Array(128)]
        // a disconnected input (no channels) is silence
        expect(p.process(a < L.length ? [[inL, inR]] : [[]], [out], { limit })).toBe(true)
        oL.set(out[0].subarray(0, Math.min(128, n - a)), a)
        oR.set(out[1].subarray(0, Math.min(128, n - a)), a)
      }
      expect(firstDiff(oL.slice(LIMITER_LATENCY, LIMITER_LATENCY + L.length), gL)).toBe(-1)
      expect(firstDiff(oR.slice(LIMITER_LATENCY, LIMITER_LATENCY + L.length), gR)).toBe(-1)
    }
  })

  it('a ceiling change through the float32 param lands on the server\'s 6-decimal limit', () => {
    const Proc = loadModule().get(LIMITER_PROCESSOR)!
    const p = new Proc({ processorOptions: { limit: 1, autoLevel: false, attackMs: 5, releaseMs: 50, latency: LIMITER_LATENCY } }) as unknown as {
      core: { limit: number }; process: (i: Float32Array[][], o: Float32Array[][], p: Record<string, Float32Array>) => boolean
    }
    const q = () => [new Float32Array(128), new Float32Array(128)]
    p.process([q()], [q()], { limit: new Float32Array([1]) })
    expect(p.core.limit).toBe(1)
    p.process([q()], [q()], { limit: new Float32Array([Math.fround(0.891251)]) })
    expect(p.core.limit).toBe(0.891251)
  })
})
