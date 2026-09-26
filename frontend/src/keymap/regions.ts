// F6 / ⇧F6 (LEFT_RAIL_SPEC §4.1): move keyboard focus region by region, in
// the order a keyboard user meets them on screen — top bar → rail (the
// selected tab, its one tab stop) → tool panel → Prompt bar → timeline
// canvas → right panel → rail foot. A region that is absent (the rail foot
// before R3), collapsed (a hidden tool panel) or has nothing focusable is
// skipped. The commands are scope 'anywhere', so F6 also leaves a text field.

export interface Region {
  id: string
  /** The region's root element. */
  root: string
  /** Where focus lands, when it is not the first focusable control inside. */
  focus?: string
}

/** The timeline canvas: where the editing keys (Space, J/K/L, S, N) act. */
export const TIMELINE_CANVAS = 'main.center canvas[aria-label="Timeline"]'

export const REGIONS: readonly Region[] = [
  { id: 'topbar', root: 'header.topbar' },
  { id: 'rail', root: 'nav.rail', focus: 'nav.rail [role="tab"][aria-selected="true"]' },
  { id: 'panel', root: '#tool-panel' },
  // The centre is two stops (review RD2): the Prompt bar, then the timeline.
  // As one stop F6 always landed in the prompt, and Space/J/K/L/S then typed.
  { id: 'prompt', root: 'main.center .prompt-bar' },
  { id: 'timeline', root: 'main.center', focus: TIMELINE_CANVAS },
  { id: 'right', root: '#right-panel' },
  { id: 'foot', root: 'nav.rail-foot' },
]

const FOCUSABLE = 'button, input, select, textarea, a[href], [tabindex]'

/** The next region to visit from `current` (-1: focus is in none of them)
 *  going `dir`, wrapping and skipping the unavailable ones; -1 when none is
 *  available. From outside every region, F6 starts at the first and ⇧F6 at
 *  the last. */
export function nextRegion(current: number, available: readonly boolean[], dir: 1 | -1): number {
  const n = available.length
  if (n === 0) return -1
  let i = current < 0 ? (dir === 1 ? -1 : n) : current
  for (let step = 0; step < n; step++) {
    i = (i + dir + n) % n
    if (available[i] && i !== current) return i
  }
  return -1
}

/** A control a keyboard user can land on: in the tab order, enabled,
 *  rendered, and not inside a hidden or inert subtree. */
export function isUsable(el: Element): boolean {
  const h = el as HTMLElement & { disabled?: boolean }
  return h.tabIndex >= 0 && !h.disabled && el.getClientRects().length > 0 && !el.closest('[hidden], [inert]')
}

/** Where focus would land in `region`, or null when it cannot take focus. */
export function regionTarget(doc: Document, region: Region): HTMLElement | null {
  const root = doc.querySelector(region.root)
  if (!root || root.closest('[hidden], [inert]')) return null
  if (region.focus) {
    const el = doc.querySelector<HTMLElement>(region.focus)
    return el && isUsable(el) ? el : null
  }
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).find(isUsable) ?? null
}

/** Focus the timeline canvas (Esc from an empty Prompt bar or Chat box). */
export function focusTimeline(doc: Document = document): boolean {
  const el = doc.querySelector<HTMLElement>(TIMELINE_CANVAS)
  if (!el || !isUsable(el)) return false
  el.focus()
  return true
}

/** Move focus to the next (dir 1) or previous (dir -1) region. Returns the
 *  id of the region focused, or null when there was nowhere to go. */
export function cycleRegion(dir: 1 | -1, doc: Document = document): string | null {
  const active = doc.activeElement
  const current = REGIONS.findIndex((r) => {
    const root = doc.querySelector(r.root)
    return !!root && !!active && root.contains(active)
  })
  const targets = REGIONS.map((r) => regionTarget(doc, r))
  const i = nextRegion(current, targets.map(Boolean), dir)
  if (i < 0) return null
  targets[i]?.focus()
  return REGIONS[i].id
}
