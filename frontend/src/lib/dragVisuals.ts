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

const CORNER_SIGNS = [[-1, -1], [1, -1], [1, 1], [-1, 1]] as const

/** Draw the selection box, corner handles and (when idle) the ✕ delete handle
 *  for a box already translated/rotated into its own local frame by the caller. */
export function drawSelectionChrome(
  ctx: CanvasRenderingContext2D,
  hw: number, hh: number,
  opts: { dragging: boolean; resizing: boolean; showDelete: boolean },
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
  // Hidden mid-gesture: a drag that ends over the ✕ must not read as a delete
  // click, and it cuts chrome noise while moving.
  if (!opts.showDelete || opts.dragging) return
  const dx = hw + DEL_GAP, dy = -hh - DEL_GAP
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
