// The Transitions panel — every look the renderer has, grouped the way
// CapCut groups them, applied to a cut with one click or one key.
//
// The catalog is the backend's (`list_transitions` → lib/transitionCatalog),
// never a hand-copied list: a name shown here is a name `add_transition`
// accepts, and the duration a tile applies is the renderer's own default
// for that look. Each tile carries a two-colour CSS preview of its family
// (lib/transitionCatalog.previewFor) that plays on hover/focus and is
// otherwise frozen at its midpoint — no video, no network.
//
// Target: the cut a selected v1 clip starts at, else the cut nearest the
// playhead (lib/cutPoints.targetCut) — always named in the header so the
// click is never a surprise. ‹ › (and `[` `]` in the grid) walk the cuts.
// "Every cut" hands the whole job to the Prompt bar as one sentence, so it
// lands as ONE op with a verified result instead of N undo steps. The
// sentence carries the CANONICAL backend name and the panel's own duration
// ("add a vertopen transition between every clip lasting 0.5 seconds"),
// never the display label: a label round-tripped through the grammar is a
// second parser between the click and the look, and 19 of 72 labels once
// resolved to the wrong transition that way (Barn Doors Open → crossdissolve).
//
// Tiles are always enabled for BROWSING (hover/focus previews, tooltips,
// keyboard). Only the APPLY needs a cut: without one a click shows the "a
// transition joins a cut" hint. A `disabled` tile takes no focus and no
// mouse events in WebKit, which froze the whole grid on a fresh project.
//
// Keys inside the grid (scoped with data-keymap-ignore so Space/arrows/
// Backspace stay here instead of driving the transport):
//   ← → ↑ ↓ Home End   move between tiles (roving tabindex, two columns)
//   Enter / Space       apply the focused look to the target cut
//   Backspace / Delete  remove the transition on the target cut
//   [ / ]               previous / next cut

import { useEffect, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent } from 'react'
import { useStore } from '../store'
import { usePromptStore, isBusy } from '../lib/promptStore'
import { formatCutTime, targetCut, v1CutPoints } from '../lib/cutPoints'
import {
  FAMILY_ORDER, FALLBACK_CATALOG, MAX_DURATION_S, MIN_DURATION_S, cachedTransitionCatalog, clampDuration,
  everyCutPrompt, loadTransitionCatalog, lookupTransition, type Family, type TransitionCatalog, type TransitionEntry,
} from '../lib/transitionCatalog'
import { previewClass, previewStyle } from '../lib/transitionPreview'
import './transitionsPanel.css'

const COLS = 2
const TAB_KEY = 'vai.transitionsTab'

const NO_ENTRIES: TransitionEntry[] = []

// Short tab labels; the full family name is the tab's title and the grid's label.
const TAB_LABEL: Record<Family, string> = {
  Basic: 'Basic', Wipe: 'Wipe', Slide: 'Slide', Zoom: 'Zoom', Blur: 'Blur', Shape: 'Shape',
  'Glitch/Stylised': 'Glitch', Light: 'Light',
}

function readTab(): Family {
  try {
    const v = localStorage.getItem(TAB_KEY)
    if (v && (FAMILY_ORDER as string[]).includes(v)) return v as Family
  } catch { /* private mode */ }
  return 'Basic'
}

interface Props {
  /** False while the left tab is hidden: the catalog fetch waits for first show. */
  active: boolean
}

