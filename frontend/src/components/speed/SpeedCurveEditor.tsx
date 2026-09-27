import React from 'react'
import { Icon } from '../Icon'
import {
  addPoint, canRemove, curvePath, formatSpeed, movePoint, normalizeForCommit, nudgePoint, pointLabel,
  removePoint, roundSpeed, speedToY, widestGapMid, yToSpeed, type Curve, type NudgeKey,
} from '../../lib/speed/curveMath'

// The Inspector's editable speed curve (CapCut's Curve › edit): x across the
// clip as it plays, speed on a log axis with 1x in the middle. All maths is
// lib/speed/curveMath (unit-tested); this file is pointer/keyboard plumbing.
//
// ONE commit per gesture, like the Inspector's sliders: a drag commits on
// release, a burst of arrow keys once it goes idle (or on blur), add/remove/
// reset at once — so a gesture is one `set_speed` and one undo step.

const KEY_COMMIT_MS = 450
const GRID_SPEEDS = [10, 2, 1, 0.5, 0.1]
/** The plot's inset from the graph frame (px, top and bottom). */
const PLOT_TOP = 10
const NUDGE_KEYS = new Set<string>(['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'])

export interface SpeedCurveEditorProps {
  /** The committed curve (from the EDL). */
  points: Curve
  /** Where the playhead is across the clip (0..1), or null when outside it. */
  playheadFrac: number | null
  /** Commit an edited curve (rounded, ends pinned). */
  onCommit: (points: [number, number][]) => void
  /** Every live change (drag, key), for the duration readout. */
  onDraft?: (points: Curve) => void
  /** What Reset restores (the preset the curve was picked from). */
  resetTo: Curve
}

