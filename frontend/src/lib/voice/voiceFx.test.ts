// The preview's voice effects against REAL ffmpeg (tests/goldens/
// voice_fx_cases.json, written by tests/gen_voice_fx_goldens.py from the
// export's own filter text): every non-pitch preset sample for sample, the
// Hall's impulse response, and the pitch presets' spectral centroid and level
// per 50 ms block (the granular shifter is APPROX; the bounds are support.ts
// VOICE_FX_PARITY's).
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import { ICONS } from '../icons'
import { VOICE_FX_PARITY } from '../preview/timeline/support'
import {
  GRAIN, PERIOD_MAX_OFFSET, PERIOD_WINDOW, SR, VOICE_PRESETS, VOICE_TABLE, processStereo, reverbIr, stagesAt, vibratoTable, voicePeakBound,
  voicePlan, voicePreset,
} from './voiceFx'

interface Win { start: number; L: number[]; R: number[] }
interface Case {
  effect: string; intensity: number; filter: string; length: number
  windows?: Win[]; blocks?: { centroid: number[]; rms_db: number[] }
}
const DOC: {
  n: number; block: number; cases: Case[]
  reverb_ir: { params: { dry: number; tail: number; rt60: number; predelay_ms: number }; samples: number; windows: Win[] }
} = JSON.parse(readFileSync(fileURLToPath(new URL('../../../../tests/goldens/voice_fx_cases.json', import.meta.url)), 'utf8'))

/** gen_voice_fx_goldens.golden_input, in double then rounded to float32. */
function goldenInput(n: number): [Float32Array, Float32Array] {
  const L = new Float32Array(n)
  const R = new Float32Array(n)
  for (let i = 0; i < n; i++) {
    const t = i / SR
    const env = 0.35 + 0.65 * (Math.sin(2 * Math.PI * 3.0 * t) > -0.3 ? 1 : 0)
    let l = 0
    let r = 0
    for (let k = 1; k <= 20; k++) l += (0.3 / k) * Math.sin(2 * Math.PI * 140 * k * t + 0.3 * k)
    for (let k = 1; k <= 16; k++) r += (0.25 / k) * Math.sin(2 * Math.PI * 180 * k * t + 0.3 * k)
    l *= env
    r *= env
    if (i === 100) { l += 0.4; r += 0.4 }
    L[i] = l
    R[i] = r
  }
  return [L, R]
}

const [IN_L, IN_R] = goldenInput(DOC.n)

function blocks(y: Float64Array, block: number): { centroid: number[]; rms_db: number[] } {
  const centroid: number[] = []
  const rms: number[] = []
  const win = Array.from({ length: block }, (_, i) => 0.5 - 0.5 * Math.cos((2 * Math.PI * i) / (block - 1)))
  for (let s = 0; s + block <= y.length; s += block) {
    // |DFT|² by a direct real DFT on the bins (block is small: 2400)
    let num = 0, den = 0, e = 0
    const b = y.subarray(s, s + block)
    for (let i = 0; i < block; i++) e += b[i] * b[i]
    for (let k = 0; k <= Math.round((12000 * block) / SR); k++) {                 // ≤ 12 kHz: all the energy here
      let re = 0, im = 0
      const w = (2 * Math.PI * k) / block
      for (let i = 0; i < block; i++) {
        const v = b[i] * win[i]
        re += v * Math.cos(w * i)
        im -= v * Math.sin(w * i)
      }
      const p = re * re + im * im
      num += p * (k * SR / block)
      den += p
    }
    centroid.push(num / Math.max(den, 1e-30))
    rms.push(10 * Math.log10(e / block + 1e-20))
  }
  return { centroid, rms_db: rms }
}

describe('the table', () => {
  it('is the served one: every preset has a label, a hint, stages and an icon the app draws', () => {
    expect(VOICE_PRESETS.map((p) => p.id)).toEqual(
      ['chipmunk', 'deep', 'monster', 'robot', 'echo', 'reverb', 'telephone', 'megaphone', 'radio', 'underwater', 'vibrato'])
    for (const p of VOICE_PRESETS) {
      expect(p.label && p.hint && p.stages.length).toBeTruthy()
      expect(ICONS).toHaveProperty(p.icon)
    }
    expect(VOICE_TABLE.intensity_range).toEqual([0, 1])
    expect(voicePreset('nope')).toBeNull()
  })

  it('scales every stage to its neutral: intensity 0 is no effect, 0.5 is half', () => {
    expect(stagesAt('chipmunk', 0)).toEqual([])
    expect(voicePlan('chipmunk', 0)).toBeNull()
    expect(stagesAt('chipmunk', 0.5)[0].p.semitones).toBe(4.5)
    const echo = stagesAt('echo', 0.5)[0].p
    expect(echo.decays).toEqual([0.25, 0.125, 0.0625])
    expect(echo.out_gain).toBeCloseTo(0.925, 12)
    expect(voicePlan(null, 1)).toBeNull()
    expect(voicePlan('reverb', 1)!.reverb).not.toBeNull()
  })
})

