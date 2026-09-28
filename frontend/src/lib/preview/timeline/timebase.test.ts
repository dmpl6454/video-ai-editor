// Parity with edl/timebase.py over tests/goldens/timebase_cases.json (spec R2):
// 100 % of the golden cases, not a sample.
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import {
  ceilToFrame, editSample, enableWindow, ffmpegMicros, ffmpegRate, floorToFrame, fpsFloat, frameDuration,
  frameOf, framesBetween, mulDivRoundAway, quantize, rateOf, rescale, samplesForFrames,
  seekPreroll, sourceRate, timeOf, _internal,
} from './timebase'

type Fps = number | null
interface Golden {
  grid: {
    times: number[]; fps: Fps[]; frames: number[]
    frame_of: number[][]; quantize: number[][]; floor_to_frame: number[][]
    ceil_to_frame: number[][]; seek_preroll: number[][]
    time_of: number[][]; samples_for_frames: number[][]; samples_44100: number[][]
  }
  rate_of: [Fps, [number, number]][]
  scalars: [Fps, number, string, number][]
  frames_between: [number, number, Fps, number][]
  enable_window: [number, number, Fps, [number, number]][]
  source_rate: [number | string | null, number | string | null, [number, number]][]
  ffmpeg_us: [number, number][]
  edit_sample: [number, number][]
}

const G: Golden = JSON.parse(readFileSync(fileURLToPath(
  new URL('../../../../../tests/goldens/timebase_cases.json', import.meta.url)), 'utf8'))

function gridMismatches(name: keyof Golden['grid'], fn: (t: number, f: Fps) => number): string[] {
  const out: string[] = []
  const rows = G.grid[name] as number[][]
  G.grid.fps.forEach((f, i) => {
    G.grid.times.forEach((t, j) => {
      const got = fn(t, f)
      if (!Object.is(got, rows[i][j]) && got !== rows[i][j]) out.push(`${name}(${t}, ${f}) = ${got}, python ${rows[i][j]}`)
    })
  })
  return out
}

describe('timebase.ts reproduces edl/timebase.py on every golden case', () => {
  it('rate_of, fps_float, ffmpeg_rate, frame_duration', () => {
    for (const [f, [num, den]] of G.rate_of) expect(rateOf(f), `rate_of(${f})`).toEqual({ num, den })
    for (const [f, ff, fr, fd] of G.scalars) {
      expect(fpsFloat(f)).toBe(ff)
      expect(ffmpegRate(f)).toBe(fr)
      expect(frameDuration(f)).toBe(fd)
    }
  })

  it.each([
    ['frame_of', frameOf],
    ['quantize', quantize],
    ['floor_to_frame', floorToFrame],
    ['ceil_to_frame', ceilToFrame],
    ['seek_preroll', seekPreroll],
  ] as const)('%s over the time × fps grid', (name, fn) => {
    const bad = gridMismatches(name, fn)
    expect(bad.slice(0, 10)).toEqual([])
    expect(G.grid.times.length * G.grid.fps.length).toBeGreaterThan(5000)
  })

  it('time_of and samples_for_frames over the frame grid', () => {
    G.grid.fps.forEach((f, i) => {
      G.grid.frames.forEach((n, j) => {
        expect(timeOf(n, f), `time_of(${n}, ${f})`).toBe(G.grid.time_of[i][j])
        expect(samplesForFrames(n, f)).toBe(G.grid.samples_for_frames[i][j])
        expect(samplesForFrames(n, f, 44100)).toBe(G.grid.samples_44100[i][j])
      })
    })
  })

  it('frames_between and enable_window', () => {
    for (const [a, b, f, n] of G.frames_between) expect(framesBetween(a, b, f)).toBe(n)
    for (const [a, b, f, w] of G.enable_window) expect(enableWindow(a, b, f)).toEqual(w)
  })

  it('source_rate', () => {
    for (const [avg, nom, [num, den]] of G.source_rate) {
      expect(sourceRate(avg, nom), `source_rate(${avg}, ${nom})`).toEqual({ num, den })
    }
  })

  it('the %.6f microseconds ffmpeg parses (half-even, unlike toFixed)', () => {
    for (const [x, us] of G.ffmpeg_us) expect(ffmpegMicros(x), `us(${x})`).toBe(us)
    // The trap in one line: toFixed rounds this exact tie up, Python down.
    expect((0.0078125).toFixed(6)).toBe('0.007813')
    expect(ffmpegMicros(0.0078125)).toBe(7812)
  })

  it('edit_sample: R9\'s one audio start rule, the nearest sample to the printed time', () => {
    expect(G.edit_sample.length).toBeGreaterThan(600)
    const bad = G.edit_sample.filter(([x, n]) => editSample(x) !== n).map(([x, n]) => `${x}: ${editSample(x)} ≠ ${n}`)
    expect(bad.slice(0, 10)).toEqual([])
  })
})

