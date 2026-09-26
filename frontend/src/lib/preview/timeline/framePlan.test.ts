// Parity with the compositor's LAYOUT rules over 500 EDLs produced by random
// edit sequences through the real dispatch (tests/goldens/frame_plan_cases.json,
// written by tests/test_frame_plan_golden.py): clip_frames, seam_table_for
// (with and without whole-frame rounding), the layout extent, _v1_frame_plan
// and the whole program map (RLE, seams, sound placement).
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import { seamTable } from '../../timelineLayout'
import {
  clipFrames, effectiveDuration, planView, seamCost, seamTableFor, v1FramePlan, v1Transitions,
  videoClips, type EdlLike,
} from './framePlan'
import type { SourceInfoJson } from './frameMap'
import { audioJson, audioTotalSamples, buildProgramMap, lookupFromJson, toRle, type Run } from './programMap'

interface PlanCase {
  seq: number
  edl: EdlLike
  clip_frames: number[]
  seams: [number, number][]
  seams_nofps: [number, number][]
  total_duration: number
  plan: [string, number | null, number][]
  model: { R: [number, number]; T: number | null; total: number; runs: Run[]; seams: unknown[]; audio: unknown[]; audio_total: number }
}

const DOC: { sources: Record<string, SourceInfoJson>; cases: PlanCase[] } = JSON.parse(readFileSync(
  fileURLToPath(new URL('../../../../../tests/goldens/frame_plan_cases.json', import.meta.url)), 'utf8'))
const lookup = lookupFromJson(DOC.sources)

describe('frame plan corpus (500 dispatch-made EDLs)', () => {
  it('is the full corpus', () => {
    expect(DOC.cases).toHaveLength(500)
  })

  it.each(DOC.cases.map((c) => [c.seq, c] as const))('sequence %i', (_seq, c) => {
    const fps = c.edl.canvas!.fps!
    const clips = videoClips(c.edl)
    expect(clips.map((x) => clipFrames(x, fps))).toEqual(c.clip_frames)
    const trs = v1Transitions(c.edl)
    expect(seamTableFor(clips, trs, fps)).toEqual(c.seams)
    expect(seamTableFor(clips, trs)).toEqual(c.seams_nofps)
    const view = planView(c.edl)
    // planView plans from the reversed-intermediate timing; the corpus
    // records the plan of the EDL as stored, so recompute that directly.
    expect(v1FramePlan(clips, c.total_duration, fps).map((s) => [s.kind, s.clip, s.frames]))
      .toEqual(c.plan)
    if (!clips.some((x) => x.reverse)) expect(view.totalDuration).toBe(c.total_duration)
    const pm = buildProgramMap(c.edl, lookup)
    expect([pm.R.num, pm.R.den]).toEqual(c.model.R)
    expect(pm.T).toBe(c.model.T)
    expect(pm.total).toBe(c.model.total)
    expect(toRle(pm)).toEqual(c.model.runs)
    expect(pm.seams).toEqual(c.model.seams)
    expect(audioJson(pm, lookup)).toEqual(c.model.audio)
    expect(audioTotalSamples(pm)).toBe(c.model.audio_total)
  })
})

describe('timelineLayout.seamTable(…, fps) charges what the renderer xfades', () => {
  it('equals seam_table_for(fps) on every charged seam of the corpus', () => {
    let checked = 0
    for (const c of DOC.cases) {
      if (!c.seams.length) continue
      const fps = c.edl.canvas!.fps!
      const clips = videoClips(c.edl)
      const layout = seamTable(clips.map((x) => ({ id: x.id, start: x.start ?? 0, duration: effectiveDuration(x) })),
        v1Transitions(c.edl).map((t) => ({ at: t.at, duration: t.duration ?? 0.5 })), fps)
      const charged = layout.filter((s) => s.overlap > 0).map((s) => [s.boundary, s.overlap])
      expect(charged).toEqual(c.seams)
      checked += charged.length
    }
    expect(checked).toBeGreaterThan(100)
  })

  it('rounds an off-grid cost to whole frames and never past the shorter side', () => {
    expect(seamCost(0.5, 2, 25)).toBe(0.48)            // 12.5 frames → 12 (half-even)
    expect(seamCost(0.37, 2, 30)).toBeCloseTo(11 / 30, 15)
    expect(seamCost(0.5, 0.3, 30)).toBeCloseTo(9 / 30, 15)   // clamped, whole frames
    expect(seamCost(0.5, 0.01, 30)).toBe(0)            // shorter than a frame: a cut
    expect(seamCost(0.37, 2)).toBe(0.37)               // no fps: raw seconds (old callers)
  })
})
