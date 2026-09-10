// The chat pane. With an ANTHROPIC_API_KEY it is a Claude tool-use turn; without
// one, `/chat` delegates to the Prompt Editor (agent/loop.py, spec §4.6) and
// the same stream carries `brain`, `plan`, `step`, `verify` and `clarify`
// frames alongside the six it always had. This pane therefore:
//
//   * reads the stream with lib/promptEvents.readSseStream — the one loop it
//     shares with the Prompt bar, so a frame-handling fix lands in both;
//   * shows which brain answered as a header pill (the `brain` event, or the
//     `via <label> — ` prefix of the first text when only that arrived);
//   * renders a `clarify` as the same ClarifyCard the bar uses and answers it
//     through `POST …/prompt/answer`, consuming the resumed stream here;
//   * ignores `plan`/`step`/`verify` — the per-step tool_use/tool_result
//     lines already render them as a Claude turn (that pairing is why the
//     backend emits them, §4.1).
//
// The session lock is shared (§4.2): while the Prompt bar is running, this
// pane waits, and while a chat turn streams, the bar waits (`chatBusy`).

import { useEffect, useRef, useState } from 'react'
import { useStore, errorMessage } from '../store'
import { api } from '../api'
import { usePromptStore, isBusy } from '../lib/promptStore'
import { brainLabel, readSseStream, type ClarifyEvent, type Plan, type PromptEvent } from '../lib/promptEvents'
import type { Answers } from '../lib/clarifyDefaults'
import { ClarifyCard } from './ClarifyCard'

type ChatEvent = PromptEvent

interface Msg {
  role: 'user' | 'assistant' | 'tool'
  text?: string
  tool?: string
  args?: Record<string, unknown>
  result?: unknown
  ok?: boolean
}

interface PendingClarify { token: string; questions: ClarifyEvent['questions']; plan: Plan | null }

const VIA_RE = /^via ([^—]+?) — /

