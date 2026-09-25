// Viewport-sized timeline canvases (QA-024).
//
// The timeline used to size its canvases to the WHOLE timeline — backing store
// `contentW * dpr` wide — and let the wrapper scroll them natively. A 12-min
// project at the default 80 px/s is ~58,000 CSS px, i.e. ~115,000 device px at
// dpr 2, far past Chromium's 32,767 px canvas limit (and WKWebView's area
// limit): the 2D context is lost, the track area goes solid white, and it stays
// white even after the width shrinks again, because the draw effect keeps
// reusing the dead context. Zooming an 85 s clip to 600 px/s did it at dpr 1.
//
// Now each canvas is only as wide as the visible pane and sticks to it, a
// content-sized spacer provides the scroll extent, and drawing happens in
// CONTENT coordinates under a translate of −scrollLeft — so every x the draw
// loop, the hit-testing and the drag math compute is unchanged. These helpers
// hold the arithmetic so it can be tested without a DOM.

/** Backing-store and CSS size for a canvas that covers the visible pane. */
export function viewportCanvasSize(
  viewW: number, contentW: number, contentH: number, dpr: number,
): { cssW: number; cssH: number; pxW: number; pxH: number } {
  const cssW = Math.max(1, Math.min(viewW, contentW))
  const cssH = Math.max(1, contentH)
  return {
    cssW, cssH,
    pxW: Math.max(1, Math.round(cssW * dpr)),
    pxH: Math.max(1, Math.round(cssH * dpr)),
  }
}

/** The 2D transform that draws CONTENT coordinates into a viewport canvas
 *  scrolled to `scrollX`: [a, b, c, d, e, f] for ctx.setTransform. */
export function contentTransform(dpr: number, scrollX: number): [number, number, number, number, number, number] {
  return [dpr, 0, 0, dpr, -scrollX * dpr, 0]
}

/** Columns [from, to) of a span starting at content x `x`, `w` px wide, that
 *  fall inside the visible content range [viewL, viewR). Empty → from >= to. */
export function visibleColumns(x: number, w: number, viewL: number, viewR: number): [number, number] {
  const from = Math.max(0, Math.floor(viewL - x))
  const to = Math.min(Math.floor(w), Math.ceil(viewR - x))
  return [from, Math.max(from, to)]
}

/** Whether a span [x, x+w] intersects the visible range (with a small margin
 *  for strokes and handles drawn just outside a clip's rect). */
export function spanVisible(x: number, w: number, viewL: number, viewR: number, margin = 8): boolean {
  return x + w >= viewL - margin && x <= viewR + margin
}

/** Ruler tick times whose x (labelWidth + t·zoom) lies in [viewL, viewR],
 *  on the global `tickSec` grid (never accumulated, so no drift), up to maxT. */
export function visibleTicks(
  viewL: number, viewR: number, labelWidth: number, zoom: number, tickSec: number, maxT: number,
): number[] {
  if (!(zoom > 0) || !(tickSec > 0)) return []
  const first = Math.max(0, Math.floor((viewL - labelWidth) / zoom / tickSec))
  const out: number[] = []
  for (let k = first; ; k++) {
    const t = k * tickSec
    if (t > maxT + 1e-9) break
    const x = labelWidth + t * zoom
    if (x > viewR) break
    out.push(t)
  }
  return out
}
