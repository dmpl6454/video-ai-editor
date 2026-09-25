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

export function TopBarMore({ version }: { version: string | null }) {
  const [open, setOpen] = useState(false)
  const [pos, setPos] = useState<{ right: number; top: number } | null>(null)
  const btnRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (!open) return
    const rect = btnRef.current?.getBoundingClientRect()
    if (rect) setPos({ right: window.innerWidth - rect.right, top: rect.bottom + 4 })
    const close = (e: MouseEvent) => {
      if (!(e.target as HTMLElement).closest('[data-topbar-more]')) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') { setOpen(false); btnRef.current?.focus() } }
    const t = setTimeout(() => window.addEventListener('mousedown', close), 0)
    window.addEventListener('keydown', onKey)
    return () => { clearTimeout(t); window.removeEventListener('mousedown', close); window.removeEventListener('keydown', onKey) }
  }, [open])

  return (
    <div data-topbar-more className="topbar-narrow" style={{ display: 'inline-flex' }}>
      <button
        ref={btnRef}
        aria-haspopup="true"
        aria-expanded={open}
        aria-label="More: safe zones and app version"
        title={version ? `More — safe zones · ${version}` : 'More — safe zones'}
        onClick={() => setOpen((o) => !o)}
        style={{ fontSize: 13, padding: '2px 8px' }}
      >⋯</button>
      {open && pos && createPortal(
        <div data-topbar-more className="topbar-more-menu" style={{ right: pos.right, top: pos.top }}>
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
