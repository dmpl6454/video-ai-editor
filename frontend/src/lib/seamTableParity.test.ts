// Review RD1 (spec R3): the timeline UI, the engine and the fidelity
// classifier must share ONE seam table — framePlan's seam_table_for port —
// so they cannot drift. Structural pins (no second gap/match loop, no second
// render-time fold) plus a behavioural parity sweep.
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import { seamTableFor, type EdlClip, type EdlTransition } from './preview/timeline/framePlan'
import { seamTable, renderTime } from './timelineLayout'

const src = (rel: string) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8')

describe('one seam table (review RD1, spec R3)', () => {
  it('timelineLayout.seamTable adapts framePlan instead of re-implementing it', () => {
    const tl = src('./timelineLayout.ts')
    expect(tl).not.toMatch(/transitions\.find\(/)          // no second first-match lookup
    expect(tl).not.toMatch(/const GAP_EPS\b|const SEAM_TOL\b/)
    expect(tl).toMatch(/seamCharges\(/)
  })

  it('support.ts folds render time through timelineLayout.renderTime', () => {
    const sp = src('./preview/timeline/support.ts')
    expect(sp).not.toMatch(/seams\.reduce\(/)
    expect(sp).toMatch(/renderTime\(/)
  })

  it('the UI table and the engine table agree on every charged seam', () => {
    let seed = 11
    const rnd = () => { seed = (seed * 1103515245 + 12345) % 2 ** 31; return seed / 2 ** 31 }
    for (let trial = 0; trial < 300; trial++) {
      const fps = [30, 25, 30000 / 1001, 24][trial % 4]
      const clips: EdlClip[] = []
      let at = 0
      for (let i = 0; i < 2 + (trial % 5); i++) {
        const dur = 0.2 + rnd() * 3
        const speed = rnd() < 0.3 ? 2 : 1
        clips.push({ id: `c${i}`, start: at, in: 0, out: dur * speed, speed } as EdlClip)
        const gapOrOverlap = rnd() < 0.2 ? 0.4 : rnd() < 0.1 ? -0.0005 : 0
        at += dur + gapOrOverlap
      }
      const trs: EdlTransition[] = clips.slice(0, -1).filter(() => rnd() < 0.7).map((c) => ({
        at: (c.start ?? 0) + ((c.out ?? 0) - (c.in ?? 0)) / (c.speed as number) + (rnd() - 0.5) * 0.06,
        duration: rnd() < 0.2 ? undefined : rnd() * 1.5,
      } as EdlTransition))
      const engine = seamTableFor(clips, trs, fps)
      const ui = seamTable(clips.map((c) => ({
        id: c.id as string, start: c.start ?? 0, duration: ((c.out ?? 0) - (c.in ?? 0)) / (c.speed as number),
      })), trs.map((t) => ({ at: t.at, duration: t.duration ?? 0.5 })), fps)
      expect(ui.filter((s) => s.overlap > 0).map((s) => [s.boundary, s.overlap])).toEqual(engine)
      const t = rnd() * at
      const folded = t - engine.reduce((s, [b, d]) => (b <= t + 1e-6 ? s + d : s), 0)
      expect(renderTime(ui, t)).toBeCloseTo(folded, 12)
    }
  })
})
