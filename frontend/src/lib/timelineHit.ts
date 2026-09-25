// What is under the pointer on the timeline canvas — ONE answer shared by the
// mousedown gesture and the hover cursor (QA-051 / QA-052).
//
// The canvas was `cursor: crosshair` everywhere, so nothing showed where a
// trim handle was. And the transition bowtie (r≈8, centred on the cut at mid
// row) was hit-tested before the 6 px trim zones, which it covers completely
// at mid height: dragging a clip's end there opened "Add transition" and
// never trimmed. Now a grab inside a trim zone is a TRIM; if it is also on the
// bowtie it carries that cut, and a release without movement opens the
// transition popover instead — click adds a transition, drag trims.

export interface HitBox {
  trackId: string
  clipId: string
  x: number; y: number; w: number; h: number
  locked: boolean
}

export interface CutMark { at: number; cx: number; cy: number; hasTransition: boolean }

export interface HitGeometry {
  labelWidth: number
  /** Top of the (pinned) ruler in content coordinates, and its height. */
  rulerTop: number
  rulerHeight: number
  playheadX: number
  /** Vertical inset of a clip's drawn rect inside its row. */
  clipInset: number
}

export type Hit =
  | { kind: 'label' }
  | { kind: 'ruler' }
  | { kind: 'playhead' }
  | { kind: 'transition'; cut: CutMark }
  | { kind: 'trim-l' | 'trim-r'; box: HitBox; cut: CutMark | null }
  | { kind: 'move'; box: HitBox }
  | { kind: 'empty' }

export const TRIM_EDGE_PX = 6
export const CUT_RADIUS_PX = 8
const PLAYHEAD_GRAB_PX = 5

/** Width of a clip's trim zones: 6 px, or a third of a clip too narrow for
 *  that, so a sliver still has a body to move by. */
export function trimEdge(w: number): number {
  return Math.max(1, Math.min(TRIM_EDGE_PX, w / 3))
}

function onCut(x: number, y: number, cuts: readonly CutMark[]): CutMark | null {
  for (const c of cuts) {
    const dx = x - c.cx
    const dy = y - c.cy
    if (dx * dx + dy * dy <= CUT_RADIUS_PX * CUT_RADIUS_PX) return c
  }
  return null
}

export function hitTest(x: number, y: number, g: HitGeometry,
                        boxes: readonly HitBox[], cuts: readonly CutMark[]): Hit {
  if (x < g.labelWidth) return { kind: 'label' }
  if (y >= g.rulerTop && y < g.rulerTop + g.rulerHeight) return { kind: 'ruler' }
  const inBox = (b: HitBox) => x >= b.x && x <= b.x + b.w && y >= b.y + g.clipInset && y <= b.y + b.h - g.clipInset
  const under = boxes.filter(inBox)
  const cut = onCut(x, y, cuts)
  // 1) A trim zone wins over the bowtie and the playhead line. At a cut the
  //    two neighbours' zones meet: left of the seam trims the outgoing clip's
  //    tail, right of it the incoming clip's head (under a crossfade the drawn
  //    rects overlap, and each clip's own zone still decides).
  for (const b of under) {
    if (b.locked) continue
    const e = trimEdge(b.w)
    if (x <= b.x + e) return { kind: 'trim-l', box: b, cut }
    if (x >= b.x + b.w - e) return { kind: 'trim-r', box: b, cut }
  }
  if (cut) return { kind: 'transition', cut }
  if (Math.abs(x - g.playheadX) <= PLAYHEAD_GRAB_PX) return { kind: 'playhead' }
  if (under.length) return { kind: 'move', box: under[0] }
  return { kind: 'empty' }
}

/** The pointer cursor for what is under it. */
export function cursorFor(hit: Hit, dragging = false): string {
  switch (hit.kind) {
    case 'trim-l':
    case 'trim-r': return 'ew-resize'
    case 'move': return hit.box.locked ? 'not-allowed' : (dragging ? 'grabbing' : 'grab')
    case 'transition': return 'pointer'
    case 'ruler':
    case 'playhead': return 'col-resize'
    default: return 'default'
  }
}
