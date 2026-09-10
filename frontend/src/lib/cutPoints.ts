// Where a transition can go: the cut points on v1 and which one an edit
// targets. Pure, so the Timeline's affordances and the Transitions panel's
// "apply to this cut" agree on the same list — the two used to be computed
// in one place (Timeline.tsx) and the panel would have duplicated it.
//
// A cut is a pair of temporally-adjacent media clips: `next.start` within
// 0.05 s of the current clip's EFFECTIVE end ((out-in)/speed — the backend
// ripples neighbours to that, so raw out-in misplaced retimed clips). `at`
// is EDL time, which is what add/remove_transition take; the timeline draws
// the seam elsewhere once transitions overlap upstream (lib/timelineLayout).
// The existing transition is matched by |tr.at − at| < 0.05, last match
// wins — the compositor's own boundary matcher lets later entries overwrite
// earlier ones, so "last wins" is what actually renders.

import { clipEnd, isMediaClip, type Clip, type EDL } from '../types'

export interface TransitionRecord { at: number; type: string; duration: number }

export interface CutPoint {
  /** EDL seconds of the boundary. */
  at: number
  /** The clip ending here and the clip starting here. */
  before: Clip
  after: Clip
  /** The transition already on this cut, if any. */
  tr: TransitionRecord | null
}

// Same tolerance the render compositor and `transition_overlap()` use.
const SEAM_TOL = 0.05

/** Every v1 cut in timeline order. `[]` without a v1 track or with one clip. */
export function v1CutPoints(edl: EDL | null | undefined): CutPoint[] {
  const v1 = (edl?.tracks ?? []).find((t) => t.id === 'v1')
  if (!v1) return []
  // types.ts's Track doesn't declare `transitions` (a hand-mirrored schema,
  // incomplete on purpose) — read via the repo's established cast pattern.
  const trs = (v1 as unknown as { transitions?: TransitionRecord[] }).transitions ?? []
  const media = v1.clips.filter(isMediaClip).slice().sort((a, b) => a.start - b.start)
  const cuts: CutPoint[] = []
  for (let i = 0; i < media.length - 1; i++) {
    const end = clipEnd(media[i])
    if (Math.abs(media[i + 1].start - end) > SEAM_TOL) continue
    let match: TransitionRecord | null = null
    for (const tr of trs) if (Math.abs(tr.at - end) < SEAM_TOL) match = tr
    cuts.push({ at: end, before: media[i], after: media[i + 1], tr: match })
  }
  return cuts
}

/**
 * The cut an edit targets, and why. Selection wins — a selected v1 clip
 * targets the cut it STARTS at (its leading edge; for the first clip, its
 * trailing edge). With nothing selected the nearest cut to the playhead is
 * the target — the CapCut model, where a transition drops onto the seam
 * the playhead sits on. `null` when there is no cut at all.
 */
export function targetCut(
  cuts: CutPoint[], selection: string | null, playhead: number,
): { cut: CutPoint; index: number; reason: 'selection' | 'playhead' } | null {
  if (!cuts.length) return null
  if (selection) {
    let i = cuts.findIndex((c) => c.after.id === selection)
    if (i === -1) i = cuts.findIndex((c) => c.before.id === selection)
    if (i !== -1) return { cut: cuts[i], index: i, reason: 'selection' }
  }
  let best = 0
  for (let i = 1; i < cuts.length; i++) {
    if (Math.abs(cuts[i].at - playhead) < Math.abs(cuts[best].at - playhead)) best = i
  }
  return { cut: cuts[best], index: best, reason: 'playhead' }
}

/** "12.40 s" — the label the panel and the popover print for a cut. */
export const formatCutTime = (at: number): string => `${at.toFixed(2)} s`
