// The DOM half of lib/menuNav (QA-102): one hook every popover uses, so the
// project picker, Export options, text presets, caption menu, "⋯" and the
// timeline context menu all open, move, close and hand focus back the same way.
//
//   const a11y = useMenuA11y({ open, ready: !!pos, mode: 'menu', menuRef, triggerRef, onClose })
//   <div ref={menuRef} role="menu" data-keymap-ignore onKeyDown={a11y.onKeyDown}>…
//
// - On open (once `ready`, i.e. the portal is positioned and mounted) focus
//   moves INTO the popover: a menu's checked item, else its first item; a
//   dialog's first control.
// - Escape closes from anywhere while open — also while focus is still on the
//   trigger — and focus returns to the trigger. `close()` does the same for an
//   item that was activated, so a keyboard user is never dropped on <body>.
// - The popover root carries `data-keymap-ignore` (keymap/engine.ts), so Space,
//   Delete or arrows inside it act on the popover, not on the timeline.

import { useCallback, useEffect, useRef, type KeyboardEvent as ReactKeyboardEvent, type RefObject } from 'react'
import { DIALOG_FOCUSABLE_SELECTOR, MENU_ITEM_SELECTOR, initialFocusIndex, menuKeyIntent, type MenuMode } from './menuNav'

export interface MenuA11yOptions {
  open: boolean
  /** The popover is mounted and positioned (portals render one pass after `open`). */
  ready?: boolean
  mode: MenuMode
  menuRef: RefObject<HTMLElement | null>
  triggerRef: RefObject<HTMLElement | null>
  /** Close the popover (the component owns its `open` state). */
  onClose: () => void
}

function items(root: HTMLElement | null, mode: MenuMode): HTMLElement[] {
  if (!root) return []
  const sel = mode === 'menu' ? MENU_ITEM_SELECTOR : DIALOG_FOCUSABLE_SELECTOR
  return Array.from(root.querySelectorAll<HTMLElement>(sel)).filter(
    (el) => !(el as HTMLButtonElement).disabled && el.getAttribute('aria-disabled') !== 'true' && !el.hidden,
  )
}

export function useMenuA11y({ open, ready = true, mode, menuRef, triggerRef, onClose }: MenuA11yOptions) {
  // Latest onClose without re-running the effects every render.
  const onCloseRef = useRef(onClose)
  useEffect(() => { onCloseRef.current = onClose }, [onClose])

  const close = useCallback((restoreFocus = true) => {
    onCloseRef.current()
    if (restoreFocus) triggerRef.current?.focus()
  }, [triggerRef])

  // Focus into the popover when it opens.
  useEffect(() => {
    if (!open || !ready) return
    const list = items(menuRef.current, mode)
    const i = mode === 'menu'
      ? initialFocusIndex(list.map((el) => el.getAttribute('aria-checked') === 'true'))
      : (list.length ? 0 : -1)
    if (i >= 0) list[i].focus()
    else menuRef.current?.focus()
  }, [open, ready, mode, menuRef])

  // Escape while focus is OUTSIDE the popover (e.g. back on the trigger, or on
  // <body> after a click elsewhere). Inside it, onKeyDown handles the key and
  // stops it, so this bubble-phase listener never sees it twice. Deliberately
  // NOT gated on `defaultPrevented`: the global keymap's capture listener
  // preventDefaults Escape (its 'deselect' binding) before this runs, which
  // made the fallback dead code.
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      e.preventDefault()
      close(true)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, close])

  const onKeyDown = useCallback((e: ReactKeyboardEvent) => {
    const list = items(menuRef.current, mode)
    const current = list.indexOf(document.activeElement as HTMLElement)
    const intent = menuKeyIntent(e.key, mode, current, list.length, e.shiftKey)
    if (intent.kind === 'none') return
    e.preventDefault()
    e.stopPropagation()
    if (intent.kind === 'close') close(true)
    else list[intent.index]?.focus()
  }, [menuRef, mode, close])

  return { onKeyDown, close }
}
