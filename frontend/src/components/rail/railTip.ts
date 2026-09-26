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