describe('against ffmpeg: every preset but the pitch shifters, sample for sample', () => {
  const exact = DOC.cases.filter((c) => c.windows)
  it('covers robot, echo, the band limits and the vibratos at 100 % and 50 %', () => {
    expect(exact.map((c) => `${c.effect}@${c.intensity}`)).toEqual([
      'robot@1', 'robot@0.5', 'echo@1', 'echo@0.5', 'telephone@1', 'telephone@0.5', 'megaphone@1', 'megaphone@0.5',
      'radio@1', 'radio@0.5', 'underwater@1', 'underwater@0.5', 'vibrato@1', 'vibrato@0.5'])
  })
  for (const c of exact) {
    it(`${c.effect} at ${c.intensity * 100} %`, () => {
      const plan = voicePlan(c.effect, c.intensity)!
      const [yL, yR] = processStereo(plan, IN_L, IN_R, 0)
      let worst = 0
      for (const w of c.windows!) {
        for (let i = 0; i < w.L.length; i++) {
          worst = Math.max(worst, Math.abs(yL[w.start + i] - w.L[i]), Math.abs(yR[w.start + i] - w.R[i]))
        }
      }
      expect(worst).toBeLessThan(2e-5)
    })
  }
})

describe('the Hall impulse response', () => {
  it('is the one aevalsrc synthesizes, both channels', () => {
    const ir = DOC.reverb_ir
    const L = reverbIr(ir.params, 0)
    const R = reverbIr(ir.params, 1)
    expect(L.length).toBe(Math.round(VOICE_TABLE.reverb_ir_seconds * SR))
    let worst = 0
    for (const w of ir.windows) {
      for (let i = 0; i < w.L.length; i++) {
        worst = Math.max(worst, Math.abs(L[w.start + i] - w.L[i]), Math.abs(R[w.start + i] - w.R[i]))
      }
    }
    expect(worst).toBeLessThan(1e-6)
    expect(L[0]).toBeCloseTo(ir.params.dry, 6)
  })
})

describe('pitch presets (APPROX): the granular shifter against asetrate + atempo', () => {
  for (const c of DOC.cases.filter((x) => x.blocks)) {
    it(`${c.effect} at ${c.intensity * 100} %: centroid and level per 50 ms`, () => {
      const plan = voicePlan(c.effect, c.intensity)!
      const y = processStereo(plan, IN_L, IN_R, 0)[0].subarray(0, DOC.n)
      const got = blocks(y, DOC.block)
      const want = c.blocks!
      const n = Math.min(got.centroid.length, want.centroid.length) - 1      // the last block holds the tail
      const cErr: number[] = []
      const rErr: number[] = []
      for (let i = 1; i < n; i++) {                                          // block 0: the attack
        cErr.push(Math.abs(got.centroid[i] / want.centroid[i] - 1))
        rErr.push(Math.abs(got.rms_db[i] - want.rms_db[i]))
      }
      const mean = (a: number[]) => a.reduce((s, v) => s + v, 0) / a.length
      const bound = VOICE_FX_PARITY[c.effect as 'chipmunk' | 'deep' | 'monster']
      // The per-block numbers are the measurement support.ts records; they
      // ride in the assertion messages (no console output: house rule).
      const what = `${c.effect}@${c.intensity}: centroid err mean ${(100 * mean(cErr)).toFixed(2)} % `
        + `max ${(100 * Math.max(...cErr)).toFixed(2)} %, rms err mean ${mean(rErr).toFixed(2)} dB `
        + `max ${Math.max(...rErr).toFixed(2)} dB`
      expect(mean(cErr), what).toBeLessThan(bound.unitCentroidMean)
      expect(mean(rErr), what).toBeLessThan(bound.unitRmsMeanDb)
    })
  }
})

