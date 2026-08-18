// Shared visual language for drag/selection/overlap feedback across the
// Timeline canvas and the preview StickerLayer, so "dragging" / "drop-ok" /
// "overlap" read identically wherever they appear. Colors come from the
// existing palette (--accent-2 blue, --accent red, the amber #f59e0b already
// used for the persisted-overlap dashed border) so nothing clashes.

export const ACCENT = '#5b8dff'            // --accent-2, the interactive blue
export const ACCENT_BAD = '#ff4d6d'        // --accent red, solid (borders/strokes)
export const GHOST_ALPHA = 0.6             // dragged-clip ghost opacity
export const DROP_OK = 'rgba(91,141,255,0.10)'    // compatible drop-target wash
export const DROP_BAD = 'rgba(255,77,109,0.12)'   // incompatible-lane wash (--accent red)
export const OVERLAP_TINT = 'rgba(245,158,11,0.18)' // would-overlap region (amber)
export const DRAG_BORDER_W = 2             // ghost / dragging-box border px
export const INSERTION_W = 2               // landing/insertion line px

// Cursor for a corner handle at local sign (sx, sy) ∈ {-1,1}². Top-left and
// bottom-right share the NWSE diagonal; top-right and bottom-left share NESW.
export function cursorForCorner(sx: number, sy: number): string {
  return sx * sy > 0 ? 'nwse-resize' : 'nesw-resize'
}

// --- selection chrome, shared by every overlay kind -------------------------
// Text and stickers must look and behave identically when selected. These
// constants and the draw below are the single definition of that, so adding a
// third overlay kind (or tuning a handle size) can't make them diverge.

export const HANDLE = 7          // half-size of a corner handle, display px
export const HANDLE_HIT = 13     // click tolerance around a handle
export const DEL_R = 9           // radius of the ✕ delete handle, display px
export const DEL_GAP = 14        // gap between box corner and the ✕ center
export const ROT_R = 8           // radius of the rotate grip, display px
export const ROT_GAP = 26        // stem length from the box's TOP edge
export const ROT_HIT = 15        // click tolerance around the grip

const CORNER_SIGNS = [[-1, -1], [1, -1], [1, 1], [-1, 1]] as const

/**
 * Where the ROTATE grip sits, in the box's LOCAL (rotated) frame: on a short
 * stem above the top edge, the arrangement Word/PowerPoint/Canva all use, so
 * the gesture needs no explanation.
 *
 * Local, not screen, coordinates — the caller has already translated and
 * rotated into the box's frame, so the grip travels with the box as it turns
 * and stays "above the top edge" rather than "above on screen".
 *
 * `flip` moves it BELOW instead, for a box close enough to the canvas top that
 * the grip would be drawn off it — the same reasoning (and the same failure)
 * as the ✕ handle's corner search: chrome that renders outside the overlay
 * canvas is invisible and unclickable, so the control silently does not exist.
 */
export function rotateHandleLocal(hh: number, flip = false): { lx: number; ly: number } {
  return { lx: 0, ly: flip ? hh + ROT_GAP : -hh - ROT_GAP }
}

/**
 * Where to put the ✕ delete handle, in the box's LOCAL (rotated) frame.
 *
 * It wants to sit just OUTSIDE the top-right corner — but the overlay canvas
 * is exactly the preview box and clips anything drawn past its edges. A
 * sticker dropped near the top or right edge therefore had its ✕ rendered
 * off-canvas: invisible and unclickable, so the only way to remove it was the
 * Delete key ("there should be a cross button on the emoji to remove, I have
 * to press delete button on the keyboard").
 *
 * So: try each corner's OUTSIDE position, then each corner's INSIDE position,
 * and take the first that lands fully within the canvas. A big sticker
 * covering the whole frame still gets an inside-corner ✕. Exported (rather
 * than duplicated at the two call sites) because the draw and the hit test
 * MUST agree — a handle you can see but not click is the same bug again.
 */
export function deleteHandleLocal(
  box: { cx: number; cy: number; hw: number; hh: number; rot: number },
  canvasW: number, canvasH: number,
): { lx: number; ly: number } {
  const { cx, cy, hw, hh, rot } = box
  const cos = Math.cos(rot), sin = Math.sin(rot)
  const fits = (lx: number, ly: number) => {
    const x = cx + lx * cos - ly * sin
    const y = cy + lx * sin + ly * cos
    return x >= DEL_R && x <= canvasW - DEL_R && y >= DEL_R && y <= canvasH - DEL_R
  }
  // Preferred first: outside top-right, then the other three outside corners,
  // then the same four pulled inside the box.
  const order = [[1, -1], [-1, -1], [1, 1], [-1, 1]] as const
  for (const [sx, sy] of order) {
    const lx = sx * (hw + DEL_GAP), ly = sy * (hh + DEL_GAP)
    if (fits(lx, ly)) return { lx, ly }
  }
  for (const [sx, sy] of order) {
    const lx = sx * Math.max(0, hw - DEL_GAP), ly = sy * Math.max(0, hh - DEL_GAP)
    if (fits(lx, ly)) return { lx, ly }
  }
  return { lx: hw + DEL_GAP, ly: -hh - DEL_GAP }   // nothing fits — original spot
}

