// "⋯" in the top bar below 1440 px: the secondary things the bar has no room
// for inline at laptop widths — the safe-zone overlay picker and the build
// identity. (Every core action — Ratio, Text, Captions, Help, Shortcuts, Save,
// Open, Export — stays inline at every supported width; QA-012.)
//
// The version badge must stay one click away at any width: "which build?"
// has to be answerable from a screenshot or a tester's report (CLAUDE.md,
// "Release identity"), so it is repeated in the trigger's tooltip too.

import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { SafeZoneToggle } from './SafeZones'
import { useMenuA11y } from '../lib/useMenuA11y'
import { Icon } from './Icon'

export function TopBarMore({ version }: { version: string | null }) {
  const [open, setOpen] = useState(false)
  const [pos, setPos] = useState<{ right: number; top: number } | null>(null)
  const btnRef = useRef<HTMLButtonElement>(null)
  const popRef = useRef<HTMLDivElement>(null)
  // Focus enters the popover, Tab stays inside, Escape closes and returns to
  // "⋯" (lib/useMenuA11y, QA-102).
  const a11y = useMenuA11y({
    open, ready: !!pos, mode: 'dialog', menuRef: popRef, triggerRef: btnRef, onClose: () => setOpen(false),
  })

  useEffect(() => {
    if (!open) return
    const rect = btnRef.current?.getBoundingClientRect()
    if (rect) setPos({ right: window.innerWidth - rect.right, top: rect.bottom + 4 })
    const close = (e: MouseEvent) => {
      if (!(e.target as HTMLElement).closest('[data-topbar-more]')) setOpen(false)
    }
    const t = setTimeout(() => window.addEventListener('mousedown', close), 0)
    return () => { clearTimeout(t); window.removeEventListener('mousedown', close) }
  }, [open])

  return (
    <div data-topbar-more className="topbar-narrow" style={{ display: 'inline-flex' }}>
      <button
        ref={btnRef}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label="More: safe zones and app version"
        title={version ? `More — safe zones · ${version}` : 'More — safe zones'}
        onClick={() => setOpen((o) => !o)}
        className="icon-btn"
      ><Icon name="more" /></button>
      {open && pos && createPortal(
        <div ref={popRef} data-topbar-more data-keymap-ignore role="dialog" aria-label="More options"
             className="topbar-more-menu" style={{ right: pos.right, top: pos.top }} onKeyDown={a11y.onKeyDown}>
          <label className="topbar-more-row">
            <span>Safe zones</span>
            <SafeZoneToggle />
          </label>
          {version && <div className="topbar-more-version">{version}</div>}
        </div>,
        document.body,
      )}
    </div>
  )
}
