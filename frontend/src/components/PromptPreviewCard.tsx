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

import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react'
import type { PreviewInfo } from '../lib/promptEvents'
import { cardMayTakeFocusNow } from '../lib/cardFocus'
import { moreLine, PREVIEW_CARD_CLASS, typeAheadKey } from '../lib/previewCard'
import { Icon } from './Icon'

export function PromptPreviewCard({ preview, onApply, onChange, onTypeAhead, busy = false }: {
  preview: PreviewInfo
  onApply: () => void
  onChange: () => void
  /** A printable key (or `/`) typed while the card has focus. */
  onTypeAhead?: (key: string) => void
  busy?: boolean
}) {
  const titleId = useId()
  const descId = useId()
  const noteId = useId()
  const applyRef = useRef<HTMLButtonElement>(null)
  const rootRef = useRef<HTMLElement>(null)
  const [showAll, setShowAll] = useState(false)
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

  return (
    <section ref={rootRef} className={PREVIEW_CARD_CLASS} role="region" aria-labelledby={titleId}
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
      <ul className="prompt-preview-list" aria-label="Changes it would make" tabIndex={0}>
        {preview.lines.map((line, i) => <li key={i}>{line}</li>)}
        {showAll && hidden.map((line, i) => <li key={`h${i}`}>{line}</li>)}
        {preview.more > 0 && (hidden.length > 0 ? (
          <li className="is-more">
            <button type="button" className="prompt-preview-more" aria-expanded={showAll}
                    onClick={() => setShowAll((v) => !v)}>
              {showAll ? 'Show fewer' : moreLine(preview.more)}
            </button>
          </li>
        ) : <li className="is-more">{moreLine(preview.more)}</li>)}
      </ul>
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
