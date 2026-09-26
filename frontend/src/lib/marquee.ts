// Box ("marquee") selection on the timeline (QA-116).
//
// A drag on empty lane space only moved the playhead; there was no way to
// select several clips but one Shift-click at a time. A press on empty space
// that travels further than MARQUEE_SLOP_PX becomes a box; releasing selects
// every clip whose DRAWN rect the box touches (Shift/⌘ adds to the selection).
// A press that does not travel stays what it was: deselect and seek.

export const MARQUEE_SLOP_PX = 4

export interface Rect { x0: number; y0: number; x1: number; y1: number }

export interface MarqueeBox { clipId: string; x: number; y: number; w: number; h: number }

/** The box between two content points, normalised (x0 ≤ x1, y0 ≤ y1). */
export function marqueeRect(ax: number, ay: number, bx: number, by: number): Rect {
  return { x0: Math.min(ax, bx), y0: Math.min(ay, by), x1: Math.max(ax, bx), y1: Math.max(ay, by) }
}

/** Has the pointer travelled far enough from the press to be a box? */
export function isMarqueeDrag(ax: number, ay: number, bx: number, by: number): boolean {
  return Math.hypot(bx - ax, by - ay) >= MARQUEE_SLOP_PX
}

/** Ids of the clips the box touches, in `boxes` order. `inset` is the clip's
 *  vertical inset inside its row (the drawn rect, not the whole row). */
export function marqueeHits(r: Rect, boxes: readonly MarqueeBox[], inset: number): string[] {
  const out: string[] = []
  for (const b of boxes) {
    const top = b.y + inset
    const bottom = b.y + b.h - inset
    if (b.x <= r.x1 && b.x + b.w >= r.x0 && top <= r.y1 && bottom >= r.y0 && !out.includes(b.clipId)) {
      out.push(b.clipId)
    }
  }
  return out
}