describe('blocks are the clip, sample for sample', () => {
  it('a block computed with its margins equals the same samples of the whole clip', () => {
    for (const id of ['chipmunk', 'monster', 'echo', 'underwater', 'megaphone', 'robot', 'vibrato']) {
      const plan = voicePlan(id, 1)!
      const [whole, wholeR] = processStereo(plan, IN_L, IN_R, 0)
      const a = 20000, b = 30000
      const e0 = Math.max(0, a - plan.back)
      const e1 = Math.min(DOC.n, b + plan.ahead)
      const [part, partR] = processStereo(plan, IN_L.subarray(e0, e1), IN_R.subarray(e0, e1), e0)
      let worst = 0
      for (let p = a; p < b; p++) {
        worst = Math.max(worst, Math.abs(part[p - e0] - whole[p]), Math.abs(partR[p - e0] - wholeR[p]))
      }
      expect(worst, id).toBeLessThan(1e-9)
    }
  })

  it('the vibrato table is ffmpeg\'s (a sine from 0 to 239 starting at its minimum)', () => {
    const t = vibratoTable(6)
    expect(t.length).toBe(8000)
    expect(t[0]).toBeCloseTo(0, 9)
    expect(Math.max(...t)).toBeCloseTo(239, 6)
  })

  it('the pitch margins cover a grain', () => {
    expect(voicePlan('chipmunk', 1)!.ahead).toBe(
      GRAIN / 2 + Math.max(PERIOD_WINDOW / 2, Math.ceil((GRAIN / 2) * (Math.pow(2, 9 / 12) - 1)) + PERIOD_MAX_OFFSET) + 2)
  })
})

// K2 (0.8.0 QA): the master limiter's APPROX ranges bounded a voice-effect
// clip by nothing (Infinity), so every voice effect — an EXACT echo too — on
// a project with a loudness target said "≈ Limiter on loud sound". The bound
// must only ever be too high: every preset, at several intensities, driven by
// hostile inputs, never tops it.
describe('the peak bound of a voice effect (limiter ranges, gate RX)', () => {
  const N = SR                                         // 1 s: the longest echo tap is 750 ms
  let seed = 12345
  const rnd = () => ((seed = (seed * 1103515245 + 12345) >>> 0) / 2 ** 32) * 2 - 1
  const inputs: Array<[string, Float32Array]> = [
    ['noise', Float32Array.from({ length: N }, () => rnd())],
    ['square 60 Hz', Float32Array.from({ length: N }, (_v, i) => (Math.sin((2 * Math.PI * 60 * i) / SR) >= 0 ? 1 : -1))],
    ['square 1 kHz', Float32Array.from({ length: N }, (_v, i) => (Math.sin((2 * Math.PI * 1000 * i) / SR) >= 0 ? 1 : -1))],
    ['sine 2 kHz', Float32Array.from({ length: N }, (_v, i) => Math.sin((2 * Math.PI * 2000 * i) / SR))],
    ['clicks', Float32Array.from({ length: N }, (_v, i) => (i % 12000 === 0 ? 1 : i % 12000 === 1 ? -1 : 0))],
    ['golden', Float32Array.from(IN_L.subarray(0, N), (v) => v / 0.93)],
  ]
  const peakOf = (x: ArrayLike<number>) => { let m = 0; for (let i = 0; i < x.length; i++) m = Math.max(m, Math.abs(x[i])); return m }

  for (const pre of VOICE_PRESETS) {
    it(`${pre.id}: never tops its bound`, () => {
      for (const intensity of [1, 0.5, 0.2]) {
        const plan = voicePlan(pre.id, intensity)!
        for (const [name, x] of inputs) {
          for (const scale of [1, 0.25]) {
            const inp = x.map((v) => v * scale)
            const [yL, yR] = processStereo(plan, inp, inp.map((v) => -v), 0)
            const out = Math.max(peakOf(yL), peakOf(yR))
            const bound = voicePeakBound({ ...plan, reverb: null }, peakOf(inp))
            expect(out, `${pre.id} ${intensity} ${name} ×${scale}`).toBeLessThanOrEqual(bound)
            expect(Number.isFinite(bound), `${pre.id}: bounded`).toBe(true)
          }
        }
      }
    }, 60000)
  }

  it('is the closed form where there is one', () => {
    const echo = voicePlan('echo', 1)!
    expect(voicePeakBound(echo, 0.1)).toBeCloseTo(0.1 * 0.85 * (1 + 0.5 + 0.25 + 0.125), 6)
    expect(voicePeakBound(voicePlan('deep', 1)!, 0.1)).toBeCloseTo(0.1, 6)    // the grains' windows sum to 1
    const rv = voicePlan('reverb', 1)!
    const l1 = Math.max(...[0, 1].map((ch) => reverbIr(rv.reverb!, ch).reduce((a, v) => a + Math.abs(v), 0)))
    expect(voicePeakBound(rv, 0.1) / (0.1 * l1)).toBeCloseTo(1, 5)            // the convolver is un-normalised
    expect(voicePeakBound(echo, Number.NaN)).toBe(Infinity)
  })
})
