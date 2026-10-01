// The Prompt bar — one sentence in, a verified edit out (spec §5).
//
// It sits above the preview in the centre column (App.tsx), never over the
// picture. The input is the only text field in the editor that the keymap
// engine cannot see into (it is a TEXTAREA, so every key is the user's), and
// `/` or ⌘K from anywhere focuses it (keymap/commands.ts `focusPrompt`).
//
// Keys, in the input:
//   Enter        run     ·  Shift+Enter  newline
//   ↑ / ↓        recall the last 20 prompts when the caret is at the start / end
//   Esc          idle → clear the field · running → ASK before cancelling
//                · a preview card is open → Change (drop it, keep the sentence)
//                (a run is a one-op edit that finishes on the Mac whether or not
//                this tab is watching; aborting it silently would be a lie about
//                what the timeline is doing — spec §5.2, §4.2)
//
// 0.8.0 "Preview, then apply": with no Anthropic key the plan is dry-run and
// shown as a PromptPreviewCard (what would change, "Nothing has changed yet",
// Apply ↵ / Change Esc). Only Apply edits the timeline; typing a different
// sentence and pressing Enter drops the card and plans the new one.
//
// The bar is also the reconnect point: on mount and when a `prompt` op lands
// from elsewhere (the phone, another tab) it reads `GET …/prompt/run` and
// re-attaches to a run that is still going.

import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { useStore } from '../store'
import { usePromptStore, isBusy } from '../lib/promptStore'
import { runProgress, terminalAnnouncement } from '../lib/promptEvents'
import { canSubmitPrompt, shouldClearPromptText, shouldRefocusPrompt, supersedesClarify } from '../lib/promptFocus'
import { focusTimeline } from '../keymap/regions'
import { promptLengthNote } from '../lib/promptLimit'
import { BrainBadge } from './BrainBadge'
import { ClarifyCard } from './ClarifyCard'
import { PromptPreviewCard } from './PromptPreviewCard'
import { PREVIEW_CARD_CLASS, previewReplyOf, typedOverCard } from '../lib/previewCard'
import { cardMayTakeFocusNow } from '../lib/cardFocus'
import { PromptRunLog } from './PromptRunLog'
import { AnalysisProgressLine } from './brain/AnalysisProgress'
import { useBrainEnabled } from '../lib/brainFlag'
import { api } from '../api'
import './promptBar.css'
import { Icon } from './Icon'

// Five real prompts from the benchmark set (spec §6.2), rotated while idle.
const EXAMPLES = [
  'make this a 30s reel with hinglish captions',
  'remove the ums and dead air',
  'add chill background music and duck it under my voice',
  'make 3 shorts under 30 seconds for tiktok',
  'give it a cinematic look and add a hook in the first 3 seconds',
]
const EXAMPLE_ROTATE_MS = 6000
const LINE_PX = 20
const MAX_LINES = 3

