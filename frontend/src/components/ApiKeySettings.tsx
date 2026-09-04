import { useEffect, useState } from 'react'
import { useApiKey } from '../lib/apiKey'

let _setOpen: ((v: boolean) => void) | null = null
/** Open the key dialog — the TopBar affordance and the chat pane's banner both
 *  call this, the same handle pattern Help/ShortcutsSettings use. */
export function openApiKeySettings() { _setOpen?.(true) }

/** Where to enter the ANTHROPIC_API_KEY, from inside the app.
 *
 *  There was no in-app route at all: a packaged .app user had to hand-create a
 *  dotfile inside a Finder-hidden folder to make the chat pane work, which is
 *  not an instruction a video editor can give. The one rule this dialog keeps
 *  is that the key is write-only from here on — it is never fetched back, never
 *  rendered, and never held past a successful save.
 */
export function ApiKeySettings() {
  const [open, setOpen] = useState(false)
  const [key, setKey] = useState('')
  const status = useApiKey((s) => s.status)
  const saving = useApiKey((s) => s.saving)
  const error = useApiKey((s) => s.error)
  const savedAt = useApiKey((s) => s.savedAt)
  const [savedAtSeen, setSavedAtSeen] = useState(0)

  useEffect(() => {
    // Opening always starts clean: no leftover key in the field, and no stale
    // "✓ Key saved" from a previous visit sitting above an empty box.
    _setOpen = (v: boolean) => {
      setOpen(v)
      // Clear the store error too: it outlives the modal, so reopening after a
      // rejected save showed "that doesn't look like an Anthropic API key"
      // above an empty field, as if the blank input had already been judged.
      if (v) { setKey(''); setSavedAtSeen(0); useApiKey.setState({ error: null }) }
    }
    return () => { _setOpen = null }
  }, [])

  // The app's single probe of key status. Mounted once (App.tsx), so the
  // TopBar affordance and the chat banner both read one answer instead of
  // asking the backend twice.
  useEffect(() => { void useApiKey.getState().refresh() }, [])

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => { if (e.code === 'Escape') setOpen(false) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])

  if (!open) return null

  const close = () => {
    setOpen(false)
    setKey('')                       // never keep the secret around
  }

  const submit = async () => {
    const ok = await useApiKey.getState().save(key)
    if (ok) {
      setKey('')
      setSavedAtSeen(useApiKey.getState().savedAt)
    }
  }

  const justSaved = savedAt > 0 && savedAt === savedAtSeen
  // A key that doesn't look like one is worth flagging BEFORE the round trip —
  // but only as a hint: the backend is the authority on what it accepts, and a
  // future key format must not be unenterable because of a guess made here.
  const oddLooking = key.trim().length > 0 && !key.trim().startsWith('sk-')

  return (
    <div
      onClick={close}
      style={{
        position: 'fixed', inset: 0, zIndex: 200,
        background: 'rgba(0,0,0,0.6)', backdropFilter: 'blur(4px)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: 'var(--bg-1)', border: '1px solid var(--line)',
          borderRadius: 10, padding: 24, width: 'min(460px, 92vw)',
          boxShadow: '0 20px 60px rgba(0,0,0,0.6)',
        }}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
          <h2 style={{ margin: 0, fontSize: 14, fontWeight: 600 }}>Anthropic API key</h2>
          <button onClick={close}>Close</button>
        </div>

        <p style={{ margin: '0 0 10px', fontSize: 12, color: 'var(--text-dim)', lineHeight: 1.6 }}>
          “Tell Claude what to do” needs your own Anthropic API key. Everything
          else in the editor — importing, editing, preview and export — runs on
          this computer and works without one.
        </p>
        <p style={{ margin: '0 0 14px', fontSize: 12, color: 'var(--text-dim)', lineHeight: 1.6 }}>
          Create a key in the Anthropic Console under Settings → API keys, then
          paste it below. It is saved on this computer and sent only to
          Anthropic.
        </p>

        {status === 'configured' && (
          <div style={{ fontSize: 11, color: 'var(--good)', marginBottom: 8 }}>
            ✓ A key is saved on this computer. Paste a new one to replace it.
          </div>
        )}

        <input
          type="password"
          value={key}
          onChange={(e) => setKey(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') void submit() }}
          placeholder={status === 'configured' ? 'Replace the saved key…' : 'sk-ant-…'}
          autoComplete="off"
          spellCheck={false}
          autoFocus
          style={{ width: '100%', fontSize: 12, fontFamily: 'ui-monospace, monospace' }}
        />

        {oddLooking && (
          <div style={{ fontSize: 11, color: 'var(--warn)', marginTop: 6 }}>
            That doesn’t look like an Anthropic key — they start with “sk-”.
          </div>
        )}
        {error && (
          <div style={{ fontSize: 11, color: 'var(--accent)', marginTop: 6 }}>
            ⚠ {error}
          </div>
        )}
        {justSaved && !error && (
          <div style={{ fontSize: 11, color: 'var(--good)', marginTop: 6 }}>
            ✓ Key saved — chat is ready.
          </div>
        )}

        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', marginTop: 14 }}>
          <button onClick={close}>{justSaved ? 'Done' : 'Cancel'}</button>
          <button
            className="primary"
            onClick={() => void submit()}
            disabled={saving || key.trim().length === 0}
            title={key.trim().length === 0 ? 'Paste a key first' : 'Save the key on this computer'}
          >
            {saving ? 'Saving…' : 'Save key'}
          </button>
        </div>
      </div>
    </div>
  )
}
