// The rail tooltip's timing and placement (RailTooltip.tsx, LEFT_RAIL_SPEC §2.6).

export const TIP_DELAY_MS = 400
export const TIP_GRACE_MS = 120
const GAP = 8
const EDGE = 8

/** Where the tip goes for a control's rect: to the right of a rail control,
 *  below anything else; clamped so it stays `EDGE` px inside the viewport. */
export function tipPosition(
  r: { left: number; top: number; right: number; bottom: number; width: number; height: number },
  tip: { width: number; height: number },
  viewport: { width: number; height: number },
  side: 'right' | 'below',
): { x: number; y: number } {
  let x = side === 'right' ? r.right + GAP : r.left + r.width / 2 - tip.width / 2
  let y = side === 'right' ? r.top + r.height / 2 - tip.height / 2 : r.bottom + 6
  x = Math.max(EDGE, Math.min(viewport.width - tip.width - EDGE, x))
  y = Math.max(EDGE, Math.min(viewport.height - tip.height - EDGE, y))
  return { x: Math.round(x), y: Math.round(y) }
}

/** Is the pointer over the tip's box? The tip is `pointer-events: none` (a
 *  click must reach the panel control under it — final QA: it covered the top
 *  row of every tool panel and ate the first click), so WCAG 1.4.13
 *  "hoverable" is kept by geometry instead of pointerenter/leave. */
export function pointInRect(
  x: number, y: number, r: { left: number; top: number; right: number; bottom: number },
): boolean {
  return x >= r.left && x < r.right && y >= r.top && y < r.bottom
}
