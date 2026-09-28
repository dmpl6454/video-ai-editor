// A reversed SPEED-CURVE clip and its split / cut pieces (wave E, item 21):
// the browser port must read the same frames as render/frame_map.py, whose
// output tests/test_reverse_edit_ops.py pins to decoded exports. The pieces'
// intermediates sit on the source's absolute grid and open at a fractional
// `in` (framePlan.intermediateSpan / reversedViewRange), so a split is
// invisible here exactly as in the export.
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import type { EdlClip, EdlLike } from './framePlan'
import { intermediateSpan, reversedViewRange } from './framePlan'
import { sourceFromJson, type SourceInfoJson } from './frameMap'
import { buildProgramMap } from './programMap'

interface Case { name: string; edl: EdlLike; source: SourceInfoJson; frames: number[] }

const DOC: { cases: Case[] } = JSON.parse(readFileSync(
  fileURLToPath(new URL('../../../../../tests/goldens/reversed_curve_pieces.json', import.meta.url)), 'utf8'))

describe('reversed curve pieces: frameMap.ts reads what the export shows', () => {
  it.each(DOC.cases.map((c) => [c.name, c] as const))('%s', (_n, c) => {
    const src = sourceFromJson(c.source)
    const pm = buildProgramMap(c.edl, () => src)
    expect(Array.from(pm.srcFrame.slice(0, pm.total))).toEqual(c.frames)
  })

  it('a split never changes a frame', () => {
    const whole = new Map(DOC.cases.filter((c) => c.name.endsWith('/whole'))
      .map((c) => [c.name.slice(0, c.name.lastIndexOf('/')), c.frames]))
    const splits = DOC.cases.filter((c) => c.name.includes('/split@'))
    expect(splits.length).toBeGreaterThanOrEqual(12)
    for (const c of splits) expect(c.frames, c.name).toEqual(whole.get(c.name.slice(0, c.name.lastIndexOf('/'))))
  })
})

describe('the source-grid recipe', () => {
  const curve = { name: 'custom', curve: [[0, 1], [0.5, 0.2], [1, 1]] }
  const c: EdlClip = { id: 'c', src: 's', in: 2.0137, out: 8.4111, start: 0, reverse: true, speed: curve }

  it('spans [floor(in), ceil(out)) and opens where `out` sits', () => {
    expect(intermediateSpan(c, 30)).toEqual([2, 193])
    const [vIn, vOut] = reversedViewRange(c, 30)
    expect(Math.abs(vIn - (253 / 30 - 8.4111))).toBeLessThan(1e-12)
    expect(vOut - vIn).toBe(8.4111 - 2.0137)
  })

  it('leaves a constant-speed reversal on its own range', () => {
    const flat = { ...c, speed: 2 }
    expect(intermediateSpan(flat, 30)).toEqual([2.0137, 192])
    expect(reversedViewRange(flat, 30)).toEqual([0, 8.4111 - 2.0137])
  })
})
