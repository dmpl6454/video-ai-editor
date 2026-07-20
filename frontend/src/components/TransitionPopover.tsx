// TransitionPopover — small fixed-position popover for adding/updating a
// transition at a v1 cut point. Opened by Timeline.tsx when the user clicks
// a cut-point affordance drawn on the timeline canvas.
//
// Catalog: fetched once per app run via the read-only `list_transitions`
// dispatch tool. store.dispatch() deliberately returns Promise<void> (it's a
// fire-and-refresh wrapper), so this component calls api.dispatch directly —
// the route envelope is { result, edl_hash, op } (main.py's dispatch_tool)
// and list_transitions' result is { transitions: string[], catalog: {...},
// count }. list_transitions is a read-only tool (it never commits), so
// calling it here creates no op / no undo entry / no EDL refresh churn.

import { useEffect, useState } from 'react'
import { api } from '../api'
import './TransitionPopover.css'

export interface TransitionInfo {
  at: number
  type: string
  duration: number
}

// Curated first screen (every name verified present in the backend catalog —
// render/transitions.py's NATIVE ∪ ALIASES ∪ CUSTOM_EXPRS). "More…" swaps in
// the full ~90-name list fetched from list_transitions.
const CURATED = [
  'fade', 'dissolve', 'fadeblack', 'fadewhite',
  'slideleft', 'slideright', 'wipeleft', 'wiperight',
  'circleopen', 'zoomin', 'pixelize', 'glitch', 'whip', 'spin',
]

// Fallback when the listing call fails: names that are always valid.
const FALLBACK = ['fade', 'dissolve', 'wipeleft', 'wiperight', 'slideleft', 'slideright']

// Module-level cache: the transition catalog is static for the lifetime of
// the backend process, so one successful fetch serves every popover open.
// A failed fetch is NOT cached — the next open retries.
let catalogCache: string[] | null = null
let catalogInflight: Promise<string[]> | null = null

function fetchCatalog(sid: string): Promise<string[]> {
  if (catalogCache) return Promise.resolve(catalogCache)
  if (!catalogInflight) {
    catalogInflight = api
      .dispatch<{ transitions?: string[] }>(sid, 'list_transitions', {})
      .then((res) => {
        const names = res.result?.transitions
        if (Array.isArray(names) && names.length) {
          catalogCache = names
          return names
        }
        return FALLBACK
      })
      .catch(() => FALLBACK)
      .finally(() => { catalogInflight = null })
  }
  return catalogInflight
}

interface Props {
  x: number            // viewport coords (position: fixed) — pass e.clientX/Y
  y: number
  at: number           // timeline second of the cut this popover edits
  existing: TransitionInfo | null
  sessionId: string
  onApply: (type: string, duration: number) => void
  onClose: () => void
}

export function TransitionPopover({ x, y, at, existing, sessionId, onApply, onClose }: Props) {
  const [names, setNames] = useState<string[]>(catalogCache ?? FALLBACK)
  const [showAll, setShowAll] = useState(false)
  const [type, setType] = useState(existing?.type ?? 'fade')
  // Kept as a string so mid-edit states ("0.", "") don't snap the input.
  const [duration, setDuration] = useState(String(existing?.duration ?? 0.5))

  useEffect(() => {
    let alive = true
    fetchCatalog(sessionId).then((n) => { if (alive) setNames(n) })
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

  const curated = CURATED.filter((n) => names.includes(n))
  const base = curated.length ? curated : names.slice(0, 12)
  // Always include the currently-selected type (an existing transition set
  // via chat/MCP can be any of the ~90 names) so the <select> never renders
  // a blank value.
  const options = showAll ? names : (base.includes(type) ? base : [type, ...base])

  // Keep the popover on-screen when the cut is near the viewport edge.
  const left = Math.max(8, Math.min(x, window.innerWidth - 248))
  const top = Math.max(8, Math.min(y, window.innerHeight - 200))

  function apply() {
    const d = Math.max(0.1, Math.min(2.0, parseFloat(duration) || 0.5))
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
        <span className="tp-at">at {at.toFixed(2)}s</span>
      </div>
      <label className="tp-row">
        <span>Type</span>
        <select value={type} onChange={(e) => setType(e.target.value)}>
          {options.map((n) => <option key={n} value={n}>{n}</option>)}
        </select>
      </label>
      {!showAll && names.length > options.length && (
        <button type="button" className="tp-more" onClick={() => setShowAll(true)}>
          More… ({names.length} available)
        </button>
      )}
      <label className="tp-row">
        <span>Duration</span>
        <input
          type="number"
          min={0.1}
          max={2.0}
          step={0.1}
          value={duration}
          onChange={(e) => setDuration(e.target.value)}
        />
        <span className="tp-unit">s</span>
      </label>
      <div className="tp-actions">
        <button type="button" onClick={onClose}>Cancel</button>
        <button type="button" className="tp-apply" onClick={apply}>
          {existing ? 'Update' : 'Add'}
        </button>
      </div>
    </div>
  )
}
