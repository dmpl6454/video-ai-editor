import { describe, expect, it } from 'vitest'
import { clipGainAt, columnPeak, waveColumn } from './waveformDraw'

describe('columnPeak (QA-081)', () => {
  const w = { peaks: [0.1, 0.1, 0.9, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1], peaks_per_sec: 10, duration: 1 }

  it('a zoomed-out column shows the loudest peak it covers, not the one at its left edge', () => {
    // one column spanning 0.0–0.5 s covers peaks 0..4; the burst is peak 2
    expect(columnPeak(w, 0, 0.5)).toBe(0.9)
  })
  it('a zoomed-in column still reads the peak under it', () => {
    expect(columnPeak(w, 0.21, 0.215)).toBe(0.9)
    expect(columnPeak(w, 0.31, 0.315)).toBe(0.1)
  })
  it('outside the data is silence', () => {
    expect(columnPeak(w, 2, 3)).toBe(0)
    expect(columnPeak({ peaks: [], peaks_per_sec: 50, duration: 0 }, 0, 1)).toBe(0)
  })
})

describe('clipGainAt (QA-081)', () => {
  it('scales by gain_db', () => {
    expect(clipGainAt({ gain_db: -6 }, 1, 4)).toBeCloseTo(0.501, 3)
    expect(clipGainAt({ gain_db: 12 }, 1, 4)).toBeCloseTo(3.981, 3)
    expect(clipGainAt(undefined, 1, 4)).toBe(1)
  })
  it('follows volume automation (gain_env offsets on the gain_db trim), in clip-local time', () => {
    // The shape the backend stores (edl/schema.py AudioProps): a scalar
    // gain_db and dB offsets in gain_env — the inspector's own level model.
    const env = { keyframes: [[0, -20], [2, 0]] as [number, number][] }
    expect(clipGainAt({ gain_db: 0, gain_env: env }, 0, 4)).toBeCloseTo(0.1, 3)
    expect(clipGainAt({ gain_db: 0, gain_env: env }, 1, 4)).toBeCloseTo(Math.pow(10, -10 / 20), 3)
    expect(clipGainAt({ gain_db: 0, gain_env: env }, 3, 4)).toBeCloseTo(1, 3)
    // the trim moves the whole curve
    expect(clipGainAt({ gain_db: -6, gain_env: env }, 3, 4)).toBeCloseTo(Math.pow(10, -6 / 20), 3)
  })
  it('ramps linearly through the fades, like afade', () => {
    const a = { fade_in: 1, fade_out: 2 }
    expect(clipGainAt(a, 0, 10)).toBe(0)
    expect(clipGainAt(a, 0.5, 10)).toBeCloseTo(0.5)
    expect(clipGainAt(a, 5, 10)).toBe(1)
    expect(clipGainAt(a, 9, 10)).toBeCloseTo(0.5)
    expect(clipGainAt(a, 10, 10)).toBe(0)
  })
})

describe('waveColumn', () => {
  it('is proportional to peak × gain and flags clipping past full scale', () => {
    expect(waveColumn(0.5, 1, 20)).toEqual({ h: 10, clipped: false })
    expect(waveColumn(0.5, 0.5, 20).h).toBe(5)
    expect(waveColumn(0.5, 4, 20)).toEqual({ h: 20, clipped: true })
  })
})
