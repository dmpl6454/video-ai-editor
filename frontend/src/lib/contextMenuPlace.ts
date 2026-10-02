// Where a context menu opens (design §3): 280 px wide at the cursor, flipped
// above it when it would overflow the window, scrolling when taller than the
// frame. Pure, so the flip is unit-tested.
import type { CSSProperties } from 'react'

export const CTX_WIDTH = 280
const MARGIN = 8

export function ctxPlacement(x: number, y: number, menuH: number, viewport: { w: number; h: number }, width = CTX_WIDTH): { left: number; top: number; maxHeight: number } {
  const left = Math.max(MARGIN, Math.min(x, viewport.w - width - MARGIN))
  const room = viewport.h - MARGIN - y
  const flip = menuH > room && y - MARGIN >= menuH
  const top = flip ? Math.max(MARGIN, y - menuH) : Math.min(y, Math.max(MARGIN, viewport.h - MARGIN - Math.min(menuH, viewport.h - 2 * MARGIN)))
  return { left, top, maxHeight: viewport.h - 2 * MARGIN }
}

/** A fixed-position style for `(x, y)`, given the menu's estimated height
 *  (rows × 24 + padding) — called at render time, so it reads the window. */
export function ctxStyle(x: number, y: number, estimatedH = 760): CSSProperties {
  const vp = { w: typeof window === 'undefined' ? 1440 : window.innerWidth, h: typeof window === 'undefined' ? 900 : window.innerHeight }
  const p = ctxPlacement(x, y, Math.min(estimatedH, vp.h - 16), vp)
  return { position: 'fixed', left: p.left, top: p.top, width: CTX_WIDTH, maxHeight: p.maxHeight, zIndex: 100 }
}
