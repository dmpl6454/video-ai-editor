import { useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'
import { TIP_DELAY_MS, TIP_GRACE_MS, pointInRect, tipPosition } from './railTip'
import './rail.css'

// ONE tooltip for the shell's icon controls (docs/design/LEFT_RAIL_SPEC.md
// §2.6, critique H4), portalled to <body> so no rail/panel overflow clips it.
// Any control opts in with `data-tip` (text) and optionally `data-kbd` (its
// live chord). The tip is `aria-hidden` and there is no `title` or `::after`
// on the control, so a tab's accessible name stays exactly its label.
//
//   shows   after 400 ms of hover, or of keyboard (:focus-visible) focus
//   hides   on Esc, pointer-down, any scroll, blur, or pointer-leave — leaving
//           has a 120 ms grace so the pointer can move ONTO the tip
//           (WCAG 1.4.13 hoverable), and staying on it keeps it. The tip is
//           `pointer-events: none` so a click lands on the panel control it
//           covers (final QA: it ate the first click on the panel's top row);
//           "on the tip" is therefore a geometry test on pointermove
//   places  right of rail controls (.rail), below everything else; clamped to
//           the viewport
//
// Driven imperatively (no React state per hover) — the listeners run on every
// pointerover in the document.

export function RailTooltip() {
  const tipRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const tip = tipRef.current
    if (!tip) return
    let showTimer = 0
    let hideTimer = 0
    let current: HTMLElement | null = null
    let onTip = false

    const show = (el: HTMLElement) => {
      if (!el.isConnected || el.getClientRects().length === 0) return
      current = el
      tip.replaceChildren(document.createTextNode(el.dataset.tip ?? ''))
      if (el.dataset.kbd) {
        const k = document.createElement('kbd')
        k.textContent = el.dataset.kbd
        tip.append(k)
      }
      tip.hidden = false
      const side = el.closest('.rail, .rail-foot') ? 'right' : 'below'
      const at = tipPosition(el.getBoundingClientRect(), { width: tip.offsetWidth, height: tip.offsetHeight },
        { width: window.innerWidth, height: window.innerHeight }, side)
      tip.style.left = `${at.x}px`
      tip.style.top = `${at.y}px`
    }
    const hide = () => {
      window.clearTimeout(showTimer)
      window.clearTimeout(hideTimer)
      tip.hidden = true
      current = null
      onTip = false
    }
    const arm = (el: HTMLElement) => {
      window.clearTimeout(hideTimer)
      if (current === el) return
      window.clearTimeout(showTimer)
      showTimer = window.setTimeout(() => show(el), TIP_DELAY_MS)
    }
    const keep = () => window.clearTimeout(hideTimer)
    const softHide = () => {
      window.clearTimeout(showTimer)
      window.clearTimeout(hideTimer)
      hideTimer = window.setTimeout(hide, TIP_GRACE_MS)
    }
    const tipOwner = (t: EventTarget | null) =>
      t instanceof Element ? t.closest<HTMLElement>('[data-tip]') : null

    // Over the shown tip, what lies UNDER it is not being hovered: its
    // pointerover/out must neither re-arm nor start the hide.
    const overTip = (e: PointerEvent) =>
      !tip.hidden && pointInRect(e.clientX, e.clientY, tip.getBoundingClientRect())
    const onOver = (e: PointerEvent) => {
      const el = tipOwner(e.target)
      if (el && !overTip(e)) arm(el)
    }
    const onOut = (e: PointerEvent) => {
      const el = tipOwner(e.target)
      const to = e.relatedTarget as Node | null
      if (el && !(to && el.contains(to)) && !overTip(e)) softHide()
    }
    // Hoverable without taking pointer events: entering the tip's box within
    // the grace keeps it; leaving the box (not back onto its control) starts
    // the grace again.
    const onMove = (e: PointerEvent) => {
      if (tip.hidden) return
      const inside = overTip(e)
      if (inside === onTip) return
      onTip = inside
      if (inside) keep()
      else if (!(current && e.target instanceof Node && current.contains(e.target))) softHide()
    }
    const onFocusIn = (e: FocusEvent) => {
      const el = tipOwner(e.target)
      let keyboard = false
      try { keyboard = !!el?.matches(':focus-visible') } catch { /* engine without :focus-visible */ }
      if (el && keyboard) arm(el)
      else hide()
    }
    const onFocusOut = (e: FocusEvent) => {
      if (current && e.target === current) hide()
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !tip.hidden) hide()
    }

    document.addEventListener('pointerover', onOver)
    document.addEventListener('pointerout', onOut)
    document.addEventListener('pointermove', onMove, { passive: true })
    document.addEventListener('focusin', onFocusIn)
    document.addEventListener('focusout', onFocusOut)
    document.addEventListener('pointerdown', hide, true)
    document.addEventListener('scroll', hide, true)
    window.addEventListener('blur', hide)
    // Bubble phase: the keymap engine lets Escape propagate (engine.ts).
    document.addEventListener('keydown', onKey)
    return () => {
      hide()
      document.removeEventListener('pointerover', onOver)
      document.removeEventListener('pointerout', onOut)
      document.removeEventListener('pointermove', onMove)
      document.removeEventListener('focusin', onFocusIn)
      document.removeEventListener('focusout', onFocusOut)
      document.removeEventListener('pointerdown', hide, true)
      document.removeEventListener('scroll', hide, true)
      window.removeEventListener('blur', hide)
      document.removeEventListener('keydown', onKey)
    }
  }, [])

  if (typeof document === 'undefined') return null
  return createPortal(<div ref={tipRef} className="rail-tip" aria-hidden="true" hidden />, document.body)
}