describe('exact helpers', () => {
  it('ties go to EVEN in frame_of — Math.round would say 23', () => {
    expect(frameOf(0.75, 30)).toBe(22)
    expect(Math.round(0.75 * 30)).toBe(23)
    expect(frameOf(0.25, 30)).toBe(8)
  })

  it('floatToQ is the exact binary value', () => {
    expect(_internal.floatToQ(0.1)).toEqual({ n: 3602879701896397n, d: 36028797018963968n })
    expect(_internal.floatToQ(-2.5)).toEqual({ n: -5n, d: 2n })
    expect(_internal.floatToQ(5e-324).d).toBe(1n << 1074n)
  })

  it('correctly rounded division agrees with the float division it replaces', () => {
    for (const [n, d] of [[1n, 3n], [2n, 3n], [1001n, 30000n], [123456789n, 1000n], [7n, 1n]]) {
      expect(_internal.correctlyRoundedDiv(n, d)).toBe(Number(n) / Number(d))
    }
    const big = 2n ** 80n + 1n
    expect(_internal.correctlyRoundedDiv(big, 3n)).toBe(Number(big) / 3)
  })

  it('rescale rounds half away from zero, like av_rescale_q', () => {
    expect(mulDivRoundAway(1, 1, 2)).toBe(1)
    expect(mulDivRoundAway(-1, 1, 2)).toBe(-1)
    expect(mulDivRoundAway(3, 1, 2)).toBe(2)
    expect(mulDivRoundAway(5, 1, 4)).toBe(1)
    expect(rescale(512, { num: 1, den: 15360 }, { num: 1, den: 30 })).toBe(1)
    // BigInt path: a product beyond 2^53.
    expect(mulDivRoundAway(2 ** 40, 2 ** 20, 3)).toBe(Math.round(2 ** 60 / 3))
  })

  it('rateOf snaps decimal NTSC spellings to the exact standard rate', () => {
    expect(rateOf(29.97)).toEqual({ num: 30000, den: 1001 })
    expect(rateOf(30000 / 1001)).toEqual({ num: 30000, den: 1001 })
    expect(rateOf(24)).toEqual({ num: 24, den: 1 })
    expect(rateOf({ num: 120, den: 1 })).toEqual({ num: 120, den: 1 })
    expect(rateOf(Number.NaN)).toEqual({ num: 30, den: 1 })
  })
})

describe('float fast paths are exact (review RD1: frameOf/timeOf run per overlay per rAF)', () => {
  const rates = [30, 29.97, 30000 / 1001, 25, 24, 23.976, 60, 59.94, 50, 48, 12.5, 7, 144,
    { num: 30000, den: 1001 }, { num: 24000, den: 1001 }, { num: 90000, den: 3001 }]
  it('frameOf equals the BigInt rational on random, tie and near-tie times', () => {
    let seed = 7
    const rnd = () => { seed = (seed * 1103515245 + 12345) % 2 ** 31; return seed / 2 ** 31 }
    for (const fps of rates) {
      const r = rateOf(fps)
      const times: number[] = []
      for (let i = 0; i < 3000; i++) times.push(rnd() * 7200)
      for (let k = 0; k < 400; k++) {
        const tie = ((k + 0.5) * r.den) / r.num       // exact half-frame times (displaySeekTime)
        times.push(tie, tie * (1 + 2 ** -52), tie * (1 - 2 ** -52), (k * r.den) / r.num)
      }
      times.push(0.75, 1e-9, 5e-324, 86400 * 3)
      for (const t of times) expect(frameOf(t, fps)).toBe(_internal.frameOfExact(t, fps))
    }
  })

  it('timeOf equals the correctly rounded BigInt quotient', () => {
    for (const fps of rates) {
      const r = rateOf(fps)
      for (const k of [1, 2, 3, 22, 1001, 30000, 123457, 2 ** 31, 2 ** 40]) {
        expect(timeOf(k, fps)).toBe(_internal.correctlyRoundedDiv(BigInt(k) * BigInt(r.den), BigInt(r.num)))
      }
    }
  })

  it('rateOf memoises without changing any answer', () => {
    for (const fps of [...rates, 0, -3, NaN, 1e9, 29.970029970029973]) {
      const a = rateOf(fps as number)
      expect(rateOf(fps as number)).toEqual(a)
      expect(a).toEqual(_internal.rateOfExact(fps as number))
    }
  })
})
