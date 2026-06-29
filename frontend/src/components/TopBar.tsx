import { useEffect, useRef, useState } from 'react'
import { useStore } from '../store'
import { api } from '../api'
import { openHelp } from './Help'
import { openShortcuts } from './ShortcutsSettings'

interface SessionRow { id: string; name: string }

export function TopBar() {
  const name = useStore((s) => s.sessionName)
  const dispatch = useStore((s) => s.dispatch)
  const exporting = useStore((s) => s.exporting)
  const exportUrl = useStore((s) => s.exportUrl)
  const exportStatus = useStore((s) => s.exportStatus)
  const exportError = useStore((s) => s.exportError)
  const clearExportError = useStore((s) => s.clearExportError)
  const doExport = useStore((s) => s.doExport)
  const [exportElapsed, setExportElapsed] = useState(0)
  const edl = useStore((s) => s.edl)
  const sid = useStore((s) => s.sessionId)
  const refresh = useStore((s) => s.refresh)
  const [saving, setSaving] = useState(false)
  const [savedUrl, setSavedUrl] = useState<string | null>(null)
  const importRef = useRef<HTMLInputElement>(null)
  const [sessions, setSessions] = useState<SessionRow[]>([])
  const [pickerOpen, setPickerOpen] = useState(false)
  const [appVersion, setAppVersion] = useState('')

  useEffect(() => {
    fetch('/api/version').then((r) => r.json())
      .then((d) => setAppVersion(d.version || '')).catch(() => {})
  }, [])

  // Tick an elapsed-seconds counter while an export is running so the button
  // shows live progress instead of a frozen "Exporting…".
  useEffect(() => {
    if (!exporting) { setExportElapsed(0); return }
    const startedAt = Date.now()
    const id = window.setInterval(() => {
      setExportElapsed(Math.floor((Date.now() - startedAt) / 1000))
    }, 1000)
    return () => window.clearInterval(id)
  }, [exporting])

  const onSaveProject = async () => {
    if (!sid) return
    setSaving(true)
    setSavedUrl(null)
    try {
      const r = await api.saveProject(sid)
      setSavedUrl(r.url)
    } finally {
      setSaving(false)
    }
  }

  const onLoadProject = async (file: File) => {
    const r = await api.loadProject(file)
    // Switch to the new session and refresh
    useStore.setState({ sessionId: r.id, sessionName: r.id })
    await refresh()
  }

  // Load sessions when picker opens; close on outside click
  useEffect(() => {
    if (!pickerOpen) return
    api.listSessions().then((r) => setSessions(r.sessions ?? [])).catch(() => {})
    const close = (e: MouseEvent) => {
      const tgt = e.target as HTMLElement
      if (!tgt.closest('[data-session-picker]')) setPickerOpen(false)
    }
    setTimeout(() => window.addEventListener('mousedown', close), 0)
    return () => window.removeEventListener('mousedown', close)
  }, [pickerOpen])

  const switchSession = async (newId: string) => {
    setPickerOpen(false)
    if (newId === sid) return
    useStore.setState({ sessionId: newId, sessionName: newId })
    await refresh()
  }

  const newSession = async () => {
    setPickerOpen(false)
    const r = await api.createSession(`project ${new Date().toLocaleString()}`)
    useStore.setState({ sessionId: r.id, sessionName: r.name })
    await refresh()
  }

  return (
    <header className="topbar">
      <h1>Video AI Editor</h1>
      <div data-session-picker style={{ position: 'relative' }}>
        <button
          className="pill"
          title="Switch project"
          onClick={() => setPickerOpen((o) => !o)}
          style={{ cursor: 'pointer', padding: '3px 10px', fontSize: 11 }}
        >
          {name} ▾
        </button>
        {pickerOpen && (
          <div
            style={{
              position: 'absolute', left: 0, top: '100%', marginTop: 4, zIndex: 100,
              background: 'var(--bg-2)', border: '1px solid var(--line)', borderRadius: 6,
              boxShadow: '0 8px 24px rgba(0,0,0,0.5)', minWidth: 280, maxHeight: 400,
              overflow: 'auto', padding: 4,
            }}
          >
            <div
              onClick={newSession}
              style={{ padding: '6px 10px', cursor: 'pointer', fontSize: 12, borderRadius: 3,
                       borderBottom: '1px solid var(--line)' }}
              onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--bg-3)')}
              onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
            >
              ＋ New project
            </div>
            {sessions.length === 0 && (
              <div style={{ padding: '6px 10px', fontSize: 11, color: 'var(--text-dim)' }}>
                Loading…
              </div>
            )}
            {sessions.map((s) => (
              <div
                key={s.id}
                onClick={() => switchSession(s.id)}
                title={s.id}
                style={{
                  padding: '6px 10px', cursor: 'pointer', fontSize: 12, borderRadius: 3,
                  background: s.id === sid ? 'var(--bg-3)' : 'transparent',
                  fontWeight: s.id === sid ? 600 : 400,
                  display: 'flex', justifyContent: 'space-between', gap: 8,
                }}
                onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--bg-3)')}
                onMouseLeave={(e) => (e.currentTarget.style.background = s.id === sid ? 'var(--bg-3)' : 'transparent')}
              >
                <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {s.name || s.id}
                </span>
                <span style={{ color: 'var(--text-dim)', fontSize: 10 }}>{s.id.slice(0, 10)}</span>
              </div>
            ))}
          </div>
        )}
      </div>
      {edl && (
        <span className="pill">
          {edl.canvas.w}×{edl.canvas.h} · {edl.canvas.fps}fps · {edl.duration.toFixed(1)}s
        </span>
      )}
      <div className="grow" />
      <button onClick={() => dispatch('undo')} title="Cmd+Z">Undo</button>
      <button onClick={() => dispatch('redo')} title="Cmd+Shift+Z">Redo</button>
      {(['9:16', '16:9', '1:1', '4:5'] as const).map((r) => (
        <button key={r} onClick={() => dispatch('set_aspect_ratio', { ratio: r })}>{r}</button>
      ))}
      <span style={{ width: 1, height: 20, background: 'var(--line)', margin: '0 4px' }} />
      {[
        { label: 'Reels',    title: 'Instagram Reels — 1080×1920 @ 30fps',  w: 1080, h: 1920, fps: 30 },
        { label: 'Shorts',   title: 'YouTube Shorts — 1080×1920 @ 30fps',   w: 1080, h: 1920, fps: 30 },
        { label: 'TikTok',   title: 'TikTok — 1080×1920 @ 30fps',           w: 1080, h: 1920, fps: 30 },
        { label: 'IG 1:1',   title: 'Instagram feed square — 1080×1080',    w: 1080, h: 1080, fps: 30 },
        { label: 'IG 4:5',   title: 'Instagram feed portrait — 1080×1350',  w: 1080, h: 1350, fps: 30 },
      ].map((p) => (
        <button
          key={p.label}
          title={p.title}
          onClick={() => dispatch('set_canvas', { w: p.w, h: p.h, fps: p.fps })}
          style={{ fontSize: 11 }}
        >
          {p.label}
        </button>
      ))}
      <button onClick={openHelp} title="Keyboard shortcuts (?)" style={{ fontSize: 11 }}>?</button>
      <button onClick={openShortcuts} title="Customize keyboard shortcuts (CapCut / Premiere / Final Cut)" style={{ fontSize: 13 }}>⌨</button>
      {appVersion && (
        <span title="App version" style={{ fontSize: 10, color: 'var(--text-dim, #888)', opacity: 0.7 }}>
          v{appVersion}
        </span>
      )}
      <button onClick={onSaveProject} disabled={saving || !edl?.duration} title="Save the project as a .vae bundle (EDL + media)">
        {saving ? 'Saving…' : '💾 Save'}
      </button>
      <button onClick={() => importRef.current?.click()} title="Open a saved .vae project">
        📂 Open
      </button>
      <input ref={importRef} type="file" accept=".vae,.zip" hidden
        onChange={(e) => { const f = e.target.files?.[0]; if (f) void onLoadProject(f) }} />
      {savedUrl && (
        <a href={savedUrl} download style={{ color: 'var(--good)', fontSize: 12 }}>
          ↓ .vae
        </a>
      )}
      <button className="primary" onClick={() => doExport()} disabled={exporting || !edl?.duration}>
        {exporting
          ? `Exporting${exportStatus === 'queued' ? ' (queued)' : ''}… ${exportElapsed}s`
          : 'Export'}
      </button>
      {exportUrl && !exporting && (
        <a href={exportUrl} download style={{ color: 'var(--good)', fontSize: 12 }}>
          ↓ MP4
        </a>
      )}
      {exportError && (
        <span
          style={{ color: 'var(--accent)', fontSize: 12, cursor: 'pointer' }}
          title={`${exportError} (click to dismiss)`}
          onClick={() => clearExportError()}
        >
          ⚠ Export failed ✕
        </span>
      )}
    </header>
  )
}
