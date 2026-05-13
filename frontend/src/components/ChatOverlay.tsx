import { useEffect, useRef, useState } from 'react'
import { useStore } from '../store'

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
}

export function ChatOverlay() {
  const sid = useStore((s) => s.sessionId)
  const refresh = useStore((s) => s.refresh)
  const renderPreview = useStore((s) => s.renderPreview)

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
    if (!text || !sid || busy) return
    setInput('')
    setMsgs((m) => [...m, { role: 'user', text }])
    setBusy(true)
    try {
      const res = await fetch(`/api/sessions/${sid}/chat`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ message: text }),
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
          const evt: ChatEvent = JSON.parse(line.slice(6))
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
            setMsgs((m) => [...m, { role: 'assistant', text: `Error: ${evt.message}` }])
          }
        }
      }
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
            {busy && <div style={{ color: 'var(--text-dim)' }}>…</div>}
          </div>
          <footer>
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={onKeyDown}
              placeholder={busy ? 'Working…' : 'Tell Claude what to do — Enter to send'}
              disabled={busy}
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
