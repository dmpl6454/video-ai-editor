/**
 * What ⌘B / "Split at playhead" cuts, and WHERE in layout time.
 *
 * The playhead is RENDER time; `split_at` takes a LAYOUT `time`. The store
 * used the playhead for both the containment test (`c.start <= t <
 * clipEnd(c)`) and the dispatched time, on every lane. For v1 that was off by
 * the clip's per-clip pull; for overlay lanes it became wrong the moment they
 * were drawn and played at render positions: a caption at layout 20.0–23.0
 * after 1.5 s of consumed overlap is on screen at render 18.5, so with the
 * playhead at render 19.0 the layout test said "not inside the caption" and
 * cut v1 instead — at layout 19.0, 1.5 s before the frame shown.
 *
 * Pure, so the rule is a table test rather than a store mock: v1 decodes
 * through v1's own inverse (`v1TimeFromOutput` — a crossfade maps into clip
 * B's head, a slot not a coordinate), every other lane through `layoutTime`
 * (a crossfade snaps to the seam). Both are the inverses the Timeline already
 * uses for drops on those lanes, so a split lands where a drop would.
 */
import { clipEnd, type EDL } from '../types'
import { layoutTime, v1SeamsOf, v1TimeFromOutput } from './timelineLayout'

export interface SplitTarget {
  track: string
  /** LAYOUT time — what `split_at` takes. */
  time: number
}

/** The layout time `split_at(track, …)` needs for the playhead on `trackId`. */
export function splitTimeFor(edl: EDL | null | undefined, trackId: string, playhead: number): number {
  return trackId === 'v1'
    ? v1TimeFromOutput(edl, playhead)
    : layoutTime(v1SeamsOf(edl), playhead)
}

/**
 * One split per distinct track holding a SELECTED clip that contains the
 * playhead (tested in that track's own layout time); v1 when none does —
 * the historical default.
 */
export function splitTargets(
  edl: EDL | null | undefined, selected: ReadonlySet<string>, playhead: number,
): SplitTarget[] {
  const out: SplitTarget[] = []
  if (edl && selected.size) {
    for (const tk of edl.tracks) {
      const time = splitTimeFor(edl, tk.id, playhead)
      const hit = tk.clips.some((c) => selected.has(c.id) && c.start <= time && time < clipEnd(c))
      if (hit && !out.some((o) => o.track === tk.id)) out.push({ track: tk.id, time })
    }
  }
  if (!out.length) out.push({ track: 'v1', time: splitTimeFor(edl, 'v1', playhead) })
  return out
}