/** Draw the selection box, corner handles and (when idle) the ✕ delete handle
 *  for a box already translated/rotated into its own local frame by the caller. */
export function drawSelectionChrome(
  ctx: CanvasRenderingContext2D,
  hw: number, hh: number,
  opts: { dragging: boolean; resizing: boolean; showDelete: boolean
          deleteAt?: { lx: number; ly: number }
          showRotate?: boolean; rotateAt?: { lx: number; ly: number }
          rotating?: boolean },
): void {
  ctx.strokeStyle = ACCENT
  if (opts.dragging) {
    ctx.lineWidth = DRAG_BORDER_W
    ctx.setLineDash([])
    ctx.shadowColor = 'rgba(0,0,0,0.5)'
    ctx.shadowBlur = 8
  } else {
    ctx.lineWidth = 1.5
    ctx.setLineDash([4, 3])
  }
  ctx.strokeRect(-hw, -hh, hw * 2, hh * 2)
  ctx.setLineDash([])
  ctx.shadowBlur = 0
  ctx.fillStyle = ACCENT
  for (const [sx, sy] of CORNER_SIGNS) {
    const pad = opts.resizing ? HANDLE + 1 : HANDLE
    ctx.fillRect(sx * hw - pad, sy * hh - pad, pad * 2, pad * 2)
  }
  // The rotate grip: a stem from the top edge to a small circle. Drawn before
  // the ✕ early-return below so it survives on a box that has no delete
  // handle (a PIP), and hidden while MOVING for the same reason the ✕ is —
  // except during a rotate, where it is the thing being dragged.
  if (opts.showRotate && (!opts.dragging || opts.rotating)) {
    const at = opts.rotateAt ?? rotateHandleLocal(hh)
    const edgeY = at.ly < 0 ? -hh : hh
    ctx.strokeStyle = ACCENT
    ctx.lineWidth = 1.5
    ctx.beginPath()
    ctx.moveTo(0, edgeY)
    ctx.lineTo(at.lx, at.ly)
    ctx.stroke()
    ctx.beginPath()
    ctx.arc(at.lx, at.ly, opts.rotating ? ROT_R + 1 : ROT_R, 0, Math.PI * 2)
    ctx.fillStyle = opts.rotating ? ACCENT : 'rgba(20,20,24,0.9)'
    ctx.fill()
    ctx.strokeStyle = ACCENT
    ctx.lineWidth = 1.5
    ctx.stroke()
    // A circular arrow, so the grip reads as "turn me" rather than "resize me".
    ctx.beginPath()
    ctx.arc(at.lx, at.ly, ROT_R * 0.45, Math.PI * 0.25, Math.PI * 1.75)
    ctx.strokeStyle = opts.rotating ? '#fff' : ACCENT
    ctx.lineWidth = 1.6
    ctx.stroke()
  }

  // Hidden mid-gesture: a drag that ends over the ✕ must not read as a delete
  // click, and it cuts chrome noise while moving.
  if (!opts.showDelete || opts.dragging) return
  // Position comes from the caller (deleteHandleLocal) so draw and hit test
  // can never disagree; the literal below is only the no-canvas-size default.
  const dx = opts.deleteAt ? opts.deleteAt.lx : hw + DEL_GAP
  const dy = opts.deleteAt ? opts.deleteAt.ly : -hh - DEL_GAP
  ctx.beginPath()
  ctx.arc(dx, dy, DEL_R, 0, Math.PI * 2)
  ctx.fillStyle = 'rgba(20,20,24,0.9)'
  ctx.fill()
  ctx.lineWidth = 1.5
  ctx.strokeStyle = ACCENT
  ctx.stroke()
  const r = DEL_R * 0.42
  ctx.beginPath()
  ctx.moveTo(dx - r, dy - r); ctx.lineTo(dx + r, dy + r)
  ctx.moveTo(dx + r, dy - r); ctx.lineTo(dx - r, dy + r)
  ctx.lineWidth = 1.8
  ctx.strokeStyle = '#fff'
  ctx.stroke()
}
