// Keyboard contract for every popover in the app (QA-102) — the pure half.
// lib/useMenuA11y.ts applies it to the DOM.
//
// The top bar grew five popovers (project picker, Export options, text presets,
// caption language, "⋯") and the timeline a context menu, each a portaled <div>
// that closed on an outside mousedown and nothing else: Escape did not close the
// picker or Export, focus never entered them, and the picker's rows and the
// context menu's items were click-only <div>s. Two shapes cover them all:
//
//   'menu'   — a list of actions/choices (role="menu"): arrows, Home/End move
//              between items (roving focus), Escape closes and returns focus to
//              the trigger, Tab closes too (a menu is not a tab stop).
//   'dialog' — a small form (role="dialog": Export options, text presets): Tab
//              and Shift+Tab cycle INSIDE it (the portal sits at the end of
//              <body>, so an untrapped Tab fell off the page), arrows are left
//              to the <select>s, Escape closes and returns focus.

export type MenuMode = 'menu' | 'dialog'

export type MenuIntent =
  | { kind: 'focus'; index: number }
  | { kind: 'close' }
  | { kind: 'none' }

/** What a key does inside a popover with `count` focusable items, focus at `current` (-1 = none). */
export function menuKeyIntent(
  key: string, mode: MenuMode, current: number, count: number, shiftKey = false,
): MenuIntent {
  if (key === 'Escape') return { kind: 'close' }
  if (count <= 0) return { kind: 'none' }
  if (mode === 'menu') {
    switch (key) {
      case 'ArrowDown': return { kind: 'focus', index: current < 0 ? 0 : (current + 1) % count }
      case 'ArrowUp': return { kind: 'focus', index: current < 0 ? count - 1 : (current - 1 + count) % count }
      case 'Home': return { kind: 'focus', index: 0 }
      case 'End': return { kind: 'focus', index: count - 1 }
      case 'Tab': return { kind: 'close' }
      default: return { kind: 'none' }
    }
  }
  if (key !== 'Tab') return { kind: 'none' }
  if (shiftKey && current <= 0) return { kind: 'focus', index: count - 1 }
  if (!shiftKey && (current < 0 || current >= count - 1)) return { kind: 'focus', index: 0 }
  return { kind: 'none' }   // an interior Tab: let the browser move focus
}

/** Which item gets focus when a popover opens: a menu's checked item, else the first. */
export function initialFocusIndex(checked: readonly boolean[]): number {
  if (!checked.length) return -1
  const i = checked.indexOf(true)
  return i >= 0 ? i : 0
}

/** Selector for the items a mode navigates between. */
export const MENU_ITEM_SELECTOR = '[role="menuitem"],[role="menuitemradio"],[role="menuitemcheckbox"]'
export const DIALOG_FOCUSABLE_SELECTOR =
  'button, select, input:not([type="hidden"]), textarea, a[href], [tabindex]:not([tabindex="-1"])'
