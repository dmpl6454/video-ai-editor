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
//                (a run is a one-op edit that finishes on the Mac whether or not
//                this tab is watching; aborting it silently would be a lie about
//                what the timeline is doing — spec §5.2, §4.2)
//
// The bar is also the reconnect point: on mount and when a `prompt` op lands
// from elsewhere (the phone, another tab) it reads `GET …/prompt/run` and
// re-attaches to a run that is still going.

import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { useStore } from '../store'
import { usePromptStore, isBusy } from '../lib/promptStore'
import { runProgress, terminalAnnouncement } from '../lib/promptEvents'
import { BrainBadge } from './BrainBadge'
import { ClarifyCard } from './ClarifyCard'
import { PromptRunLog } from './PromptRunLog'
import './promptBar.css'

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
  const run = usePromptStore((s) => s.run)
  const answer = usePromptStore((s) => s.answer)
  const cancel = usePromptStore((s) => s.cancel)
  const dropClarify = usePromptStore((s) => s.dropClarify)
  const reconnect = usePromptStore((s) => s.reconnect)

  const [text, setText] = useState('')
  const [histIdx, setHistIdx] = useState(-1)     // -1 = the live draft
  const [draft, setDraft] = useState('')
  const [example, setExample] = useState(0)
  const [askCancel, setAskCancel] = useState(false)
  const [announce, setAnnounce] = useState('')
  const taRef = useRef<HTMLTextAreaElement>(null)
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
  // ends — but only if focus was not somewhere the user put it on purpose.
  useEffect(() => { if (focusNonce > 0) { taRef.current?.focus(); taRef.current?.select() } }, [focusNonce])
  useEffect(() => {
    const was = prevStatus.current
    prevStatus.current = status
    if (isBusy(was) && !isBusy(status)) {
      setAskCancel(false)
      const active = document.activeElement
      const insideBar = !!active && !!taRef.current?.closest('.prompt-bar')?.contains(active)
      if (!active || active === document.body || insideBar) taRef.current?.focus()
    }
    if (isBusy(was) && !isBusy(status) && status !== 'clarify') setText('')
    const line = terminalAnnouncement(usePromptStore.getState())
    if (line && !isBusy(status) && was !== status) setAnnounce(line)
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

  // Esc during a run opens the confirm; focus moves onto "Keep going" so a
  // second Esc keeps going and Enter on the red button cancels.
  useEffect(() => { if (askCancel) keepRef.current?.focus() }, [askCancel])

  const submit = () => {
    if (busy || disabled) return
    const t = text.trim()
    if (!t) return
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
      if (status === 'clarify') { void dropClarify(); return }
      if (text) { setText(''); setHistIdx(-1); return }
      if (logOpen) usePromptStore.getState().setLogOpen(false)
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

  const progress = busy ? runProgress(steps) : null
  const cls = [
    'prompt-bar',
    busy ? 'is-busy' : '',
    status === 'planning' ? 'is-planning' : '',
    status === 'done' ? 'is-done' : '',
    status === 'error' ? 'is-error' : '',
  ].filter(Boolean).join(' ')
  const showLog = logOpen && status !== 'clarify' && (steps.length > 0 || !!plan || !!reply || !!lastError || busy)
  const placeholder = disabled
    ? (chatBusy ? 'Chat is working — the bar waits for the same session lock' : 'Open a project to start')
    : busy ? 'Working… Esc to cancel' : `Try: ${EXAMPLES[example]}`

  return (
    <form
      className={cls}
      role="form"
      aria-label="Prompt editor"
      data-keymap-ignore
      style={{ '--prompt-progress': progress ?? 0 } as React.CSSProperties}
      onSubmit={(e) => { e.preventDefault(); submit() }}
    >
      <div className="prompt-row">
        <span className="prompt-glyph" aria-hidden="true">›</span>
        <textarea
          ref={taRef}
          className="prompt-input"
          rows={1}
          value={text}
          placeholder={placeholder}
          aria-label="What should happen to this video?"
          aria-keyshortcuts="Enter / ArrowUp ArrowDown Escape"
          disabled={disabled}
          readOnly={busy}
          spellCheck={false}
          onChange={(e) => { setText(e.target.value); setHistIdx(-1) }}
          onKeyDown={onKeyDown}
        />
        <div className="prompt-actions">
          <BrainBadge />
          {busy ? (
            <button type="button" className="prompt-run is-cancel" onClick={() => setAskCancel(true)}>
              Cancel<kbd>Esc</kbd>
            </button>
          ) : (
            <button type="submit" className="prompt-run primary" disabled={disabled || !text.trim()}>
              Run<kbd>↵</kbd>
            </button>
          )}
        </div>
        <div className="prompt-line" aria-hidden="true" />
      </div>

      {askCancel && busy && (
        <div className="prompt-confirm" role="alertdialog" aria-label="Cancel the run?"
             onKeyDown={(e) => { if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); setAskCancel(false); taRef.current?.focus() } }}>
          <span className="grow">Cancel the run? The timeline is unchanged until it finishes.</span>
          <button type="button" className="danger" onClick={() => { setAskCancel(false); void cancel() }}>Cancel run</button>
          <button type="button" ref={keepRef} onClick={() => { setAskCancel(false); taRef.current?.focus() }}>Keep going</button>
        </div>
      )}

      {status === 'clarify' && clarify && (
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
        <div className="prompt-hint">Enter runs · ↑ recalls · <kbd className="kbd">/</kbd> focuses from anywhere</div>
      )}

      <div className="prompt-sr-only" aria-live="polite" aria-atomic="true">{announce}</div>
    </form>
  )
}
