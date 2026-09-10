// TransitionPopover — small fixed-position popover for adding/updating a
// transition at a v1 cut point. Opened by Timeline.tsx when the user clicks
// a cut-point affordance drawn on the timeline canvas.
//
// Catalog: the same normalised catalog the Transitions panel uses
// (lib/transitionCatalog — fetched once per app run through the read-only
// `list_transitions` dispatch tool, never an op / undo entry / EDL refresh).
// The <select> is grouped by family with CapCut-style names, and picking a
// look sets the duration to that look's own default unless the user has
// typed one — the same duration the panel applies, so the two surfaces never
// disagree about what "Whip Pan" lasts.

import { useEffect, useState } from 'react'
import {
  FALLBACK_CATALOG, FAMILY_ORDER, MAX_DURATION_S, MIN_DURATION_S, cachedTransitionCatalog, clampDuration,
  loadTransitionCatalog, lookupTransition, type TransitionCatalog,
} from '../lib/transitionCatalog'
import { formatCutTime } from '../lib/cutPoints'
import './TransitionPopover.css'

export interface TransitionInfo {
  at: number
  type: string
  duration: number
}

interface Props {
  x: number            // viewport coords (position: fixed) — pass e.clientX/Y
  y: number
  at: number           // timeline second of the cut this popover edits
  existing: TransitionInfo | null
  sessionId: string
  onApply: (type: string, duration: number) => void
  /** Only meaningful when `existing` is set — there is nothing to remove
   *  from a cut that has no transition yet. */
  onRemove: () => void
  onClose: () => void
}

export function TransitionPopover(
  { x, y, at, existing, sessionId, onApply, onRemove, onClose }: Props,
) {
  const [catalog, setCatalog] = useState<TransitionCatalog>(() => cachedTransitionCatalog() ?? FALLBACK_CATALOG)
  // An existing transition may carry an alias ("spin") set via chat/MCP;
  // the select shows its canonical look so the value is never blank.
  const [type, setType] = useState(() => lookupTransition(cachedTransitionCatalog() ?? FALLBACK_CATALOG, existing?.type ?? 'fade')?.name ?? existing?.type ?? 'fade')
  // Kept as a string so mid-edit states ("0.", "") don't snap the input.
  // `touched` remembers that the user typed a duration, so a later type
  // change keeps it instead of resetting to the look's default.
  const [duration, setDuration] = useState(() => String(existing?.duration ?? lookupTransition(FALLBACK_CATALOG, existing?.type ?? 'fade')?.duration ?? 0.5))
  const [touched, setTouched] = useState(!!existing)

  useEffect(() => {
    let alive = true
    loadTransitionCatalog(sessionId)
      .then((c) => {
        if (!alive) return
        setCatalog(c)
        setType((t) => lookupTransition(c, t)?.name ?? t)
      })
      .catch(() => { /* the fallback list is already showing; the next open retries */ })
    return () => { alive = false }
  }, [sessionId])

  // Close on Escape or a mousedown outside the popover. Mirrors Timeline's
  // context-menu pattern: the root div stops mousedown propagation (React's
  // synthetic stopPropagation halts the underlying native event before it
  // bubbles up to this window-level listener), so only genuinely-outside
  // clicks close it. The listener attaches on mount — after the opening
  // click's event has fully finished — so the open click never self-closes.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    const onDown = () => onClose()
    window.addEventListener('keydown', onKey)
    window.addEventListener('mousedown', onDown)
    return () => {
      window.removeEventListener('keydown', onKey)
      window.removeEventListener('mousedown', onDown)
    }
  }, [onClose])

  const pickType = (name: string) => {
    setType(name)
    if (!touched) {
      const d = lookupTransition(catalog, name)?.duration
      if (typeof d === 'number') setDuration(String(d))
    }
  }

  // Keep the popover on-screen when the cut is near the viewport edge.
  const left = Math.max(8, Math.min(x, window.innerWidth - 248))
  const top = Math.max(8, Math.min(y, window.innerHeight - 200))

  const known = lookupTransition(catalog, type)
  const entry = known ?? null

  function apply() {
    const fallback = entry?.duration ?? 0.5
    const d = clampDuration(parseFloat(duration) || fallback)
    onApply(type, d)
  }

  return (
    <div
      className="transition-popover"
      style={{ left, top }}
      onMouseDown={(e) => e.stopPropagation()}
    >
      <div className="tp-title">
        {existing ? 'Edit transition' : 'Add transition'}
        <span className="tp-at">at {formatCutTime(at)}</span>
      </div>
      <label className="tp-row">
        <span>Type</span>
        <select value={type} onChange={(e) => pickType(e.target.value)}>
          {/* A value the catalog does not know (a stale project, a renamed
              look) stays selectable so the select never renders blank. */}
          {!known && <option value={type}>{type}</option>}
          {FAMILY_ORDER.map((f) => {
            const rows = catalog.families.get(f) ?? []
            if (!rows.length) return null
            return (
              <optgroup key={f} label={f}>
                {rows.map((e) => <option key={e.name} value={e.name}>{e.display}</option>)}
              </optgroup>
            )
          })}
        </select>
      </label>
      {entry?.description && <div className="tp-desc">{entry.description}</div>}
      <label className="tp-row">
        <span>Duration</span>
        <input
          type="number"
          min={MIN_DURATION_S}
          max={MAX_DURATION_S}
          step={0.05}
          value={duration}
          onChange={(e) => { setTouched(true); setDuration(e.target.value) }}
        />
        <span className="tp-unit">s</span>
      </label>
      <div className="tp-actions">
        {/* Only offered for a cut that HAS a transition. Removing was
            otherwise reachable only through chat/MCP (`remove_transition`),
            so a transition added by a mis-click had no undo but Undo itself. */}
        {existing && (
          <button type="button" className="tp-remove" onClick={onRemove}>
            Remove
          </button>
        )}
        <button type="button" onClick={onClose}>Cancel</button>
        <button type="button" className="tp-apply" onClick={apply}>
          {existing ? 'Update' : 'Add'}
        </button>
      </div>
    </div>
  )
}