export function TransitionsPanel({ active }: Props) {
  const sid = useStore((s) => s.sessionId)
  const edl = useStore((s) => s.edl)
  const selection = useStore((s) => s.selection)
  const playhead = useStore((s) => s.playhead)
  const dispatch = useStore((s) => s.dispatch)
  const setPlayhead = useStore((s) => s.setPlayhead)
  const promptStatus = usePromptStore((s) => s.status)
  const chatBusy = usePromptStore((s) => s.chatBusy)
  const runPrompt = usePromptStore((s) => s.run)

  const [catalog, setCatalog] = useState<TransitionCatalog>(() => cachedTransitionCatalog() ?? FALLBACK_CATALOG)
  const [listError, setListError] = useState<string | null>(null)
  const [family, setFamily] = useState<Family>(readTab)
  const [focusName, setFocusName] = useState<string | null>(null)
  // The duration field: '' means "the look's own default" and shows it as a placeholder.
  const [durationText, setDurationText] = useState('')
  // Walking cuts with ‹ › moves the playhead onto the cut, so the target rule
  // (selection first) would snap back to the selected clip's cut. A manual
  // pick therefore pins the index — for the EDL it was made against; a new
  // EDL (any op) drops the pin without an effect having to clear it.
  const [pinned, setPinned] = useState<{ index: number; edl: typeof edl } | null>(null)
  const tileRefs = useRef<Map<string, HTMLButtonElement>>(new Map())
  const tabRefs = useRef<Map<Family, HTMLButtonElement>>(new Map())
  const fetchedRef = useRef(false)

  // One catalog fetch per session, deferred to the first time the panel is
  // shown (the Media tab is the first paint; nothing here should be).
  useEffect(() => {
    if (!active || !sid || fetchedRef.current) return
    fetchedRef.current = true
    let alive = true
    loadTransitionCatalog(sid)
      .then((cat) => { if (alive) { setCatalog(cat); setListError(null) } })
      .catch((e) => {
        if (!alive) return
        fetchedRef.current = false   // the next show retries
        setListError(e instanceof Error ? e.message : String(e))
      })
    return () => { alive = false }
  }, [active, sid, listError])

  const cuts = useMemo(() => v1CutPoints(edl), [edl])
  const auto = targetCut(cuts, selection, playhead)
  const pinnedIdx = pinned && pinned.edl === edl ? pinned.index : null
  const target = pinnedIdx !== null && cuts[pinnedIdx]
    ? { cut: cuts[pinnedIdx], index: pinnedIdx, reason: 'picked' as const }
    : auto
  const applied = target?.cut.tr ? lookupTransition(catalog, target.cut.tr.type) : null

  const entries = catalog.families.get(family) ?? NO_ENTRIES
  // The roving tab stop: the remembered tile when it is in this family, else
  // the first — derived, so switching families never needs a state sync.
  const focused = (focusName ? entries.find((e) => e.name === focusName) : undefined) ?? entries[0]

  const promptBusy = isBusy(promptStatus) || chatBusy
  const durationFor = (e: TransitionEntry): number => {
    const typed = durationText.trim() === '' ? NaN : Number(durationText)
    return clampDuration(Number.isFinite(typed) ? typed : e.duration)
  }

  const pickTab = (f: Family, focus = false) => {
    setFamily(f)
    try { localStorage.setItem(TAB_KEY, f) } catch { /* not worth failing over */ }
    if (focus) tabRefs.current.get(f)?.focus()
  }

  const onTabKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const i = FAMILY_ORDER.indexOf(family)
    const next =
      e.key === 'ArrowRight' ? (i + 1) % FAMILY_ORDER.length
      : e.key === 'ArrowLeft' ? (i - 1 + FAMILY_ORDER.length) % FAMILY_ORDER.length
      : e.key === 'Home' ? 0
      : e.key === 'End' ? FAMILY_ORDER.length - 1
      : -1
    if (next < 0) return
    e.preventDefault()
    pickTab(FAMILY_ORDER[next], true)
  }

  const goCut = (delta: number) => {
    if (!target || !cuts.length) return
    const next = Math.max(0, Math.min(cuts.length - 1, target.index + delta))
    setPinned({ index: next, edl })
    // Put the playhead on the seam so the preview shows the frames the
    // transition will join.
    setPlayhead(cuts[next].at)
  }

  const [hint, setHint] = useState<string | null>(null)
  const noTargetHint = edl && (edl.tracks.find((t) => t.id === 'v1')?.clips.length ?? 0) > 1
    ? 'No adjacent cuts on the main track — a transition needs two clips touching.'
    : 'A transition joins a cut — add two clips to the main track (or split one at the playhead) first.'
  const apply = (e: TransitionEntry) => {
    if (!target) { setHint(noTargetHint); return }
    setHint(null)
    void dispatch('add_transition', { at: target.cut.at, type: e.name, duration: durationFor(e) })
  }
  const remove = () => {
    if (!target?.cut.tr) return
    void dispatch('remove_transition', { at: target.cut.at })
  }
  const applyEverywhere = (e: TransitionEntry) => {
    if (promptBusy || cuts.length < 2) return
    void runPrompt(everyCutPrompt(e, durationFor(e)))
  }

  const onGridKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (!entries.length) return
    const i = Math.max(0, entries.findIndex((x) => x.name === (focused?.name ?? '')))
    let next: number
    switch (e.key) {
      case 'ArrowRight': next = Math.min(entries.length - 1, i + 1); break
      case 'ArrowLeft': next = Math.max(0, i - 1); break
      case 'ArrowDown': next = Math.min(entries.length - 1, i + COLS); break
      case 'ArrowUp': next = Math.max(0, i - COLS); break
      case 'Home': next = 0; break
      case 'End': next = entries.length - 1; break
      case 'Backspace': case 'Delete': e.preventDefault(); remove(); return
      case '[': e.preventDefault(); goCut(-1); return
      case ']': e.preventDefault(); goCut(1); return
      default: return
    }
    e.preventDefault()
    const name = entries[next].name
    setFocusName(name)
    tileRefs.current.get(name)?.focus()
  }

  const placeholder = focused ? `${focused.duration}` : '0.5'

  return (
    <section className="transitions-panel" aria-label="Transitions" data-keymap-ignore>
      <div className="trp-target" aria-live="polite">
        {!cuts.length ? (
          <span className="trp-target-none">
            {edl && (edl.tracks.find((t) => t.id === 'v1')?.clips.length ?? 0) > 1
              ? 'No adjacent cuts on the main track — a transition needs two clips touching.'
              : 'Add two clips to the main track — a transition joins a cut.'}
          </span>
        ) : (
          <>
            <button type="button" className="trp-nav" onClick={() => goCut(-1)} disabled={!target || target.index === 0}
                    aria-label="Previous cut" title="Previous cut ([ in the grid)">‹</button>
            <div className="trp-target-body">
              <span className="trp-target-at">
                Cut at <b>{target ? formatCutTime(target.cut.at) : '—'}</b>
                <small> · {target ? target.index + 1 : 0} of {cuts.length}</small>
              </span>
              <span className="trp-target-why">
                {target?.reason === 'selection' ? 'the selected clip starts here'
                  : target?.reason === 'picked' ? 'picked with ‹ ›' : 'nearest the playhead'}
              </span>
            </div>
            <button type="button" className="trp-nav" onClick={() => goCut(1)} disabled={!target || target.index >= cuts.length - 1}
                    aria-label="Next cut" title="Next cut (] in the grid)">›</button>
          </>
        )}
      </div>
      {hint && (
        <div className="trp-hint-line" role="status">{hint}</div>
      )}
      {target?.cut.tr && (
        <div className="trp-applied">
          <span className="trp-applied-dot" aria-hidden="true" />
          <span className="trp-applied-name">
            {applied?.display ?? target.cut.tr.type} · {target.cut.tr.duration.toFixed(2)} s
          </span>
          <button type="button" className="trp-remove" onClick={remove} title="Remove the transition on this cut (Backspace in the grid)">
            Remove
          </button>
        </div>
      )}

      {listError && (
        <div className="trp-error">
          <span>Showing the built-in list — the catalog could not be read: {listError}</span>
          <button type="button" onClick={() => setListError(null)}>Retry</button>
        </div>
      )}

      <div className="trp-tabs" role="tablist" aria-label="Transition families" onKeyDown={onTabKey}>
        {FAMILY_ORDER.map((f) => {
          const n = catalog.families.get(f)?.length ?? 0
          return (
            <button
              key={f}
              ref={(el) => { if (el) tabRefs.current.set(f, el); else tabRefs.current.delete(f) }}
              type="button"
              role="tab"
              id={`trp-tab-${TAB_LABEL[f]}`}
              aria-selected={family === f}
              aria-controls="trp-grid"
              tabIndex={family === f ? 0 : -1}
              className="trp-tab"
              title={`${f} — ${n} look${n === 1 ? '' : 's'}`}
              onClick={() => pickTab(f)}
            >
              {TAB_LABEL[f]}<span className="trp-tab-n">{n}</span>
            </button>
          )
        })}
      </div>

      <div
        id="trp-grid"
        className="trp-grid"
        role="group"
        aria-labelledby={`trp-tab-${TAB_LABEL[family]}`}
        onKeyDown={onGridKey}
      >
        {entries.length === 0 && <div className="trp-empty">Nothing in this family on this backend.</div>}
        {entries.map((e) => {
          const onCut = !!target?.cut.tr && applied?.name === e.name
          const label = `${e.display} — ${e.duration} s${e.description ? `. ${e.description}` : ''}${onCut ? '. On this cut' : ''}`
          return (
            <button
              key={e.name}
              ref={(el) => { if (el) tileRefs.current.set(e.name, el); else tileRefs.current.delete(e.name) }}
              type="button"
              className={`trp-tile${onCut ? ' is-on-cut' : ''}${target ? '' : ' is-browse-only'}`}
              tabIndex={focused?.name === e.name ? 0 : -1}
              aria-label={label}
              aria-pressed={onCut}
              aria-disabled={target ? undefined : true}
              title={e.description || e.display}
              onFocus={() => setFocusName(e.name)}
              onClick={() => apply(e)}
            >
              <span className={previewClass(e.preview)} style={previewStyle(e.preview) as CSSProperties} aria-hidden="true">
                <span className="tp-a" /><span className="tp-b" /><span className="tp-veil" />
              </span>
              <span className="trp-tile-name">{e.display}</span>
              <span className="trp-tile-dur">{e.duration} s</span>
            </button>
          )
        })}
      </div>

      <div className="trp-foot">
        <label className="trp-dur">
          <span>Duration</span>
          <input
            type="number"
            inputMode="decimal"
            min={MIN_DURATION_S}
            max={MAX_DURATION_S}
            step={0.05}
            value={durationText}
            placeholder={placeholder}
            aria-label="Transition duration in seconds; empty uses the look's own default"
            onChange={(ev) => setDurationText(ev.target.value)}
          />
          <span className="trp-unit">s</span>
          {durationText !== '' && (
            <button type="button" className="trp-link" onClick={() => setDurationText('')} title="Use each look's own default again">default</button>
          )}
        </label>
        <button
          type="button"
          className="trp-every"
          disabled={!focused || cuts.length < 2 || promptBusy || !sid}
          title={cuts.length < 2 ? 'Needs at least two cuts'
            : promptBusy ? 'The Prompt bar is busy — wait or cancel'
            : `Runs "${everyCutPrompt(focused ?? { name: '…' }, focused ? durationFor(focused) : 0)}" through the Prompt bar: one undo step, verified`}
          onClick={() => focused && applyEverywhere(focused)}
        >
          Every cut <small>via Prompt</small>
        </button>
      </div>
      <div className="trp-hint">
        {catalog.looks} looks · {catalog.aliasCount} more names accepted
        {catalog.source === 'fallback' ? ' · built-in list' : ''}
      </div>
    </section>
  )
}
