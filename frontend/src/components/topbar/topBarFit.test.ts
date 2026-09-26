// The top bar's density steps (LEFT_RAIL_SPEC §1.4, §8.2): the viewport
// baseline, the one-step function and the bounded loop useTopBarFit runs in a
// layout effect. The measuring itself is a real-browser matter
// (tests/test_wave_d_topbar_ui.py, Chromium and WebKit).
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { MAX_DENSITY, baselineFor, densityClass, fitDensity, nextDensity, type Density } from './useTopBarFit'

describe('baselineFor (the viewport sets the starting step)', () => {
  it.each([
    [1920, 0], [1440, 0], [1439, 1], [1280, 1], [1279, 2], [1100, 2], [1099, 3], [1024, 3], [900, 3], [899, 3],
  ])('%i px starts at step %i', (w, d) => {
    expect(baselineFor(w)).toBe(d)
  })

  it('never starts at the content-driven step 4', () => {
    for (let w = 320; w <= 2560; w += 7) expect(baselineFor(w)).toBeLessThan(4)
  })
})

describe('nextDensity', () => {
  it('stays where the bar fits', () => {
    for (const d of [0, 1, 2, 3, 4] as Density[]) expect(nextDensity(d, true)).toBe(d)
  })
  it('steps one denser where it does not', () => {
    expect(nextDensity(0, false)).toBe(1)
    expect(nextDensity(3, false)).toBe(4)
  })
  it('never exceeds 4', () => {
    expect(nextDensity(4, false)).toBe(4)
  })
})

describe('fitDensity (the loop)', () => {
  /** A bar that fits from step `k` on; records every step it was asked about. */
  const barFitsFrom = (k: number) => {
    const asked: number[] = []
    return { asked, fitsAt: (d: Density) => { asked.push(d); return d >= k } }
  }

  it('stops at the first step that fits, measuring nothing past it', () => {
    const b = barFitsFrom(2)
    expect(fitDensity(0, b.fitsAt)).toBe(2)
    expect(b.asked).toEqual([0, 1, 2])
  })

  it('starts from the baseline: a bar that fits there is left alone', () => {
    const b = barFitsFrom(0)
    expect(fitDensity(3, b.fitsAt)).toBe(3)
    expect(b.asked).toEqual([3])
  })

  it('never exceeds 4 and is bounded when nothing fits', () => {
    const b = barFitsFrom(99)
    expect(fitDensity(0, b.fitsAt)).toBe(4)
    expect(b.asked.length).toBeLessThanOrEqual(MAX_DENSITY + 1)
    expect(Math.max(...b.asked)).toBe(4)
  })

  it('reaches step 4 only when step 3 does not fit (content-driven)', () => {
    expect(fitDensity(3, barFitsFrom(3).fitsAt)).toBe(3)
    expect(fitDensity(3, barFitsFrom(4).fitsAt)).toBe(4)
  })
})

describe('densityClass (cumulative, so each step keeps the ones before it)', () => {
  it('names every step up to d', () => {
    expect(densityClass(0)).toBe('')
    expect(densityClass(1)).toBe('tb-d1')
    expect(densityClass(3)).toBe('tb-d1 tb-d2 tb-d3')
    expect(densityClass(4)).toBe('tb-d1 tb-d2 tb-d3 tb-d4')
  })
})

describe('the density rules in styles.css hide WORDS, never controls', () => {
  const css = readFileSync(new URL('../../styles.css', import.meta.url), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '')
  const rules = [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
    .map((m) => ({ sel: m[1].trim(), body: m[2] }))
    .filter((r) => /\.tb-d\d/.test(r.sel))
  // What a step may take off screen: text inside a control, or a decoration.
  const WORDS = ['.topbar-btn-label', '.tb-err-text', '.tb-err-x', '.tb-applying-word', '.tb-stale-word',
    '.act-word', '.ratio-facts']

  it('has rules for all four steps', () => {
    for (const k of [1, 2, 3, 4]) expect(rules.some((r) => r.sel.includes(`.tb-d${k}`)), `tb-d${k}`).toBe(true)
  })

  it('display:none only ever targets words', () => {
    const hidden = rules.filter((r) => /display\s*:\s*none/.test(r.body))
      .flatMap((r) => r.sel.split(',').map((s) => s.trim().split(/\s+/).pop() ?? ''))
    expect(hidden.length).toBeGreaterThan(0)
    for (const target of hidden) expect(WORDS, target).toContain(target)
  })

  it('the wordmark is only ever visually hidden (it stays the page\'s h1)', () => {
    const brand = rules.filter((r) => r.sel.includes('.topbar-brand'))
    expect(brand.length).toBe(1)
    expect(brand[0].body).not.toMatch(/display\s*:\s*none/)
    expect(brand[0].body).toMatch(/clip-path\s*:\s*inset\(50%\)/)
  })
})
