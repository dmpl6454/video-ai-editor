// The preview card (0.8.0 "Preview, then apply"): what a key-free Prompt-bar
// plan WOULD change, with nothing changed yet (agent/prompt/preview.py).
//
// Presentational, like ClarifyCard: the bar owns the store. The lines come
// from the backend's EDL diff (agent/prompt/changes.py), never from the plan
// text, and the card says so in so many words — "Nothing has changed yet".
//
// Keys, inside the card:
//   Enter   Apply (the primary; on the Change button, Enter is Change)
//   Escape  Change — back to the prompt with the sentence kept for editing
//   any character, `/`, Space — goes to the PROMPT (`onTypeAhead`), never to
//           the focused Apply: typing "no wait" used to apply the card at
//           the first space (final sweep 3)
// The list scrolls when long (it is a focusable region so the keyboard can
// scroll it) and ends with "and N more changes" when the backend capped it —
// a disclosure button that shows the rest, so every change is seen before
// Apply.
//
// Editor Brain (EB1, behind `brain.enabled`): a brain run's card carries
// `preview.brain` and grows a Plan / Changes tablist (APG tabs: roving
// tabindex, ←/→/Home/End select) — Plan first for a brain run, the Changes
// tab the same diff list with a why per line. The card's focus rules do not
// change: Apply still takes focus a frame later, a typed key still goes to
// the prompt, Escape is still Change. With the flag off, or for an ordinary
// prompt, the markup is exactly the 0.8.0 card.

import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react'
import type { PreviewInfo } from '../lib/promptEvents'
import { cardMayTakeFocusNow } from '../lib/cardFocus'
import { moreLine, PREVIEW_CARD_CLASS, typeAheadKey } from '../lib/previewCard'
import { useBrainEnabled } from '../lib/brainFlag'
import { useStore } from '../store'
import { EditPlanTab } from './brain/EditPlanTab'
import { Icon } from './Icon'

type CardTab = 'plan' | 'changes'

