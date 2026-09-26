// THE modal dialog of the app, on the shared popover contract (lib/useMenuA11y,
// QA-102): focus moves into it on open, Tab stays inside, Escape and the
// backdrop close it, and focus returns to the control that opened it. While
// it is open the editor behind it is `inert`, so neither Tab nor a screen
// reader can reach controls under the backdrop.
//
// Every modal is this one component (wave-B review): the Export options, the
// confirm (role="alertdialog", ConfirmDialog) and the export progress
// (ExportModal) used to be three implementations — ConfirmDialog trapped Tab
// by hand, and the progress modal took no focus at all, so Tab walked to the
// Media panel behind its backdrop.
//
// Portaled to <body> (the top bar clips overflow), dark and token-styled —
// the Export options used to be a light, native-select popover (QA-100).

import { useEffect, useLayoutEffect, useRef, type ReactNode, type RefObject } from 'react'
import { createPortal } from 'react-dom'
import { useMenuA11y } from '../lib/useMenuA11y'
import { Icon } from './Icon'

interface Props {
  open: boolean
  title: string
  onClose: () => void
  /** Where focus returns on close; defaults to whatever had focus on open. */
  triggerRef?: RefObject<HTMLElement | null>
  children: ReactNode
  footer?: ReactNode
  className?: string
  /** An id for the heading; the dialog is labelled by it. */
  labelId: string
  /** An id of the element that describes the dialog (a confirm's body). */
  describedById?: string
  /** "alertdialog" for a confirm that interrupts (QA-099). */
  role?: 'dialog' | 'alertdialog'
  /** The corner × (default on). A confirm or a progress view has its own. */
  showClose?: boolean
  /** Close on a backdrop click (default on). */
  closeOnBackdrop?: boolean
}

// Several dialogs can stack (a confirm over the export dialog): the editor
// root stays inert until the last one closes.
let inertHolders = 0
function holdInert(): () => void {
  const root = typeof document !== 'undefined' ? document.getElementById('root') : null
  inertHolders++
  if (root) root.inert = true
  let released = false
  return () => {
    if (released) return
    released = true
    inertHolders = Math.max(0, inertHolders - 1)
    if (root && inertHolders === 0) root.inert = false
  }
}

export function Dialog({
  open, title, onClose, triggerRef, children, footer, className, labelId, describedById,
  role = 'dialog', showClose = true, closeOnBackdrop = true,
}: Props) {
  const panelRef = useRef<HTMLDivElement>(null)
  // Focus to restore when no trigger is given: captured BEFORE the hook's
  // effect moves focus into the dialog (layout effects run first).
  const openerRef = useRef<HTMLElement | null>(null)
  const releaseRef = useRef<(() => void) | null>(null)
  useLayoutEffect(() => {
    if (!open) return
    openerRef.current = document.activeElement as HTMLElement | null
    releaseRef.current = holdInert()
    return () => {
      // Closed by a path that did not go through the hook (a footer button
      // that unmounts the dialog): release the editor and give focus back.
      releaseRef.current?.()
      releaseRef.current = null
      const back = triggerRef?.current ?? openerRef.current
      const active = document.activeElement
      if (back && (!active || active === document.body || !document.contains(active))) back.focus?.()
    }
  }, [open, triggerRef])
  const restoreRef = triggerRef ?? openerRef
  const a11y = useMenuA11y({
    open, ready: open, mode: 'dialog', menuRef: panelRef, triggerRef: restoreRef,
    // The editor must be un-inert BEFORE the hook hands focus back to it.
    onClose: () => { releaseRef.current?.(); releaseRef.current = null; onClose() },
  })
  // Keep a stale inert flag from outliving an unmounted dialog tree.
  useEffect(() => () => { releaseRef.current?.() }, [])
  if (!open) return null
  return createPortal(
    <div
      className="dialog-backdrop"
      onMouseDown={(e) => { if (closeOnBackdrop && e.target === e.currentTarget) a11y.close(true) }}
    >
      <div
        ref={panelRef}
        className={`dialog${className ? ` ${className}` : ''}`}
        role={role}
        aria-modal="true"
        aria-labelledby={labelId}
        aria-describedby={describedById}
        data-keymap-ignore
        onKeyDown={a11y.onKeyDown}
      >
        <header className="dialog-head"><h2 id={labelId}>{title}</h2></header>
        <div className="dialog-body">{children}</div>
        {footer && <footer className="dialog-foot">{footer}</footer>}
        {/* Last in the tab order (focus opens on the first field), drawn
            top-right by CSS. */}
        {showClose && (
          <button type="button" className="dialog-x" aria-label="Close" onClick={() => a11y.close(true)}>
            <Icon name="close" />
          </button>
        )}
      </div>
    </div>,
    document.body,
  )
}
