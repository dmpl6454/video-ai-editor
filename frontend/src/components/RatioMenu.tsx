// "9:16 ▾" in the centre of the top bar: the canvas in one menu
// (docs/design/LEFT_RAIL_SPEC.md §2.10, critique M6; QA-012 before it).
//
//   role="menu" "Canvas ratio", three role="group" sections, each named; the
//   visual headings inside them are aria-hidden (the group's name says it):
//     Aspect ratio       9:16 · 16:9 · 1:1 · 4:5
//     Platform presets   Reels · Shorts · TikTok · IG 1:1 · IG 4:5 · YouTube
//     Safe-zone overlay  Off · TikTok · Reels · Shorts — named "TikTok safe
//                        zone" and so on, so the two TikToks differ by name
//   then the canvas facts as a footer.
//
// Every pick closes the menu and returns focus to the trigger. Keys: ↑/↓/Home/
// End move across all three groups, Enter/Space pick, Esc closes to the
// trigger, Tab closes. The safe-zone overlay (SafeZones.tsx) is driven by the
// same store field its old <select> wrote (lib/safeZonesStore).
//
// The items keep a native `title` for their detail (a preset's bitrate and
// loudness, why a safe zone needs 9:16): the shell's portalled tooltip opens
// BELOW a top-bar control, where it would cover the next item of a menu.
//
// Rendered through a portal to document.body and positioned from the trigger,
// like the session picker and the Export popover: .topbar clips overflow, so a
// menu positioned as a child would be cut off below the 44 px bar.

import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { createPortal } from 'react-dom'
import { useStore } from '../store'
import {
  ASPECTS, PLATFORM_PRESETS, activeAspect, presetActive, ratioFacts, ratioTriggerName, ratioValue,
  type Aspect, type PlatformPreset,
} from '../lib/ratioMenu'
import { platformMenuCommand } from '../lib/exportOptions'
import { canvasFacts } from '../lib/frameStep'
import { SAFE_ZONES, SAFE_ZONE_MODES, NOT_916_HINT, isVertical916, type SafeZoneMode } from '../lib/safeZones'
import { useSafeZones } from '../lib/safeZonesStore'
import { Icon } from './Icon'

const SAFE_TITLE = 'Overlay where TikTok / Reels / Shorts draw their own UI over a 9:16 video, '
  + 'so captions and lower-thirds land clear of it (approximate guides)'

export function RatioMenu() {
  const canvas = useStore((s) => s.edl?.canvas ?? null)
  const duration = useStore((s) => s.edl?.duration ?? 0)
  const dispatch = useStore((s) => s.dispatch)
  const safeMode = useSafeZones((s) => s.mode)
  const setSafeMode = useSafeZones((s) => s.setMode)
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

  // Keyboard: focus lands on the first checked item (else the first) when the menu opens.
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
  // View chrome, not project state: no dispatch, no undo step (safeZonesStore).
  const pickSafe = (m: SafeZoneMode) => { closeToTrigger(); setSafeMode(m) }

  const aspect = activeAspect(canvas)
  const facts = canvas ? canvasFacts(canvas, duration) : ''
  // The overlay draws on a 9:16 canvas only; elsewhere each platform says so.
  const nonVertical = !!canvas && !isVertical916(canvas)

  return (
    <div data-ratio-menu style={{ display: 'inline-flex' }}>
      <button
        ref={btnRef}
        className={`ratio-trigger${open ? ' is-open' : ''}`}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={ratioTriggerName(canvas)}
        disabled={!canvas}
        data-tip={canvas ? `Canvas ${facts} — aspect ratio, platform presets and safe zones` : 'Aspect ratio, platform presets and safe zones'}
        onClick={() => setOpen((o) => !o)}
      >
        <span className="ratio-glyph" aria-hidden="true" data-aspect={aspect ?? 'custom'} />
        {ratioValue(canvas)}
        {/* Shown at density 0 only — by CSS (.tb-d1 hides it), never by a
            prop: useTopBarFit measures each step with classes alone, so a
            step must not depend on a React render to take effect. */}
        {canvas && <span className="ratio-facts">· {ratioFacts(canvas)}</span>}
        <Icon name="chevronDown" />
      </button>
      {open && pos && createPortal(
        <div
          ref={menuRef}
          data-ratio-menu
          data-keymap-ignore
          role="menu"
          aria-label="Canvas ratio"
          className="ratio-menu"
          style={{ left: pos.left, top: pos.top }}
          onKeyDown={onMenuKey}
        >
          <div role="group" aria-label="Aspect ratio">
            <div className="ratio-menu-head section-label" aria-hidden="true">Aspect ratio</div>
            {ASPECTS.map((r) => (
              <button key={r} role="menuitemradio" aria-checked={aspect === r} className="ratio-item"
                      title={`Set canvas aspect ratio to ${r} — overlays reposition to fit`}
                      onClick={() => pickAspect(r)}>
                <span className="ratio-check menu-check" aria-hidden="true">{aspect === r && <Icon name="check" />}</span>
                <span className="ratio-label">{r}</span>
              </button>
            ))}
          </div>
          <div className="ratio-menu-sep" role="separator" />
          <div role="group" aria-label="Platform presets">
            <div className="ratio-menu-head section-label" aria-hidden="true">Platform presets</div>
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
          </div>
          <div className="ratio-menu-sep" role="separator" />
          <div role="group" aria-label="Safe-zone overlay">
            <div className="ratio-menu-head section-label" aria-hidden="true">Safe-zone overlay</div>
            {/* The name is exactly "TikTok safe zone" etc. (§2.10); on a canvas
                that is not 9:16 the "9:16 only" hint is shown, and said as the
                item's DESCRIPTION, never folded into its name. */}
            {nonVertical && <span id="ratio-safe-916" className="ratio-sr-only" hidden>{NOT_916_HINT}</span>}
            {SAFE_ZONE_MODES.map((m) => (
              <button key={m} role="menuitemradio" aria-checked={safeMode === m} className="ratio-item"
                      title={nonVertical && m !== 'off' ? `${NOT_916_HINT} (approximate guides)` : SAFE_TITLE}
                      aria-describedby={nonVertical && m !== 'off' ? 'ratio-safe-916' : undefined}
                      onClick={() => pickSafe(m)}>
                <span className="ratio-check menu-check" aria-hidden="true">{safeMode === m && <Icon name="check" />}</span>
                {m === 'off'
                  ? <span className="ratio-label"><span className="ratio-sr-only">Safe zones </span>Off</span>
                  : <span className="ratio-label">{SAFE_ZONES[m].label}<span className="ratio-sr-only"> safe zone</span></span>}
                {nonVertical && m !== 'off' && <span className="ratio-hint" aria-hidden="true">9:16 only</span>}
              </button>
            ))}
          </div>
          {facts && <div className="ratio-menu-foot" aria-hidden="true">Canvas now {facts}</div>}
        </div>,
        document.body,
      )}
    </div>
  )
}
