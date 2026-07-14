// Shared visual language for drag/selection/overlap feedback across the
// Timeline canvas and the preview StickerLayer, so "dragging" / "drop-ok" /
// "overlap" read identically wherever they appear. Colors come from the
// existing palette (--accent-2 blue, --accent red, the amber #f59e0b already
// used for the persisted-overlap dashed border) so nothing clashes.

export const ACCENT = '#5b8dff'            // --accent-2, the interactive blue
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
