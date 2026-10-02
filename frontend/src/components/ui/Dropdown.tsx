// A ✓-checked single-choice menu under (or above) its trigger (design:
// `rgba(44,44,46,.96)` + blur 20, r10, p5, 26 px items, hover `#0A84FF`).
// Built on the app's popover contract (lib/useMenuA11y): focus lands on the
// checked item, ↑/↓ move, Enter picks, Esc returns focus to the trigger, an
// outside mousedown closes. Portaled to <body> so a panel's overflow clip
// cannot cut it (the toolbar lesson of CaptionsButton / TopBar).
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { useMenuA11y } from '../../lib/useMenuA11y'
import { Icon } from '../Icon'

export interface DropdownItem<T extends string> {
  id: T
  label: string
  title?: string
  disabled?: boolean
}

export function Dropdown<T extends string>({ items, value, onChange, label, trigger, className, width = 150, align = 'end',
                                             placement = 'below', note, footer }: {
  items: readonly DropdownItem<T>[]
  value: T | null
  onChange: (id: T) => void
  /** The menu's accessible name. */
  label: string
  /** The trigger's content (icon, current value, caret). */
  trigger: ReactNode
  className?: string
  width?: number
  align?: 'start' | 'end'
  placement?: 'below' | 'above'
  /** A quiet line under the items ("Playback only. Export uses full quality."). */
  note?: string
  /** Extra rows after a divider (the Layout menu's "Reset current layout"). */
  footer?: ReactNode
}) {
  const [open, setOpen] = useState(false)
  const btnRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const [pos, setPos] = useState<{ left: number; top: number; bottom: number } | null>(null)
  const a11y = useMenuA11y({ open, ready: !!pos, mode: 'menu', menuRef, triggerRef: btnRef, onClose: () => setOpen(false) })

  const toggle = () => {
    if (open) { setOpen(false); setPos(null); return }
    const r = btnRef.current?.getBoundingClientRect()
    if (!r) return
    const left = align === 'end' ? Math.max(8, r.right - width) : Math.max(8, Math.min(r.left, window.innerWidth - width - 8))
    setPos({ left, top: r.bottom + 4, bottom: window.innerHeight - r.top + 4 })
    setOpen(true)
  }

  useEffect(() => {
    if (!open) return
    const close = (e: MouseEvent) => {
      const t = e.target as HTMLElement
      if (menuRef.current?.contains(t) || btnRef.current?.contains(t)) return
      setOpen(false)
    }
    const id = window.setTimeout(() => window.addEventListener('mousedown', close), 0)
    return () => { window.clearTimeout(id); window.removeEventListener('mousedown', close) }
  }, [open])

  return (
    <>
      <button ref={btnRef} type="button" className={`ui-dd-trigger${open ? ' is-open' : ''}${className ? ` ${className}` : ''}`}
              aria-haspopup="menu" aria-expanded={open} aria-label={label} onClick={toggle}>
        {trigger}
      </button>
      {open && pos && createPortal(
        <div ref={menuRef} role="menu" aria-label={label} className="ui-menu" data-keymap-ignore
             onKeyDown={a11y.onKeyDown}
             style={{ left: pos.left, width, ...(placement === 'below' ? { top: pos.top } : { bottom: pos.bottom }) }}>
          {items.map((it) => (
            <button key={it.id} type="button" role="menuitemradio" aria-checked={value === it.id} className="ui-menu-item"
                    title={it.title} disabled={it.disabled}
                    onClick={() => { onChange(it.id); a11y.close() }}>
              <span className="ui-menu-check" aria-hidden="true">{value === it.id && <Icon name="check" />}</span>
              {it.label}
            </button>
          ))}
          {note && <div className="ui-menu-note">{note}</div>}
          {footer && <><div className="ui-menu-sep" role="separator" /><div onClick={() => a11y.close()}>{footer}</div></>}
        </div>,
        document.body,
      )}
    </>
  )
}
