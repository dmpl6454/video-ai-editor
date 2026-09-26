// Focus rescue (docs/design/LEFT_RAIL_SPEC.md §5.3, critique H2).
//
// When a region hides (the tool panel collapses, the rail switches panels, the
// right panel collapses or changes tab) and keyboard focus was inside it, focus
// would be stranded on an element nobody can see. The rescue moves it to the
// control that owns the region; when focus was anywhere else it is left alone,
// so showing a panel from the timeline keeps you on the timeline.
//
// The check is by ELEMENT STATE, not `activeElement === body`: WebKit may leave
// activeElement on a display:none element while Chromium blurs it to <body>
// during its focus fix-up, so both shapes count as "lost".
import { useLayoutEffect, useRef, type DependencyList, type FocusEvent } from 'react'

interface FocusLike {
  isConnected?: boolean
  closest?(selector: string): unknown
  getClientRects?(): { length: number }
}

/** Whether `active` (document.activeElement) is no longer a place focus can
 *  usefully be: gone, parked on <body>, inside a [hidden] subtree, or not
 *  rendered at all (a display:none ancestor without the attribute). */
export function isFocusLost(active: FocusLike | null | undefined, body: unknown): boolean {
  if (!active || active === body) return true
  if (active.isConnected === false) return true
  if (active.closest?.('[hidden]')) return true
  return (active.getClientRects?.().length ?? 1) === 0
}

/**
 * Track focus-within on a region and, after every commit that changes one of
 * `deps`, rescue focus to `target()` if it was inside and is now lost.
 * Returns the focus/blur handlers to spread on the region's root element.
 */
export function useFocusRescue(
  target: () => HTMLElement | null,
  deps: DependencyList,
): { onFocus: () => void; onBlur: (e: FocusEvent) => void } {
  const inside = useRef(false)
  useLayoutEffect(() => {
    if (!inside.current) return
    if (!isFocusLost(document.activeElement, document.body)) return
    // focus() fires focusin/focusout synchronously, so the handlers below
    // re-derive `inside` for wherever it lands (the right panel's expand
    // button is inside its own region; a rail tab is not).
    target()?.focus()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
  return {
    onFocus: () => { inside.current = true },
    onBlur: (e) => {
      const next = e.relatedTarget as Node | null
      if (!next || !(e.currentTarget as Node).contains(next)) inside.current = false
    },
  }
}