export function SpeedCurveEditor({ points, playheadFrac, onCommit, onDraft, resetTo }: SpeedCurveEditorProps) {
  const [draft, setDraftState] = React.useState<Curve>(points)
  const [sel, setSel] = React.useState<number | null>(null)
  const draftRef = React.useRef<Curve>(points)
  const editing = React.useRef(false)
  const keyTimer = React.useRef<number | null>(null)
  const graphRef = React.useRef<HTMLDivElement>(null)
  const pointRefs = React.useRef<(HTMLButtonElement | null)[]>([])
  const focusAfter = React.useRef<number | null>(null)
  const hintId = React.useId()

  const setDraft = React.useCallback((c: Curve) => {
    draftRef.current = c
    setDraftState(c)
    onDraft?.(c)
  }, [onDraft])

  // Follow the EDL (undo, another window, the agent) — never mid-gesture.
  const committedKey = JSON.stringify(points)
  React.useEffect(() => {
    if (editing.current) return
    draftRef.current = points
    setDraftState(points)
    setSel((s) => (s !== null && s < points.length ? s : null))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [committedKey])

  React.useEffect(() => {
    if (focusAfter.current === null) return
    pointRefs.current[focusAfter.current]?.focus()
    focusAfter.current = null
  })

  React.useEffect(() => () => { if (keyTimer.current !== null) window.clearTimeout(keyTimer.current) }, [])

  const commit = React.useCallback((c: Curve) => {
    editing.current = false
    if (keyTimer.current !== null) { window.clearTimeout(keyTimer.current); keyTimer.current = null }
    const next = normalizeForCommit(c)
    if (JSON.stringify(next) !== JSON.stringify(normalizeForCommit(points))) onCommit(next)
  }, [onCommit, points])

  const fromPointer = (e: React.PointerEvent | React.MouseEvent): [number, number] | null => {
    const r = graphRef.current?.getBoundingClientRect()
    if (!r || r.width <= 0 || r.height <= 0) return null
    return [(e.clientX - r.left) / r.width, (e.clientY - r.top) / r.height]
  }

  const onPointDown = (i: number) => (e: React.PointerEvent<HTMLButtonElement>) => {
    if (e.button !== 0) return
    e.preventDefault()
    e.currentTarget.setPointerCapture(e.pointerId)
    e.currentTarget.focus()
    editing.current = true
    setSel(i)
  }
  const onPointMove = (i: number) => (e: React.PointerEvent<HTMLButtonElement>) => {
    if (!editing.current || !e.currentTarget.hasPointerCapture(e.pointerId)) return
    const p = fromPointer(e)
    if (!p) return
    setDraft(movePoint(draftRef.current, i, p[0], roundSpeed(yToSpeed(p[1]))))
  }
  const onPointUp = (e: React.PointerEvent<HTMLButtonElement>) => {
    if (!e.currentTarget.hasPointerCapture(e.pointerId)) return
    e.currentTarget.releasePointerCapture(e.pointerId)
    commit(draftRef.current)
  }

  const onPointKey = (i: number) => (e: React.KeyboardEvent<HTMLButtonElement>) => {
    if (NUDGE_KEYS.has(e.key)) {
      e.preventDefault()
      e.stopPropagation()          // not the timeline's ←/→ frame step
      editing.current = true
      setDraft(nudgePoint(draftRef.current, i, e.key as NudgeKey, e.shiftKey))
      if (keyTimer.current !== null) window.clearTimeout(keyTimer.current)
      keyTimer.current = window.setTimeout(() => commit(draftRef.current), KEY_COMMIT_MS)
    } else if ((e.key === 'Delete' || e.key === 'Backspace') && canRemove(draftRef.current, i)) {
      e.preventDefault()
      e.stopPropagation()          // not the timeline's ripple delete
      remove(i)
    }
  }
  const onPointBlur = () => { if (keyTimer.current !== null) commit(draftRef.current) }

  const add = (x: number) => {
    const r = addPoint(draftRef.current, x)
    if (!r) return
    setDraft(r.curve)
    setSel(r.index)
    focusAfter.current = r.index
    commit(r.curve)
  }
  const remove = (i: number) => {
    const next = removePoint(draftRef.current, i)
    setDraft(next)
    const s = Math.min(i, next.length - 1)
    setSel(s)
    focusAfter.current = s
    commit(next)
  }

  const onGraphDouble = (e: React.MouseEvent<HTMLDivElement>) => {
    if ((e.target as HTMLElement).closest('.speed-point')) return
    const p = fromPointer(e)
    if (p) add(p[0])
  }

  const addAtBest = () => {
    const x = playheadFrac !== null && playheadFrac > 0 && playheadFrac < 1 ? playheadFrac : widestGapMid(draft)
    const r = addPoint(draft, x) ?? addPoint(draft, widestGapMid(draft))
    if (r) add(r.curve[r.index][0])
  }

  const selPoint = sel !== null ? draft[sel] : null
  const resetKey = JSON.stringify(normalizeForCommit(resetTo))
  const atReset = JSON.stringify(normalizeForCommit(draft)) === resetKey

  return (
    // Arrows and Delete/Backspace on a focused point are its own
    // (data-keymap-own: a plain button no longer keeps its arrows under
    // keymap rule 4 since review RD3), so ripple delete never fires there
    // while ⌘Z, Space, J/K/L and N still do (review RD2).
    <div className="speed-editor">
      <div className="speed-graph" onDoubleClick={onGraphDouble}
           role="group" aria-label="Speed curve" aria-describedby={hintId}>
        <span className="speed-axis" style={{ top: `${PLOT_TOP}px` }} aria-hidden="true">10×</span>
        <span className="speed-axis" style={{ top: '50%' }} aria-hidden="true">1×</span>
        <span className="speed-axis" style={{ top: `calc(100% - ${PLOT_TOP}px)` }} aria-hidden="true">0.1×</span>
        {/* The plot is inset so the end points (x = 0 and 1) and the 10x /
            0.1x points sit fully inside the frame, clear of the axis labels. */}
        <div className="speed-plot" ref={graphRef}>
          <svg className="speed-graph-svg" viewBox="0 0 100 50" preserveAspectRatio="none" aria-hidden="true">
            {GRID_SPEEDS.map((r) => (
              <line key={r} x1={0} x2={100} y1={speedToY(r) * 50} y2={speedToY(r) * 50}
                    className={r === 1 ? 'speed-grid speed-grid-one' : 'speed-grid'} />
            ))}
            <path className="speed-curve-area" d={`${curvePath(draft, 100, 50)} L100 50 L0 50 Z`} />
            <path className="speed-curve-line" d={curvePath(draft, 100, 50)} />
            {playheadFrac !== null && (
              <line className="speed-playhead" x1={playheadFrac * 100} x2={playheadFrac * 100} y1={0} y2={50} />
            )}
          </svg>
          {draft.map(([x, r], i) => (
            <button key={i} type="button" className="speed-point" data-point={i} data-keymap-own="Delete Backspace ArrowLeft ArrowRight ArrowUp ArrowDown"
                    ref={(el) => { pointRefs.current[i] = el }}
                    aria-label={pointLabel(draft, i)}
                    aria-pressed={sel === i}
                    aria-describedby={hintId}
                    style={{ left: `${x * 100}%`, top: `${speedToY(r) * 100}%` }}
                    onPointerDown={onPointDown(i)} onPointerMove={onPointMove(i)}
                    onPointerUp={onPointUp} onPointerCancel={onPointUp}
                    onKeyDown={onPointKey(i)} onBlur={onPointBlur}
                    onFocus={() => setSel(i)} />
          ))}
        </div>
      </div>
      <p id={hintId} className="speed-hint">
        Drag a point · double-click to add one · arrow keys move the focused point (Shift: bigger steps) · Delete removes it
      </p>
      <div className="speed-editor-bar">
        <span className="speed-readout" aria-live="polite">
          {selPoint ? `${sel === 0 ? 'Start' : sel === draft.length - 1 ? 'End' : `${Math.round(selPoint[0] * 100)}%`} · ${formatSpeed(selPoint[1])}`
            : `${draft.length} points`}
        </span>
        <button type="button" className="speed-tool" onClick={addAtBest} title="Add a point on the curve (at the playhead when it is over this clip)">
          <Icon name="plus" /> Add point
        </button>
        <button type="button" className="speed-tool" disabled={sel === null || !canRemove(draft, sel)}
                onClick={() => { if (sel !== null) remove(sel) }}
                title={sel === null ? 'Select a point to remove it' : 'Remove the selected point (the start and end stay)'}>
          <Icon name="minus" /> Remove point
        </button>
        <button type="button" className="speed-tool" disabled={atReset}
                onClick={() => { setDraft(resetTo); setSel(null); commit(resetTo) }}
                title="Reset the curve to the preset it came from">
          <Icon name="reset" /> Reset
        </button>
      </div>
    </div>
  )
}
