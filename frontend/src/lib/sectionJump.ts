// Where the Inspector's jump list scrolls to (components/inspector/
// SectionIndex). The list is a sticky bar at the top of the Inspector's own
// scroll container and wraps to 1–3 rows, so "scroll the section to the top"
// (scrollIntoView block:'start') parked the section header UNDER it. It also
// scrolled every scrollable ancestor — the document included, which is how
// the whole app shell moved up by 1 px. This computes the container's own
// scrollTop so the section's top lands just below the stuck bar.

/** Breathing room between the bar's bottom edge and the section header. */
export const JUMP_GAP_PX = 4

export interface JumpGeometry {
  /** getBoundingClientRect().top of the scroll container. */
  containerTop: number
  /** Its top border (clientTop): the scrollport starts below it. */
  clientTop: number
  /** Its current scrollTop. */
  scrollTop: number
  /** getBoundingClientRect().top of the section. */
  sectionTop: number
  /** The sticky bar's rendered height (it sticks at the scrollport's top). */
  barHeight: number
}

/** The container scrollTop that shows the section right under the bar. */
export function sectionScrollTop(g: JumpGeometry): number {
  const offset = g.sectionTop - (g.containerTop + g.clientTop) + g.scrollTop
  return Math.max(0, Math.round(offset - g.barHeight - JUMP_GAP_PX))
}

/** The nearest ancestor that actually scrolls vertically, else null. */
export function scrollParentOf(el: HTMLElement | null): HTMLElement | null {
  for (let p = el?.parentElement ?? null; p; p = p.parentElement) {
    const oy = getComputedStyle(p).overflowY
    if ((oy === 'auto' || oy === 'scroll' || oy === 'overlay') && p.scrollHeight > p.clientHeight) return p
  }
  return null
}
