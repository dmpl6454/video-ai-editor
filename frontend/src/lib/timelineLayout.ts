/**
 * Where v1 clips actually LAND in the rendered output.
 *
 * A clip's `start` is its position in the EDL's own coordinate space, and for
 * every track except v1 that is also its position in the output. v1 is the
 * exception: `xfade` plays two clips at once for the transition's duration, so
 * the renderer emits them OVERLAPPED (`compositor.py`: `cur_dur = cur_dur +
 * seg_dur[i] - tdur`). Every clip after a transition therefore plays EARLIER
 * than its `start` says, and the whole timeline ends sooner.
 *
 * `EDL.recompute_duration()` already accounts for that, so the transport and
 * the ruler are right — but `Timeline.tsx` drew each clip rectangle at raw
 * `start * zoom`, which is the un-overlapped position. On a real report: an 8s
 * clip split at 2/4/6 with transitions at 2.0 and 4.0 renders 7.0s, and the
 * strip was drawn out to 8.0s, so the last second was unreachable AND — worse
 * than the stray tail — every clip after the first transition sat up to a
 * second right of where it plays, putting the playhead over clip 3 while the
 * preview showed clip 4.
 *
 * This mirrors `EDL.transition_overlap()` in `edl/schema.py` rule for rule.
 * It has to: if the two ever disagree, the canvas promises a layout the
 * renderer will not produce, which is the whole class of bug this replaces.
 * Kept pure and unit-tested here rather than inline in the component for the
 * same reason `frameWalk.ts` is — it is sign-and-ordering logic that a test
 * catches instantly and a screenshot does not.
 */

export interface LayoutClip {
  id: string
  start: number
  /** EFFECTIVE timeline duration — (out - in) / speed, i.e. `clipDuration()`. */
  duration: number
}

export interface LayoutTransition {
  at: number
  duration: number
}

export interface SeamLayout {
  /** The boundary in EDL time — matches `v1Cuts[].at`. */
  boundary: number
  /** Seconds the two clips overlap here (0 for a hard cut). */
  overlap: number
  /** Where to draw the seam affordance, in OUTPUT time. */
  outAt: number
}

export interface V1Layout {
  /** clip id -> seconds it is pulled LEFT of its `start`. */
  shift: Map<string, number>
  seams: SeamLayout[]
}

// Same 1ms tolerance as compositor._GAP_EPS, and the same 0.05s boundary
// match the renderer and `transition_overlap()` use. Duplicated rather than
// shared because this is the browser side of the same rule.
const GAP_EPS = 0.001
const SEAM_TOL = 0.05

export function v1Layout(
  clips: LayoutClip[], transitions: LayoutTransition[],
): V1Layout {
  const shift = new Map<string, number>()
  const seams: SeamLayout[] = []
  const sorted = [...clips].sort((a, b) => a.start - b.start)
  if (sorted.length === 0) return { shift, seams }

  let cum = 0
  shift.set(sorted[0].id, 0)
  for (let i = 0; i < sorted.length - 1; i++) {
    const cur = sorted[i]
    const nxt = sorted[i + 1]
    const boundary = cur.start + cur.duration
    let overlap = 0
    // A positive GAP is what makes `_v1_segments` insert black filler, and the
    // renderer leaves that seam a hard cut — so no transition applies and
    // nothing is removed. An OVERLAP is not a gap: the renderer packs it with
    // `max(cursor, start)` and the transition IS applied. Testing `abs()` here
    // would wrongly skip a legacy overlapping pair the renderer does charge.
    if (nxt.start - boundary <= GAP_EPS) {
      // First match wins, as in `transition_overlap()`.
      const m = transitions.find((tr) => Math.abs(tr.at - boundary) < SEAM_TOL)
      if (m) {
        // Never claim more than the shorter side can give — xfade cannot
        // overlap further than a clip is long.
        overlap = Math.max(0, Math.min(m.duration, cur.duration, nxt.duration))
      }
    }
    // The seam sits at the MIDDLE of the overlap in output time; with no
    // transition the two edges coincide and this is just the cut position.
    seams.push({ boundary, overlap, outAt: boundary - cum - overlap / 2 })
    cum += overlap
    shift.set(nxt.id, cum)
  }
  return { shift, seams }
}

/** Convenience: a clip's start in OUTPUT time. */
export function outStart(clip: LayoutClip, shift: Map<string, number>): number {
  return clip.start - (shift.get(clip.id) ?? 0)
}

/**
 * The inverse: an OUTPUT time (what a pointer position means once the canvas
 * draws shifted clips) back to the EDL time a tool argument needs.
 *
 * Needed because `move_clip`/`add_clip` take EDL `start`, while the pointer is
 * now over output coordinates. Without it, dropping media onto a v1 lane that
 * already has transitions lands the clip earlier than where it was dropped, by
 * the accumulated overlap. Piecewise: the applicable shift is that of the last
 * clip starting at or before this point, and past the end it is the total.
 */
export function edlTimeFromOutput(
  outTime: number, clips: LayoutClip[], shift: Map<string, number>,
): number {
  const sorted = [...clips].sort((a, b) => a.start - b.start)
  let applicable = 0
  for (const c of sorted) {
    if (outStart(c, shift) <= outTime + 1e-9) applicable = shift.get(c.id) ?? 0
    else break
  }
  return outTime + applicable
}
