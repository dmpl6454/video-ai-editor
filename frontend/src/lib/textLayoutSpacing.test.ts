// QA-078 (wave C): letter spacing — rule 8 of the shared text layout model.
// The export side is pinned to the same fixture by tests/test_c6_letter_spacing.py.
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import {
  LETTER_SPACING_RANGE, graphemeClusters, letterSpacingOf, needsShaping, splitScripts,
  trackedUnits, trackedWidth,
} from './textLayout'

const LS = (JSON.parse(readFileSync(fileURLToPath(
  new URL('./__fixtures__/text_layout_cases.json', import.meta.url)), 'utf-8')) as {
  block: { letter_spacing: {
    RANGE: number[]
    width: { advances: number[]; tracked: boolean[]; spacing: number; width: number }[]
    units: { chunk: string; units: [string, boolean][] }[]
  } }
}).block.letter_spacing

describe('letter spacing contract (rule 8)', () => {
  it('range matches the export', () => {
    expect([...LETTER_SPACING_RANGE]).toEqual(LS.RANGE)
  })

  it('width = advances + spacing after every tracked unit but the last', () => {
    for (const c of LS.width) expect(trackedWidth(c.advances, c.tracked, c.spacing)).toBeCloseTo(c.width, 9)
  })

  it('splits a chunk into the same units as the export', () => {
    for (const c of LS.units) expect(trackedUnits(c.chunk)).toEqual(c.units)
  })

  it('never splits a complex-script run, keeps marks on their base', () => {
    expect(needsShaping('नमस्ते')).toBe(true)
    expect(needsShaping('HELLO')).toBe(false)
    expect(splitScripts('Hi नमस्ते World')).toEqual(['Hi ', 'नमस्ते ', 'World'])
    expect(graphemeClusters('café')).toEqual(['c', 'a', 'f', 'é'])
  })

  it('clamps the style value like the schema', () => {
    expect(letterSpacingOf(500)).toBe(100)
    expect(letterSpacingOf(-99)).toBe(-20)
    expect(letterSpacingOf(undefined)).toBe(0)
    expect(letterSpacingOf(Number.NaN)).toBe(0)
  })
})
