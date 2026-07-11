import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useStore } from '../store'
import { api } from '../api'
import { openHelp } from './Help'
import { openShortcuts } from './ShortcutsSettings'

interface SessionRow { id: string; name: string }

export function TopBar() {
  const name = useStore((s) => s.sessionName)
  const dispatch = useStore((s) => s.dispatch)
  const redoAvailable = useStore((s) => s.redoAvailable)
  const pendingOps = useStore((s) => s.pendingOps)
  const exporting = useStore((s) => s.exporting)
  const exportUrl = useStore((s) => s.exportUrl)
  const exportGen = useStore((s) => s.exportGen)
  const opsLen = useStore((s) => s.ops.length)
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
  const [savedGen, setSavedGen] = useState(0)
  // A download link is "outdated" once history advances past the generation it
  // was made at. We keep the link (you can still grab the last render) but mark
  // it so nobody ships a stale file by mistake.
  const exportStale = !!exportUrl && opsLen > exportGen
  const savedStale = !!savedUrl && opsLen > savedGen
  const importRef = useRef<HTMLInputElement>(null)
  const [sessions, setSessions] = useState<SessionRow[]>([])
  const [pickerOpen, setPickerOpen] = useState(false)
  const [appVersion, setAppVersion] = useState('')
  // The session-picker dropdown is rendered via a portal to document.body
  // (positioned from this ref's rect) instead of as a normal absolutely-
  // positioned child of .topbar. .topbar clips overflow on both axes to keep
  // the toolbar on one line (see .topbar-scroll/.topbar-pinned), so a child
  // positioned `top:100%` — below the 44px toolbar row — was always cut off
  // by that same clip (issue 11, "dropdown is half-cut when clicked").
  const pickerBtnRef = useRef<HTMLButtonElement>(null)
  const [pickerPos, setPickerPos] = useState<{ left: number; top: number } | null>(null)

  // Export options popover — resolution + quality. `doExport()` already
  // forwarded `{height, crf}` all the way to POST /export (store.ts/api.ts),
  // but this button never passed anything, so every export used the hardcoded
  // defaults. Rendered via the same document.body portal pattern as the
  // session picker above, for the same reason (.topbar clips overflow).
  const [exportOptsOpen, setExportOptsOpen] = useState(false)
  const [exportHeight, setExportHeight] = useState<number>(edl?.canvas?.h ?? 1080)
  const [exportCrf, setExportCrf] = useState<number>(18)
  const exportBtnRef = useRef<HTMLButtonElement>(null)
  const [exportOptsPos, setExportOptsPos] = useState<{ left: number; top: number } | null>(null)

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
      setSavedGen(useStore.getState().ops.length)
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
    // Compute the portal's position here (an effect), not inline during
    // render — reading a ref's .current mid-render doesn't participate in
    // React's reactivity model and can read a stale layout on some render
    // paths (react-hooks/refs flags this for good reason, not just style).
    const rect = pickerBtnRef.current?.getBoundingClientRect()
    if (rect) setPickerPos({ left: rect.left, top: rect.bottom + 4 })
    api.listSessions().then((r) => setSessions(r.sessions ?? [])).catch(() => {})
    const close = (e: MouseEvent) => {
      const tgt = e.target as HTMLElement
      if (!tgt.closest('[data-session-picker]')) setPickerOpen(false)
    }
    setTimeout(() => window.addEventListener('mousedown', close), 0)
    return () => window.removeEventListener('mousedown', close)
  }, [pickerOpen])

  // Position + outside-click-close for the export options popover — same
  // pattern as the session picker effect above (compute in an effect, not
  // inline during render, since reading a ref mid-render can see stale layout).
  useEffect(() => {
    if (!exportOptsOpen) return
    const rect = exportBtnRef.current?.getBoundingClientRect()
    if (rect) setExportOptsPos({ left: rect.right, top: rect.bottom + 4 })
    const close = (e: MouseEvent) => {
      const tgt = e.target as HTMLElement
      if (!tgt.closest('[data-export-opts]')) setExportOptsOpen(false)
    }
    setTimeout(() => window.addEventListener('mousedown', close), 0)
    return () => window.removeEventListener('mousedown', close)
  }, [exportOptsOpen])

  // Keep the resolution default in sync with the current canvas ("Source")
  // until the user explicitly picks something else.
  useEffect(() => {
    if (edl?.canvas?.h) setExportHeight((h) => h || edl.canvas.h)
  }, [edl?.canvas?.h])

  const confirmExport = () => {
    setExportOptsOpen(false)
    void doExport({ height: exportHeight, crf: exportCrf })
  }

  const switchSession = async (newId: string) => {
    setPickerOpen(false)
    if (newId === sid) return
    useStore.getState().resetTransient()
    useStore.setState({ sessionId: newId, sessionName: newId })
    await refresh()
  }

  const newSession = async () => {
    setPickerOpen(false)
    const r = await api.createSession(`project ${new Date().toLocaleString()}`)
    useStore.setState({ sessionId: r.id, sessionName: r.name })
    await refresh()
  }

  const removeSession = async (id: string, e: React.MouseEvent) => {
    e.stopPropagation()  // don't trigger switchSession
    if (!window.confirm(`Delete project ${id}? This removes its media and history permanently.`)) return
    await api.deleteSession(id)
    const list = await api.listSessions()
    setSessions(list.sessions)
    // If we deleted the active session, switch to the newest remaining, or create one.
    if (id === sid) {
      const next = list.sessions[0]?.id ?? (await api.createSession()).id
      await switchSession(next)
    }
  }

  return (
    <header className="topbar">
      <h1>Video AI Editor</h1>
      <div data-session-picker style={{ position: 'relative' }}>
        <button
          ref={pickerBtnRef}
          className="pill"
          title="Switch project"
          onClick={() => setPickerOpen((o) => !o)}
          style={{ cursor: 'pointer', padding: '3px 10px', fontSize: 11 }}
        >
          {name} ▾
        </button>
        {pickerOpen && pickerPos && createPortal(
          <div
            data-session-picker
            style={{
              position: 'fixed',
              left: pickerPos.left,
              top: pickerPos.top,
              zIndex: 1000,
              background: 'var(--bg-2)', border: '1px solid var(--line)', borderRadius: 6,
              boxShadow: '0 8px 24px rgba(0,0,0,0.5)', minWidth: 280, maxHeight: 400,
              overflow: 'auto', padding: 4,
            }}
          >
            <div
              onClick={newSession}
              style={{ padding: '6px 10px', cursor: 'pointer', fontSize: 12, borderRadius: 3 }}
              onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--bg-3)')}
              onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
            >
              ＋ New project
            </div>
            <div
              onClick={() => { setPickerOpen(false); importRef.current?.click() }}
              style={{ padding: '6px 10px', cursor: 'pointer', fontSize: 12, borderRadius: 3,
                       borderBottom: '1px solid var(--line)' }}
              onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--bg-3)')}
              onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
            >
              📂 Open .vae…
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
                <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>
                  {s.name || s.id}
                </span>
                <span style={{ color: 'var(--text-dim)', fontSize: 10 }}>{s.id.slice(0, 10)}</span>
                <button
                  onClick={(e) => removeSession(s.id, e)}
                  title={`Delete project ${s.id}`}
                  style={{
                    background: 'transparent', border: 'none', color: 'var(--text-dim)',
                    cursor: 'pointer', fontSize: 12, padding: '0 2px', lineHeight: 1,
                  }}
                  onMouseEnter={(e) => (e.currentTarget.style.color = '#ff4d6d')}
                  onMouseLeave={(e) => (e.currentTarget.style.color = 'var(--text-dim)')}
                >
                  ×
                </button>
              </div>
            ))}
          </div>,
          document.body,
        )}
      </div>
      {pendingOps > 0 && (
        <span className="pill" title="An edit is being applied" style={{ color: 'var(--text-dim)' }}>
          ⋯ Applying
        </span>
      )}
      {edl && (
        <span className="pill">
          {edl.canvas.w}×{edl.canvas.h} · {edl.canvas.fps}fps · {edl.duration.toFixed(1)}s
        </span>
      )}
      <div className="grow" />
      {/* Scrollable middle section: aspect-ratio + platform-preset buttons.
          These can grow without bound (more presets, longer labels) — if this
          section overflows the window, IT scrolls internally, but the
          right-side cluster below (Save/Open/Export) never does. Previously
          every button here shared one flex row with Export at the tail end,
          so on a ~1280px window (a common 13" laptop size) Export could sit
          past the visible edge with no visual cue that scrolling the
          TOOLBAR ITSELF (not the page) would reveal it — issues 9/10. */}
      <div className="topbar-scroll">
        <button onClick={() => dispatch('undo')} title="Cmd+Z">Undo</button>
        <button onClick={() => dispatch('redo')} disabled={!redoAvailable} title="Cmd+Shift+Z">Redo</button>
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
      </div>
      {/* Pinned right-side cluster: never scrolls away, regardless of how
          much content is in .topbar-scroll above. Export is always the
          right-most, always-visible element. */}
      <div className="topbar-pinned">
        <button onClick={onSaveProject} disabled={saving || !edl?.duration} title="Save the project as a .vae bundle (EDL + media)">
          {saving ? 'Saving…' : '💾 Save'}
        </button>
        <button onClick={() => importRef.current?.click()} title="Open a saved .vae project">
          📂 Open
        </button>
        <input ref={importRef} type="file" accept=".vae,.zip" hidden
          onChange={(e) => { const f = e.target.files?.[0]; if (f) void onLoadProject(f) }} />
        {savedUrl && (
          <a href={savedUrl} download
            className={savedStale ? 'stale-dl' : ''}
            title={savedStale ? 'This .vae predates your latest edits' : 'Download saved project'}
            style={{ color: savedStale ? undefined : 'var(--good)', fontSize: 12 }}>
            ↓ .vae{savedStale ? ' (outdated)' : ''}
          </a>
        )}
        <div data-export-opts style={{ position: 'relative', display: 'inline-block' }}>
          <button
            ref={exportBtnRef}
            className="primary"
            onClick={() => setExportOptsOpen((o) => !o)}
            disabled={exporting || !edl?.duration}
          >
            {exporting
              ? `Exporting${exportStatus === 'queued' ? ' (queued)' : ''}… ${exportElapsed}s`
              : 'Export ▾'}
          </button>
          {exportOptsOpen && exportOptsPos && createPortal(
            <div
              data-export-opts
              style={{
                position: 'fixed',
                left: exportOptsPos.left,
                top: exportOptsPos.top,
                transform: 'translateX(-100%)',
                zIndex: 1000,
                background: 'var(--bg-2)', border: '1px solid var(--line)', borderRadius: 6,
                boxShadow: '0 8px 24px rgba(0,0,0,0.5)', minWidth: 220,
                padding: 10, display: 'flex', flexDirection: 'column', gap: 8,
              }}
            >
              <label style={{ fontSize: 11, color: 'var(--text-dim)', display: 'flex', flexDirection: 'column', gap: 3 }}>
                Resolution
                <select
                  value={exportHeight}
                  onChange={(e) => setExportHeight(Number(e.target.value))}
                  style={{ fontSize: 12, padding: '3px 4px' }}
                >
                  {edl?.canvas?.h && (
                    <option value={edl.canvas.h}>Source ({edl.canvas.w}×{edl.canvas.h})</option>
                  )}
                  <option value={2160}>2160p (4K)</option>
                  <option value={1440}>1440p (2K)</option>
                  <option value={1080}>1080p</option>
                  <option value={720}>720p</option>
                  <option value={480}>480p</option>
                </select>
              </label>
              <label style={{ fontSize: 11, color: 'var(--text-dim)', display: 'flex', flexDirection: 'column', gap: 3 }}>
                Quality
                <select
                  value={exportCrf}
                  onChange={(e) => setExportCrf(Number(e.target.value))}
                  style={{ fontSize: 12, padding: '3px 4px' }}
                >
                  <option value={18}>High</option>
                  <option value={23}>Medium</option>
                  <option value={28}>Small file</option>
                </select>
              </label>
              <button className="primary" onClick={confirmExport} style={{ fontSize: 12, marginTop: 2 }}>
                Export
              </button>
            </div>,
            document.body,
          )}
        </div>
        {exportUrl && !exporting && (
          <a href={exportUrl} download
            className={exportStale ? 'stale-dl' : ''}
            title={exportStale ? 'This render predates your latest edits — re-export for an up-to-date file' : 'Download exported MP4'}
            style={{ color: exportStale ? undefined : 'var(--good)', fontSize: 12 }}>
            ↓ MP4{exportStale ? ' (outdated)' : ''}
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
      </div>
    </header>
  )
}
