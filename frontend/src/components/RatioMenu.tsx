// "16:9 ▾" in the top bar: aspect ratios + platform presets in one menu, the
// current one checked (QA-012; lib/ratioMenu explains what it replaced).
//
// Rendered through a portal to document.body and positioned from the trigger,
// like the session picker and the Export popover: .topbar clips overflow, so a
// menu positioned as a child would be cut off below the 44 px bar.

import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { createPortal } from 'react-dom'
import { useStore } from '../store'
import { ASPECTS, PLATFORM_PRESETS, activeAspect, presetActive, ratioLabel, type Aspect, type PlatformPreset } from '../lib/ratioMenu'
import { platformMenuCommand } from '../lib/exportOptions'
import { canvasFacts } from '../lib/frameStep'
import { Icon } from './Icon'

export function RatioMenu() {
  const canvas = useStore((s) => s.edl?.canvas ?? null)
  const duration = useStore((s) => s.edl?.duration ?? 0)
  const dispatch = useStore((s) => s.dispatch)
  const [open, setOpen] = useState(false)
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null)
  const btnRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const rect = btnRef.current?.getBoundingClientRect()
    if (rect) setPos({ left: rect.left, top: rect.bottom + 4 })
    const close = (e: MouseEvent) => {
      if (!(e.target as HTMLElement).closest('[data-ratio-menu]')) setOpen(false)
    }
    const t = setTimeout(() => window.addEventListener('mousedown', close), 0)
    return () => { clearTimeout(t); window.removeEventListener('mousedown', close) }
  }, [open])

  // Keyboard: focus lands on the checked item (else the first) when the menu opens.
  useEffect(() => {
    if (!open || !pos) return
    const items = menuRef.current?.querySelectorAll<HTMLButtonElement>('[role="menuitemradio"]')
    const checked = Array.from(items ?? []).find((b) => b.getAttribute('aria-checked') === 'true')
    ;(checked ?? items?.[0])?.focus()
  }, [open, pos])

  const closeToTrigger = () => { setOpen(false); btnRef.current?.focus() }

  const onMenuKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const items = Array.from(menuRef.current?.querySelectorAll<HTMLButtonElement>('[role="menuitemradio"]') ?? [])
    const i = items.indexOf(document.activeElement as HTMLButtonElement)
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeToTrigger(); return }
    if (e.key === 'Tab') { setOpen(false); return }
    const next = e.key === 'ArrowDown' ? (i + 1) % items.length
      : e.key === 'ArrowUp' ? (i - 1 + items.length) % items.length
      : e.key === 'Home' ? 0 : e.key === 'End' ? items.length - 1 : -1
    if (next < 0 || !items.length) return
    e.preventDefault()
    items[next].focus()
  }

  const pickAspect = (r: Aspect) => { closeToTrigger(); void dispatch('set_aspect_ratio', { ratio: r }) }
  // QA-027: a platform preset applies its whole spec (canvas + bitrate +
  // loudness) via apply_export_preset — set_canvas left Shorts at -16 LUFS.
  const applyPreset = (p: PlatformPreset) => {
    closeToTrigger()
    const cmd = platformMenuCommand(p)
    void dispatch(cmd.tool, cmd.args)
  }

  const aspect = activeAspect(canvas)
  const facts = canvas ? canvasFacts(canvas, duration) : ''

  return (
    <div data-ratio-menu style={{ display: 'inline-flex' }}>
      <button
        ref={btnRef}
        className={`ratio-trigger${open ? ' is-open' : ''}`}
        aria-haspopup="menu"
        aria-expanded={open}
        disabled={!canvas}
        title={canvas ? `Canvas ${facts} — aspect ratio and platform presets` : 'Aspect ratio and platform presets'}
        onClick={() => setOpen((o) => !o)}
      >
        <span className="ratio-glyph" aria-hidden="true" data-aspect={aspect ?? 'custom'} />
        {ratioLabel(canvas)}<Icon name="chevronDown" />
      </button>
      {open && pos && createPortal(
        <div
          ref={menuRef}
          data-ratio-menu
          role="menu"
          aria-label="Aspect ratio and platform presets"
          className="ratio-menu"
          style={{ left: pos.left, top: pos.top }}
          onKeyDown={onMenuKey}
        >
          <div className="ratio-menu-head section-label">Aspect ratio</div>
          {ASPECTS.map((r) => (
            <button key={r} role="menuitemradio" aria-checked={aspect === r} className="ratio-item"
                    title={`Set canvas aspect ratio to ${r} — overlays reposition to fit`}
                    onClick={() => pickAspect(r)}>
              <span className="ratio-check menu-check" aria-hidden="true">{aspect === r && <Icon name="check" />}</span>
              <span className="ratio-label">{r}</span>
            </button>
          ))}
          <div className="ratio-menu-sep" role="separator" />
          <div className="ratio-menu-head section-label">Platform presets</div>
          {PLATFORM_PRESETS.map((p) => {
            const on = presetActive(p, canvas)
            return (
              <button key={p.label} role="menuitemradio" aria-checked={on} className="ratio-item"
                      title={p.title} onClick={() => applyPreset(p)}>
                <span className="ratio-check menu-check" aria-hidden="true">{on && <Icon name="check" />}</span>
                <span className="ratio-label">{p.label}</span>
                <span className="ratio-hint">{p.w}×{p.h} · {p.bitrateKbps / 1000} Mbps</span>
              </button>
            )
          })}
          {facts && <div className="ratio-menu-foot">Canvas now {facts}</div>}
        </div>,
        document.body,
      )}
    </div>
  )
}
