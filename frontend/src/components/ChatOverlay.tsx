import { useEffect, useRef, useState } from 'react'
import { useStore, errorMessage } from '../store'
import { keyIsMissing, useApiKey } from '../lib/apiKey'
import { openApiKeySettings } from './ApiKeySettings'

type ChatEvent =
  | { type: 'text_delta'; text: string }
  | { type: 'tool_use'; name: string; args: Record<string, unknown>; id: string }
  | { type: 'tool_result'; name: string; result: unknown; id: string; is_error?: boolean }
  | { type: 'op'; op: { tool: string; summary: string } }
  | { type: 'done' }
  | { type: 'error'; message: string }

interface Msg {
  role: 'user' | 'assistant' | 'tool'
  text?: string
  tool?: string
  args?: Record<string, unknown>
  result?: unknown
  ok?: boolean
  /** Render the key dialog's button under this message. Set only for the
   *  backend's key errors, which are the one chat failure the user can fix
   *  from here — telling them to click a button is weaker than giving them
   *  the button. */
  keyAction?: boolean
}

/** Does this error message name the API key as the problem?
 *
 *  Matches the copy `agent/loop.py::_KEY_HOWTO` is appended to (both the
 *  no-key and the bad-key line say "Anthropic API key"). Deliberately narrow:
 *  the credit-balance line says "Anthropic API credit balance" and must NOT
 *  offer a key dialog, since re-pasting the same key fixes nothing there.
 *  A miss costs only the button — the sentence itself already explains it. */
function isKeyError(message: string): boolean {
  return /anthropic api key/i.test(message)
}

export function ChatOverlay() {
  const sid = useStore((s) => s.sessionId)
  const refresh = useStore((s) => s.refresh)
  const renderPreview = useStore((s) => s.renderPreview)

  // Chat is the ONE feature that needs a key. When we KNOW there isn't one
  // (never on 'unknown' — see lib/apiKey), say so where the failure would
  // otherwise happen, and offer the fix inline: the alternative the user was
  // left with was a mid-conversation auth error and a dotfile to hand-create.
  const keyMissing = keyIsMissing(useApiKey((s) => s.status))

  const [open, setOpen] = useState(true)
  const [msgs, setMsgs] = useState<Msg[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const bodyRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (bodyRef.current) bodyRef.current.scrollTop = bodyRef.current.scrollHeight
  }, [msgs])

  async function send() {
    const text = input.trim()
    if (!text || !sid || busy || keyMissing) return
    setInput('')
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
      const reader = res.body.getReader()
      const dec = new TextDecoder()
      let buf = ''
      let assistantText = ''
      while (true) {
        const { value, done } = await reader.read()
        if (done) break
        buf += dec.decode(value, { stream: true })
        const lines = buf.split('\n\n')
        buf = lines.pop() ?? ''
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          // Per-LINE guard: one malformed frame must not abort the whole stream.
          // An uncaught throw here escaped the read loop, so chat stopped
          // mid-sentence with no message and no way to tell it had stopped.
          let evt: ChatEvent
          try {
            evt = JSON.parse(line.slice(6)) as ChatEvent
          } catch {
            console.warn('[chat] skipping malformed SSE frame:', line.slice(0, 120))
            continue
          }
          if (evt.type === 'text_delta') {
            assistantText += evt.text
            setMsgs((m) => {
              const last = m[m.length - 1]
              if (last && last.role === 'assistant' && last.text !== undefined) {
                return [...m.slice(0, -1), { ...last, text: assistantText }]
              }
              return [...m, { role: 'assistant', text: assistantText }]
            })
          } else if (evt.type === 'tool_use') {
            setMsgs((m) => [...m, { role: 'tool', tool: evt.name, args: evt.args }])
            // start a fresh assistant accumulator after tool use
            assistantText = ''
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
            const keyErr = isKeyError(evt.message)
            setMsgs((m) => [...m, { role: 'assistant', text: `Error: ${evt.message}`,
                                    keyAction: keyErr }])
            // The backend just told us something about the key that our cached
            // status may not reflect (it drives the banner, the input's enabled
            // state and the TopBar affordance's colour). Only on a key error —
            // a rate limit says nothing about whether a key exists.
            if (keyErr) void useApiKey.getState().refresh()
          }
        }
      }
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

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      void send()
    }
  }

  return (
    <>
      {!open && (
        <button className="chat-fab" onClick={() => setOpen(true)} title="Chat with Claude">
          💬 Chat
        </button>
      )}
      {open && (
        <div className="chat-pane">
          <header>
            <strong>Chat with Claude</strong>
            <div style={{ flex: 1 }} />
            <button onClick={() => setOpen(false)}>×</button>
          </header>
          <div className="body" ref={bodyRef}>
            {keyMissing && (
              <div style={{
                background: 'var(--bg-2)', border: '1px solid var(--line)',
                borderLeft: '2px solid var(--warn)', borderRadius: 6,
                padding: '8px 10px', marginBottom: 10, color: 'var(--text)',
              }}>
                <div style={{ marginBottom: 6 }}>
                  Chat needs your Anthropic API key. Everything else in the
                  editor works without one.
                </div>
                <button onClick={openApiKeySettings} style={{ fontSize: 11 }}>
                  🔑 Add API key
                </button>
              </div>
            )}
            {msgs.length === 0 && !keyMissing && (
              <div style={{ color: 'var(--text-dim)' }}>
                Try: <em>"Apply my brand kit @yourhandle with #yourtag, generate a hook,
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
                  <div style={{ whiteSpace: 'pre-wrap', color: 'var(--text)' }}>
                    {m.text}
                    {m.keyAction && (
                      <div style={{ marginTop: 6 }}>
                        <button onClick={openApiKeySettings} style={{ fontSize: 11 }}>
                          🔑 Add API key
                        </button>
                      </div>
                    )}
                  </div>
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
            {busy && <div style={{ color: 'var(--text-dim)' }}>…</div>}
          </div>
          <footer>
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={onKeyDown}
              placeholder={keyMissing
                ? 'Add an Anthropic API key above to use chat'
                : busy ? 'Working…' : 'Tell Claude what to do — Enter to send'}
              disabled={busy || keyMissing}
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