export function PromptBar() {
  const sid = useStore((s) => s.sessionId)
  const ops = useStore((s) => s.ops)
  const status = usePromptStore((s) => s.status)
  const steps = usePromptStore((s) => s.steps)
  const plan = usePromptStore((s) => s.plan)
  const clarify = usePromptStore((s) => s.clarify)
  const history = usePromptStore((s) => s.history)
  const chatBusy = usePromptStore((s) => s.chatBusy)
  const focusNonce = usePromptStore((s) => s.focusNonce)
  const logOpen = usePromptStore((s) => s.logOpen)
  const runId = usePromptStore((s) => s.runId)
  const reply = usePromptStore((s) => s.reply)
  const lastError = usePromptStore((s) => s.lastError)
  const cancelling = usePromptStore((s) => s.cancelling)
  const lastPrompt = usePromptStore((s) => s.prompt)
  const nothingToApply = usePromptStore((s) => !!s.nothingToApply)
  const analysis = usePromptStore((s) => s.analysis ?? null)
  const brainOn = useBrainEnabled()
  const run = usePromptStore((s) => s.run)
  const answer = usePromptStore((s) => s.answer)
  const applyPreview = usePromptStore((s) => s.applyPreview)
  const cancel = usePromptStore((s) => s.cancel)
  const dropClarify = usePromptStore((s) => s.dropClarify)
  const reconnect = usePromptStore((s) => s.reconnect)

  const [text, setText] = useState('')
  const [histIdx, setHistIdx] = useState(-1)     // -1 = the live draft
  const [draft, setDraft] = useState('')
  const [example, setExample] = useState(0)
  const [askCancel, setAskCancel] = useState(false)
  const [announce, setAnnounce] = useState('')
  // Cancel was confirmed: whatever the run still sends (a card its plan reaches after a read that was
  // stopped) is dropped, never shown (UX-07: 'no preview pops up after a cancel').
  const [cancelIntent, setCancelIntent] = useState(false)
  const taRef = useRef<HTMLTextAreaElement>(null)
  const formRef = useRef<HTMLFormElement>(null)
  const keepRef = useRef<HTMLButtonElement>(null)
  const prevStatus = useRef(status)

  const busy = isBusy(status)
  const disabled = !sid || chatBusy

  // Reconnect on mount / session switch.
  useEffect(() => { if (sid) void reconnect(sid) }, [sid, reconnect])

  // A `prompt` op from elsewhere (the phone, a second tab) while this bar is
  // not showing that run → pick it up. Cheap: one GET, only on a prompt op.
  useEffect(() => {
    const last = ops[ops.length - 1]
    if (!sid || !last || last.tool !== 'prompt') return
    const planId = (last.args as { plan_id?: unknown } | undefined)?.plan_id
    if (typeof planId === 'string' && planId === runId) return
    if (isBusy(usePromptStore.getState().status)) return
    void reconnect(sid)
  }, [ops, sid, runId, reconnect])

  // `/` or ⌘K → focus (keymap), and focus returns to the input after a run
  // ends — but only if focus was not somewhere the user put it on purpose, and
  // NEVER when the run paused on a clarify card: the card has just focused its
  // own first control (child effects run first) and taking it back is what
  // made Enter re-plan in a loop (lib/promptFocus, QA-019).
  useEffect(() => { if (focusNonce > 0) { taRef.current?.focus(); taRef.current?.select() } }, [focusNonce])
  useEffect(() => {
    const was = prevStatus.current
    prevStatus.current = status
    if (isBusy(was) && !isBusy(status)) setAskCancel(false)
    const active = document.activeElement
    const spot = !active || active === document.body ? 'none'
      : taRef.current?.closest('.prompt-bar')?.contains(active) ? 'inside-bar' : 'elsewhere'
    if (shouldRefocusPrompt(was, status, spot)) taRef.current?.focus()
    // Only a FINISHED run empties the field; a failed or cancelled one keeps
    // the sentence so it can be fixed and run again (QA-124).
    if (shouldClearPromptText(was, status)) setText('')
    const st = usePromptStore.getState()
    const line = terminalAnnouncement(st)
    // The result of pressing Apply is announced as that: "Applied — …".
    const said = line && st.appliedFromPreview && status === 'done' ? line.replace(/^Done\b/, 'Applied') : line
    if (said && !isBusy(status) && was !== status) setAnnounce(said)
  }, [status])


  // Rotating placeholder while idle and empty; a still string otherwise.
  useEffect(() => {
    if (busy || text) return
    const reduce = typeof matchMedia !== 'undefined' && matchMedia('(prefers-reduced-motion: reduce)').matches
    if (reduce) return
    const id = window.setInterval(() => setExample((i) => (i + 1) % EXAMPLES.length), EXAMPLE_ROTATE_MS)
    return () => window.clearInterval(id)
  }, [busy, text])

  // Auto-grow to three lines.
  useEffect(() => {
    const el = taRef.current
    if (!el) return
    el.style.height = 'auto'
    const h = Math.min(el.scrollHeight, LINE_PX * MAX_LINES + 8)
    el.style.height = `${Math.max(LINE_PX + 8, h)}px`
  }, [text])

  // A card that arrives after Cancel was confirmed is dropped on the spot (it clears the pending record
  // too, so a reload cannot bring it back), and the run is over once nothing is busy. Watched on the
  // store, so the frame that carries the card is caught whether or not React has rendered it.
  useEffect(() => {
    if (!cancelIntent) return
    const watch = (s: { status: typeof status; dropClarify: () => Promise<void> }) => {
      if (s.status === 'clarify') {
        void s.dropClarify()
        setAnnounce('Cancelled — nothing was changed.')
        setCancelIntent(false)
      } else if (!isBusy(s.status)) setCancelIntent(false)
    }
    const unsubscribe = usePromptStore.subscribe(watch)
    queueMicrotask(() => watch(usePromptStore.getState()))
    return unsubscribe
  }, [cancelIntent])

  // Esc during a run opens the confirm; focus moves onto "Keep going" so a
  // second Esc keeps going and Enter on the red button cancels.
  useEffect(() => { if (askCancel) keepRef.current?.focus() }, [askCancel])

  // QA-064: "Esc to cancel" must work wherever focus is while a run is going.
  // It was handled only in the textarea's onKeyDown, and a run moves focus
  // off it (onto the Cancel button, or to <body> when the clarify card
  // unmounts), so Esc did nothing. A window listener (bubble phase: the
  // keymap's capture-phase Escape → deselect still runs, and other dialogs'
  // own Esc handlers keep theirs) opens the same confirm.
  useEffect(() => {
    if (!busy || cancelling) return
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key !== 'Escape' || e.repeat) return
      const t = e.target as HTMLElement | null
      // Another dialog or popover owns this Esc (it closes itself).
      if (t?.closest?.('[role="dialog"], [role="alertdialog"], [role="menu"]')) return
      setAskCancel(true)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [busy, cancelling])

  // The card's first control, in the order ClarifyCard itself focuses it:
  // a text/number field, else the selected chip, else the primary action.
  // Deferred a frame: called from the textarea's Enter keydown, and Chrome
  // "clicks" a focused button on the keypress that FOLLOWS that keydown — so
  // focusing Run synchronously pressed it, and the card showed "Still
  // needed: …" for an attempt nobody made (QA-063).
  const focusCard = () => {
    requestAnimationFrame(() => {
      const f = formRef.current
      const el = f?.querySelector<HTMLElement>(`.${PREVIEW_CARD_CLASS} button.primary`)
        ?? f?.querySelector<HTMLElement>('.clarify input, .clarify textarea')
        ?? f?.querySelector<HTMLElement>('.clarify [role="radio"][tabindex="0"]')
        ?? f?.querySelector<HTMLElement>('.clarify [role="radio"]')
        ?? f?.querySelector<HTMLElement>('.clarify .clarify-actions button')
      el?.focus()
    })
  }
  // The card takes focus when it APPEARS (wave-B review: 3 of 4 trials left
  // focus in the textarea — the card's own mount-time focus lost the race
  // with the run's re-render). Only when focus is not already inside it.
  // Never from a field the person is typing in outside the bar (the Playhead
  // timecode, the Chat box): the Enter meant for it pressed Apply (final
  // sweep 3 r2, CRITICAL). The live region says the card is ready instead.
  useEffect(() => {
    if (status !== 'clarify' || !clarify) return
    const inCard = () => !!document.activeElement?.closest?.(`.clarify, .${PREVIEW_CARD_CLASS}`)
    if (!inCard() && cardMayTakeFocusNow(formRef.current)) focusCard()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, clarify?.token])

  // Cancel, confirmed. A footage read is a job of its own: name it (the frame carries its id) so the
  // read really stops, then ask the run to stop too (prompt/cancel reaches the job on a backend that
  // does not put the id on the frame). The timeline was never touched.
  const stopRun = () => {
    const job = usePromptStore.getState().analysis?.jobId
    if (job) void api.cancelJob(job).catch(() => undefined)
    if (brainOn) setCancelIntent(true)        // with the brain off the bar behaves exactly as 0.8.0
    void cancel()
  }

  const submit = () => {
    const open = usePromptStore.getState().clarify
    if (!disabled && status === 'clarify' && open?.preview) {
      // "yes" / "no" typed over the card answers it — the card's own text says
      // "Reply yes to apply or no to change it"; it used to drop the card and
      // plan "yes" as a new request (final sweep 3 r2).
      const reply = previewReplyOf(text)
      if (reply === 'apply') { setText(''); void applyPreview(); return }
      if (reply === 'change') { changePreview(true); return }
    }
    if (!disabled && supersedesClarify(status, text, usePromptStore.getState().prompt)) {
      const t = text.trim()
      setHistIdx(-1)
      setDraft('')
      void dropClarify().then(() => run(t))
      return
    }
    if (!canSubmitPrompt(status, { disabled, text, cardOpen: !!open })) {
      // A card is waiting: Enter here means "answer it", so send the user there
      // instead of re-planning the sentence over the open question.
      if (status === 'clarify') focusCard()
      return
    }
    const t = text.trim()
    setHistIdx(-1)
    setDraft('')
    void run(t)
  }

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    const el = e.currentTarget
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit(); return }
    if (e.key === 'Escape') {
      e.preventDefault()
      if (busy) { setAskCancel(true); return }
      if (status === 'clarify' && usePromptStore.getState().clarify?.preview) { changePreview(); return }
      if (status === 'clarify') { void dropClarify(); return }
      if (text) { setText(''); setHistIdx(-1); return }
      if (logOpen) { usePromptStore.getState().setLogOpen(false); return }
      // an empty bar: Esc hands the keyboard back to the timeline, so
      // Space, J/K/L and ⌥9 work again without F6 (review RD2)
      focusTimeline()
      return
    }
    const atStart = el.selectionStart === 0 && el.selectionEnd === 0
    const atEnd = el.selectionStart === el.value.length && el.selectionEnd === el.value.length
    if (e.key === 'ArrowUp' && atStart && history.length) {
      e.preventDefault()
      const next = Math.min(histIdx + 1, history.length - 1)
      if (histIdx === -1) setDraft(text)
      setHistIdx(next)
      setText(history[next])
      requestAnimationFrame(() => el.setSelectionRange(0, 0))
      return
    }
    if (e.key === 'ArrowDown' && atEnd && histIdx >= 0) {
      e.preventDefault()
      const next = histIdx - 1
      setHistIdx(next)
      setText(next === -1 ? draft : history[next])
    }
  }

  // Change: drop the card (nothing was committed) and hand the sentence back
  // for editing — restored from the run when the field is empty (a card that
  // came back after a reload).
  const changePreview = (restoreSentence = false) => {
    void dropClarify()
    if ((restoreSentence || !text.trim()) && lastPrompt) setText(lastPrompt)
    setAnnounce('Preview dropped — nothing was changed. Edit the prompt and press Enter.')
    requestAnimationFrame(() => {
      const el = taRef.current
      if (!el) return
      el.focus()
      el.setSelectionRange(el.value.length, el.value.length)
    })
  }

  // A key typed over the preview card goes to the prompt, never to the card's
  // focused Apply (final sweep 3: "no wait" applied the card at its first
  // space). Focus moves NOW, in the keydown, so the next keys land in the
  // prompt; `/` selects the sentence, any other character starts a new one.
  // The card stays open until Enter plans the new sentence (or Esc).
  const typeOverCard = (key: string) => {
    const el = taRef.current
    if (!el) return
    el.focus()
    if (key === '/') { el.select(); return }
    setText(typedOverCard(key, text))
    setHistIdx(-1)
  }

  const reading = brainOn && busy && steps.length === 0 ? analysis : null
  const progress = busy ? (runProgress(steps) ?? (reading ? reading.pct / 100 : null)) : null
  // The counter near the server's 4000-character limit, and the refusal past it (QA-124).
  const lengthNote = promptLengthNote(text)
  const cls = [
    'prompt-bar',
    busy ? 'is-busy' : '',
    status === 'planning' ? 'is-planning' : '',
    // a preview that would change nothing is not a green "done" (final sweep 3 r2)
    status === 'done' && !nothingToApply ? 'is-done' : '',
    status === 'error' ? 'is-error' : '',
    status === 'cancelled' ? 'is-cancelled' : '',
    cancelling ? 'is-cancelling' : '',
  ].filter(Boolean).join(' ')
  const showLog = logOpen && status !== 'clarify' && (steps.length > 0 || !!plan || !!reply || !!lastError || busy)
  const placeholder = disabled
    ? (chatBusy ? 'Chat is working — the Prompt bar waits until it finishes' : 'Open a project to start')
    : cancelling ? 'Stopping after the current step…'
    : busy ? 'Working… Esc to cancel' : `Try: ${EXAMPLES[example]}`

  return (
    <form
      ref={formRef}
      className={cls}
      role="form"
      aria-label="Prompt editor"
      data-keymap-ignore
      style={{ '--prompt-progress': progress ?? 0 } as React.CSSProperties}
      onSubmit={(e) => { e.preventDefault(); submit() }}
    >
      <div className="prompt-row">
        <span className="prompt-glyph" aria-hidden="true"><Icon name="chevronRight" /></span>
        <textarea
          ref={taRef}
          className="prompt-input"
          rows={1}
          value={text}
          placeholder={placeholder}
          aria-label="What should happen to this video?"
          aria-keyshortcuts="Enter / ArrowUp ArrowDown Escape"
          aria-invalid={lengthNote?.over || undefined}
          aria-describedby={lengthNote ? 'prompt-length-note' : undefined}
          disabled={disabled}
          readOnly={busy}
          spellCheck={false}
          onChange={(e) => { setText(e.target.value); setHistIdx(-1) }}
          onKeyDown={onKeyDown}
        />
        <div className="prompt-actions">
          <BrainBadge />
          {busy ? (
            <button type="button" className="prompt-run is-cancel" disabled={cancelling}
                    onClick={() => setAskCancel(true)}>
              {cancelling ? 'Stopping…' : <>Cancel<kbd>Esc</kbd></>}
            </button>
          ) : (
            <button type="submit" className="prompt-run primary" disabled={disabled || !text.trim() || !!lengthNote?.over}>
              Run<kbd>↵</kbd>
            </button>
          )}
        </div>
        <div className="prompt-line" aria-hidden="true" />
      </div>

      {lengthNote && (
        <div id="prompt-length-note" className={`prompt-limit${lengthNote.over ? ' is-over' : ''}`}
             role={lengthNote.over ? 'alert' : 'status'}>
          {lengthNote.text}
        </div>
      )}

      {askCancel && busy && !cancelling && (
        <div className="prompt-confirm" role="alertdialog" aria-label="Cancel the run?"
             onKeyDown={(e) => { if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); setAskCancel(false); taRef.current?.focus() } }}>
          <span className="grow">Cancel the run? The timeline is unchanged until it finishes.</span>
          <button type="button" className="danger" onClick={() => { setAskCancel(false); stopRun() }}>Cancel run</button>
          <button type="button" ref={keepRef} onClick={() => { setAskCancel(false); taRef.current?.focus() }}>Keep going</button>
        </div>
      )}

      {reading && <AnalysisProgressLine analysis={reading} />}

      {status === 'clarify' && clarify?.preview && !cancelIntent && (
        <PromptPreviewCard
          key={clarify.token}
          preview={clarify.preview}
          onApply={() => void applyPreview()}
          onChange={() => changePreview()}
          onTypeAhead={typeOverCard}
          onSeek={(t) => useStore.getState().setPlayhead(t)}
        />
      )}

      {status === 'clarify' && clarify && !clarify.preview && !cancelIntent && (
        <ClarifyCard
          key={clarify.token}
          questions={clarify.questions}
          plan={plan}
          onSubmit={(a) => void answer(a)}
          onCancel={() => { void dropClarify(); taRef.current?.focus() }}
        />
      )}

      {showLog && <PromptRunLog />}

      {!showLog && status === 'idle' && !text && history.length === 0 && sid && (
        <div className="prompt-hint"><kbd className="kbd">Enter</kbd> runs · <kbd className="kbd">↑</kbd> recalls · <kbd className="kbd">/</kbd> focuses from anywhere</div>
      )}

      <div className="prompt-sr-only" aria-live="polite" aria-atomic="true">{announce}</div>
    </form>
  )
}