export function ChatOverlay() {
  const sid = useStore((s) => s.sessionId)
  const refresh = useStore((s) => s.refresh)
  const renderPreview = useStore((s) => s.renderPreview)
  const promptStatus = usePromptStore((s) => s.status)
  const setChatBusy = usePromptStore((s) => s.setChatBusy)

  const [open, setOpen] = useState(true)
  const [msgs, setMsgs] = useState<Msg[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [brain, setBrain] = useState<string | null>(null)
  const [pending, setPending] = useState<PendingClarify | null>(null)
  const bodyRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (bodyRef.current) bodyRef.current.scrollTop = bodyRef.current.scrollHeight
  }, [msgs, pending])

  // Tell the bar when this pane holds the lock; release on unmount too.
  useEffect(() => { setChatBusy(busy); return () => setChatBusy(false) }, [busy, setChatBusy])

  const promptBusy = isBusy(promptStatus)

  /** One event from either the chat turn or a resumed clarification. */
  function handle(evt: ChatEvent, acc: { text: string; plan: Plan | null }) {
    if (evt.type === 'text_delta') {
      acc.text += evt.text
      const via = VIA_RE.exec(acc.text)
      if (via) setBrain(via[1].trim())
      const text = acc.text
      setMsgs((m) => {
        const last = m[m.length - 1]
        if (last && last.role === 'assistant' && last.text !== undefined) {
          return [...m.slice(0, -1), { ...last, text }]
        }
        return [...m, { role: 'assistant', text }]
      })
    } else if (evt.type === 'tool_use') {
      setMsgs((m) => [...m, { role: 'tool', tool: evt.name, args: evt.args }])
      // start a fresh assistant accumulator after tool use
      acc.text = ''
    } else if (evt.type === 'tool_result') {
      setMsgs((m) => {
        const idx = [...m].reverse().findIndex((x) => x.role === 'tool' && x.tool === evt.name && x.result === undefined)
        if (idx === -1) return m
        const realIdx = m.length - 1 - idx
        const updated = { ...m[realIdx], result: evt.result, ok: !evt.is_error }
        return [...m.slice(0, realIdx), updated, ...m.slice(realIdx + 1)]
      })
    } else if (evt.type === 'op') {
      // EDL changed → refresh store + preview
      refresh().then(() => renderPreview())
    } else if (evt.type === 'error') {
      setMsgs((m) => [...m, { role: 'assistant', text: `Error: ${evt.message}` }])
    } else if (evt.type === 'brain') {
      if (evt.status === 'answered') setBrain(evt.label || brainLabel(evt.brain))
    } else if (evt.type === 'plan') {
      acc.plan = evt.plan
    } else if (evt.type === 'clarify') {
      setPending({ token: evt.token, questions: evt.questions, plan: acc.plan })
    }
    // `step` / `verify` / `done`: the tool lines and the final text cover them.
  }

  async function consume(res: Response) {
    const acc = { text: '', plan: null as Plan | null }
    await readSseStream(res.body!, (evt) => handle(evt as ChatEvent, acc))
  }

  async function send() {
    const text = input.trim()
    if (!text || !sid || busy || promptBusy) return
    setInput('')
    setPending(null)
    setMsgs((m) => [...m, { role: 'user', text }])
    setBusy(true)
    try {
      // Snapshot the editor UI state at send time so Claude can bind "this
      // clip" (selection) and "here" (playhead) to real clip ids.
      const { selection, multiSelection, playhead } = useStore.getState()
      const res = await fetch(`/api/sessions/${sid}/chat`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          message: text,
          selection: selection ?? null,
          multi_selection: multiSelection ?? [],
          playhead,
        }),
      })
      if (!res.ok || !res.body) {
        const errText = await res.text()
        setMsgs((m) => [...m, { role: 'assistant', text: `Error ${res.status}: ${errText}` }])
        return
      }
      await consume(res)
    } catch (e) {
      // Stream-LEVEL guard: a network drop mid-answer rejects reader.read().
      // Without this the rejection escaped `send()` entirely and the user was
      // left staring at a half-written reply, unsure whether Claude was still
      // thinking. Say what happened instead.
      setMsgs((m) => [...m, {
        role: 'assistant',
        text: `⚠ The connection dropped mid-answer (${errorMessage(e)}). `
            + `Any edits already applied are saved — send the message again to continue.`,
      }])
    } finally {
      setBusy(false)
    }
  }

  async function answerClarify(answers: Answers) {
    if (!sid || !pending || busy) return
    const { token } = pending
    setPending(null)
    setBusy(true)
    try {
      const res = await api.promptAnswer(sid, token, answers)
      await consume(res)
    } catch (e) {
      setMsgs((m) => [...m, { role: 'assistant', text: `Error: ${errorMessage(e)}` }])
    } finally {
      setBusy(false)
    }
  }

  async function dropClarify() {
    if (!sid || !pending) return
    const { token } = pending
    setPending(null)
    try { await api.promptCancel(sid, token) } catch (e) { console.warn('[chat] dropping the question failed:', errorMessage(e)) }
    setMsgs((m) => [...m, { role: 'assistant', text: 'Dropped the question — nothing was changed.' }])
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      void send()
    }
  }

  const placeholder = busy ? 'Working…'
    : promptBusy ? 'The Prompt bar is running — chat waits for the same session'
    : 'Tell the editor what to do — Enter to send'

  return (
    <>
      {!open && (
        <button className="chat-fab" onClick={() => setOpen(true)} title="Chat">
          💬 Chat
        </button>
      )}
      {open && (
        <div className="chat-pane">
          <header>
            <strong>Chat</strong>
            {brain && (
              // The same pill the Prompt bar wears (promptBar.css .brain-pill),
              // so "which brain answered" looks the same in both places.
              <span className="brain-pill" title="The brain that answered the last turn">
                <span className="brain-dot is-answered" aria-hidden="true" />
                <span className="name">via {brain}</span>
              </span>
            )}
            <div style={{ flex: 1 }} />
            <button onClick={() => setOpen(false)}>×</button>
          </header>
          <div className="body" ref={bodyRef}>
            {msgs.length === 0 && (
              <div style={{ color: 'var(--text-dim)' }}>
                Try: <em>"Apply my brand kit @quicksolutions.in with #techtips, generate a hook,
                burn IG-style captions, then audit and render the preview."</em>
              </div>
            )}
            {msgs.map((m, i) => (
              <div key={i} style={{ marginBottom: 10 }}>
                {m.role === 'user' && (
                  <div style={{ color: 'var(--text)' }}>
                    <b style={{ color: 'var(--accent-2)' }}>You:</b> {m.text}
                  </div>
                )}
                {m.role === 'assistant' && (
                  <div style={{ whiteSpace: 'pre-wrap', color: 'var(--text)' }}>{m.text}</div>
                )}
                {m.role === 'tool' && (
                  <div style={{
                    fontSize: 11,
                    background: 'var(--bg-2)',
                    border: '1px solid var(--line)',
                    borderRadius: 6,
                    padding: '4px 8px',
                    color: m.ok === false ? 'var(--accent)' : 'var(--good)',
                  }}>
                    🔧 <b>{m.tool}</b>({Object.entries(m.args ?? {}).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(', ')})
                    {m.result !== undefined && (
                      <span style={{ color: 'var(--text-dim)', marginLeft: 6 }}>
                        → {summarize(m.result)}
                      </span>
                    )}
                  </div>
                )}
              </div>
            ))}
            {pending && !busy && (
              <ClarifyCard
                key={pending.token}
                questions={pending.questions}
                plan={pending.plan}
                onSubmit={(a) => void answerClarify(a)}
                onCancel={() => void dropClarify()}
              />
            )}
            {busy && <div style={{ color: 'var(--text-dim)' }}>…</div>}
          </div>
          <footer>
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={onKeyDown}
              placeholder={placeholder}
              disabled={busy || promptBusy}
            />
          </footer>
        </div>
      )}
    </>
  )
}

function summarize(r: unknown): string {
  if (r && typeof r === 'object' && 'summary' in r) return String((r as { summary: unknown }).summary)
  if (r && typeof r === 'object' && 'score' in r) {
    const o = r as { score: number; issues?: unknown[] }
    return `score=${o.score} (${o.issues?.length ?? 0} issues)`
  }
  return JSON.stringify(r).slice(0, 80)
}
