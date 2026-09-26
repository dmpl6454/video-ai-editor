// Transition affordances only where they are wanted (QA-051 hover remainder).
//
// A bowtie was drawn on EVERY cut of Main video, so at fit zoom a 70-clip
// timeline was half circles and each one sat on the trim zone it shares. Now
// a cut that HAS a transition always shows its (filled) bowtie; an empty cut
// shows its hollow one only while the pointer is near it on the Main video
// row — the same place a click would add a transition.

export const CUT_HOVER_PX = 16

export interface CutLike { at: number; cx: number; cy: number; hasTransition: boolean }

/** The untransitioned cut the pointer is near on the row [rowTop, rowTop+rowH), or null. */
export function hoveredCut<C extends CutLike>(
  x: number, y: number, cuts: readonly C[], rowTop: number, rowH: number,
): C | null {
  if (y < rowTop || y > rowTop + rowH) return null
  let best: C | null = null
  let bestD = Infinity
  for (const c of cuts) {
    if (c.hasTransition) continue
    const d = Math.abs(x - c.cx)
    if (d <= CUT_HOVER_PX && d < bestD) { best = c; bestD = d }
  }
  return best
}

/** The cuts whose bowtie is drawn: every transitioned one, plus the hovered one. */
export function drawnCuts<C extends CutLike>(cuts: readonly C[], hoveredAt: number | null): C[] {
  return cuts.filter((c) => c.hasTransition || (hoveredAt !== null && Math.abs(c.at - hoveredAt) < 1e-6))
}