export function PromptPreviewCard({ preview, onApply, onChange, onTypeAhead, onSeek, busy = false }: {
  preview: PreviewInfo
  onApply: () => void
  onChange: () => void
  /** A printable key (or `/`) typed while the card has focus. */
  onTypeAhead?: (key: string) => void
  /** The Plan tab's seek button (a timeline instant, seconds). */
  onSeek?: (t: number) => void
  busy?: boolean
}) {
  const titleId = useId()
  const descId = useId()
  const noteId = useId()
  const tabsId = useId()
  const applyRef = useRef<HTMLButtonElement>(null)
  const rootRef = useRef<HTMLElement>(null)
  const planTabRef = useRef<HTMLButtonElement>(null)
  const changesTabRef = useRef<HTMLButtonElement>(null)
  const [showAll, setShowAll] = useState(false)
  const brainOn = useBrainEnabled()
  const fps = useStore((s) => s.edl?.canvas?.fps)
  const brain = brainOn && preview.brain ? preview.brain : null
  const [tab, setTab] = useState<CardTab>(brain?.tabDefault ?? 'changes')
  const hidden = preview.hidden ?? []

  // The card takes focus on Apply when it appears, a frame later: the
  // keypress that FOLLOWS the Enter keydown which submitted the prompt must
  // not land on it (QA-063's lesson for ClarifyCard). Only when focus is on
  // nothing or inside the Prompt bar (lib/cardFocus): a person typing in the
  // Playhead timecode while the dry run finished pressed Apply with the Enter
  // meant for the seek (final sweep 3 r2, CRITICAL).
  useEffect(() => {
    const id = requestAnimationFrame(() => {
      if (document.activeElement?.closest?.(`.${PREVIEW_CARD_CLASS}`)) return
      if (cardMayTakeFocusNow(rootRef.current)) applyRef.current?.focus()
    })
    return () => cancelAnimationFrame(id)
  }, [preview])

  const onKeyDown = (e: KeyboardEvent<HTMLElement>) => {
    const typed = typeAheadKey(e)
    if (typed !== null && onTypeAhead) {
      // Space still presses Change or the "more" disclosure (harmless and
      // expected on a button) — never Apply, which took focus by itself.
      const btn = (e.target as HTMLElement).closest('button')
      if (typed === ' ' && btn && btn !== applyRef.current) return
      e.preventDefault()
      e.stopPropagation()
      onTypeAhead(typed)
      return
    }
    if (e.key === 'Escape') {
      e.preventDefault()
      e.stopPropagation()
      onChange()
      return
    }
    if (e.key === 'Enter' && !e.shiftKey && !e.metaKey && !e.ctrlKey && !e.altKey) {
      // A focused button answers for itself (Enter on Change is Change).
      if ((e.target as HTMLElement).closest('button')) return
      e.preventDefault()
      if (!busy) onApply()
    }
  }

  // The tablist's own keys (APG tabs, automatic activation): ←/→ move and
  // select, Home/End jump. Everything else falls through to the card.
  const onTabKey = (e: KeyboardEvent<HTMLElement>) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return
    e.preventDefault()
    e.stopPropagation()
    const next: CardTab = e.key === 'Home' ? 'plan' : e.key === 'End' ? 'changes' : tab === 'plan' ? 'changes' : 'plan'
    setTab(next)
    ;(next === 'plan' ? planTabRef : changesTabRef).current?.focus()
  }

  const whyOf = (i: number) => {
    if (!brain) return null
    const why = brain.whys[i]
    if (!why) return null
    return (
      <span className={`prompt-preview-why${brain.unexplained.includes(i) ? ' is-unexplained' : ''}`}> — {why}</span>
    )
  }
  const panelId = `${tabsId}-panel`
  const changesList = (
    <ul className="prompt-preview-list" aria-label="Changes it would make" tabIndex={0}>
      {preview.lines.map((line, i) => <li key={i}>{line}{whyOf(i)}</li>)}
      {showAll && hidden.map((line, i) => <li key={`h${i}`}>{line}{whyOf(preview.lines.length + i)}</li>)}
      {preview.more > 0 && (hidden.length > 0 ? (
        <li className="is-more">
          <button type="button" className="prompt-preview-more" aria-expanded={showAll}
                  onClick={() => setShowAll((v) => !v)}>
            {showAll ? 'Show fewer' : moreLine(preview.more)}
          </button>
        </li>
      ) : <li className="is-more">{moreLine(preview.more)}</li>)}
    </ul>
  )

  return (
    <section ref={rootRef} className={brain ? `${PREVIEW_CARD_CLASS} has-brain` : PREVIEW_CARD_CLASS} role="region" aria-labelledby={titleId}
             aria-describedby={preview.note ? `${noteId} ${descId}` : descId}
             onKeyDown={onKeyDown} data-testid="prompt-preview">
      <header className="prompt-preview-head">
        <span className="prompt-preview-kicker"><Icon name="info" /> Preview</span>
        <h3 id={titleId} className="prompt-preview-summary">{preview.summary}</h3>
      </header>
      {preview.note && <p id={noteId} className="prompt-preview-note" role="note">{preview.note}</p>}
      <p id={descId} className="prompt-preview-safe">
        <Icon name="lock" /> {preview.nothing_changed || 'Nothing has changed yet.'}
      </p>
      {brain && (
        <div role="tablist" aria-label="Preview details" className="prompt-preview-tabs" onKeyDown={onTabKey}>
          <button type="button" role="tab" id={`${tabsId}-plan`} ref={planTabRef} aria-selected={tab === 'plan'}
                  aria-controls={panelId} tabIndex={tab === 'plan' ? 0 : -1} onClick={() => setTab('plan')}>
            Plan
          </button>
          <button type="button" role="tab" id={`${tabsId}-changes`} ref={changesTabRef}
                  aria-selected={tab === 'changes'} aria-controls={panelId} tabIndex={tab === 'changes' ? 0 : -1}
                  onClick={() => setTab('changes')}>
            Changes
          </button>
        </div>
      )}
      {brain && tab === 'plan' ? (
        <EditPlanTab info={brain} fps={fps} onSeek={onSeek} id={panelId} labelledBy={`${tabsId}-plan`} />
      ) : brain ? (
        // the tabpanel is a div holding the list: a role on the <ul> itself would strip its listitems
        <div role="tabpanel" id={panelId} aria-labelledby={`${tabsId}-changes`} className="prompt-preview-panel">
          {changesList}
        </div>
      ) : changesList}
      <div className="prompt-preview-actions">
        <button type="button" ref={applyRef} className="primary" disabled={busy} onClick={onApply}
                aria-describedby={preview.note ? noteId : undefined}>
          <Icon name="check" /> Apply<kbd>↵</kbd>
        </button>
        <button type="button" className="ghost" onClick={onChange}>
          Change<kbd>Esc</kbd>
        </button>
      </div>
    </section>
  )
}
