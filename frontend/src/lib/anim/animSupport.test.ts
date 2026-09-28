// Fidelity class of v1 clip animations (wave E, F1; support.ts): geometry and
// opacity presets add NO reason (EXACT, measured by
// tests/wk/test_clip_anim_parity.py); Blur In / Out is BAKED over exactly its
// own window.
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import type { EdlLike } from '../preview/timeline/framePlan'
import type { SourceInfoJson } from '../preview/timeline/frameMap'
import { buildProgramMap, lookupFromJson } from '../preview/timeline/programMap'
import { ANIM_APPROX, animBlurWindows, classify, MODE_APPROX, MODE_BAKED, MODE_EXACT } from '../preview/timeline/support'
import { frameOf } from '../preview/timeline/timebase'
import { ANIM_TABLE } from './clipAnim'

interface Case { name: string; edl: EdlLike; sources: Record<string, SourceInfoJson> }
const cases: Case[] = JSON.parse(readFileSync(fileURLToPath(
  new URL('../../../../tests/goldens/frame_map/structure.json', import.meta.url)), 'utf8')).cases
const base = cases.find((c) => c.name === 'gaps_p30') ?? cases[0]

function build(edit: (clips: Array<Record<string, unknown>>) => void) {
  const edl = structuredClone(base.edl)
  const v1 = edl.tracks!.find((t) => t.id === 'v1')!
  edit(v1.clips as Array<Record<string, unknown>>)
  return { edl, pm: buildProgramMap(edl, lookupFromJson(base.sources)) }
}

describe('clip animation fidelity classes', () => {
  it('geometry / opacity presets are EXACT, the measured ones APPROX over their window only', () => {
    for (const kind of ['in', 'out'] as const) {
      for (const p of ANIM_TABLE[kind]) {
        if (p.id.startsWith('blur')) continue
        const { edl, pm } = build((cl) => { cl[0][kind === 'in' ? 'anim_in' : 'anim_out'] = p.id })
        const k0 = pm.clipStart[0]
        const n = pm.clipLen[0]
        for (const phase of [1, 3, 5] as const) {
          const s = classify(pm, edl, { phase })
          const inWindow = kind === 'in' ? k0 + 1 : k0 + n - 2
          const mid = k0 + Math.floor(n / 2)
          if (ANIM_APPROX[kind].has(p.id)) {
            expect(s.mode[inWindow]).toBe(MODE_APPROX)
            expect(s.ranges.find((r) => r.mode === MODE_APPROX)!.reasons).toEqual([`anim:${kind}:${p.id}`])
          } else {
            expect(s.mode[inWindow]).toBe(MODE_EXACT)
          }
          expect(s.mode[mid]).toBe(MODE_EXACT)
        }
      }
    }
    for (const p of ANIM_TABLE.combo) {
      const { edl, pm } = build((cl) => { cl[0].anim_combo = p.id })
      expect(classify(pm, edl, { phase: 1 }).mode[pm.clipStart[0]]).toBe(MODE_EXACT)
    }
  })

  it('the APPROX set is the measured one (tests/wk/test_clip_anim_parity.py ANIM_APPROX)', () => {
    expect([...ANIM_APPROX.in].sort()).toEqual(['spin', 'zoom_out'])
    expect([...ANIM_APPROX.out].sort()).toEqual(['spin'])
  })

  it('Blur In / Out is BAKED over its own window and nowhere else', () => {
    const { edl, pm } = build((cl) => { cl[0].anim_in = 'blur_in'; cl[0].anim_out = 'blur_out' })
    const s = classify(pm, edl, { phase: 1 })
    const c = pm.clips[0]
    const [[a0, a1], [b0, b1]] = animBlurWindows(c)
    const fps = pm.R
    const k0 = pm.clipStart[0]
    const baked = [...s.mode.keys()].filter((k) => s.mode[k] === MODE_BAKED)
    const want = [
      ...Array.from({ length: frameOf(a1, fps) + 1 - frameOf(a0, fps) }, (_, i) => k0 + frameOf(a0, fps) + i),
      ...Array.from({ length: frameOf(b1, fps) + 1 - frameOf(b0, fps) }, (_, i) => k0 + frameOf(b0, fps) + i),
    ].filter((k) => k < pm.total)
    expect(baked).toEqual([...new Set(want)].sort((x, y) => x - y))
    expect(s.ranges.find((r) => r.mode === MODE_BAKED)!.reasons).toEqual(['anim:blur'])
  })
})
