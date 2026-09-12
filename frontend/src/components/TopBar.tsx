import { useEffect, useRef, useState, type MouseEvent as ReactMouseEvent } from 'react'
import { createPortal } from 'react-dom'
import { useStore, errorMessage } from '../store'
import { api } from '../api'
import { toast } from '../toast'
import { openHelp } from './Help'
import { openShortcuts } from './ShortcutsSettings'
import { PhonePanel } from './PhonePanel'
import { TextTool } from './TextTool'
import { CaptionsButton } from './CaptionsButton'
import { SafeZoneToggle } from './SafeZones'
import { parseVersionInfo, VERSION_UNKNOWN, type VersionInfo } from '../lib/versionInfo'
import { claimClickForNativeSave, projectFilename } from '../lib/nativeSave'
import { isSavedProjectStale, savedProject, visibleSavedProject, type SavedProject } from '../lib/savedProject'

interface SessionRow { id: string; name: string }

export function TopBar() {
  const name = useStore((s) => s.sessionName)
  const dispatch = useStore((s) => s.dispatch)
  const pendingOps = useStore((s) => s.pendingOps)
  const exporting = useStore((s) => s.exporting)
  const exportUrl = useStore((s) => s.exportUrl)
  const exportGen = useStore((s) => s.exportGen)
  const opsLen = useStore((s) => s.ops.length)
  const exportStatus = useStore((s) => s.exportStatus)
  const exportError = useStore((s) => s.exportError)
  const clearExportError = useStore((s) => s.clearExportError)
  const doExport = useStore((s) => s.doExport)
  const downloadExport = useStore((s) => s.downloadExport)
  const [exportElapsed, setExportElapsed] = useState(0)
  const edl = useStore((s) => s.edl)
  const sid = useStore((s) => s.sessionId)
  const refresh = useStore((s) => s.refresh)
  const [saving, setSaving] = useState(false)
  // url + sid + generation as ONE record — see lib/savedProject for why the
  // session id belongs in it. `saved` is only shown while it belongs to the
  // session on screen, so the link can never point at one project while the
  // native bridge is handed another's id.
  const [saved, setSaved] = useState<SavedProject | null>(null)
  const savedHere = visibleSavedProject(saved, sid)
  // A download link is "outdated" once history advances past the generation it
  // was made at. We keep the link (you can still grab the last render) but mark
  // it so nobody ships a stale file by mistake.
  const exportStale = !!exportUrl && opsLen > exportGen
  const savedStale = !!savedHere && isSavedProjectStale(savedHere, opsLen)
  const importRef = useRef<HTMLInputElement>(null)
  const [sessions, setSessions] = useState<SessionRow[]>([])
  const [pickerOpen, setPickerOpen] = useState(false)
  // One object from the single GET /api/version below, replaced wholesale (never
  // mutated): the semantic version, the git short-sha / baked BUILD_ID shown next
  // to it so a bug report identifies the exact bits (which "v0.3.7" did not), and
  // `phonePairing` — whether this build has the iPhone affordance at all.
  // Starts at VERSION_UNKNOWN, whose phonePairing is false, so the phone button
  // cannot flash in and out while the request is in flight.
  const [appInfo, setAppInfo] = useState<VersionInfo>(VERSION_UNKNOWN)
  // The phone-pairing panel. Closed by default and mounted only while open:
  // it shows a live credential, and a panel that is merely hidden is one
  // stylesheet mistake away from being a code left on screen.
  const [phoneOpen, setPhoneOpen] = useState(false)
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
  // 0 = "not yet explicitly chosen" -> falls back to the current canvas height
  // ("Source") below. Kept as a sentinel rather than initialized straight to
  // edl.canvas.h and re-synced from a useEffect, so there's no setState call
  // inside an effect body (react-hooks/set-state-in-effect) and the "Source"
  // default keeps tracking canvas changes (e.g. aspect-ratio switches) until
  // the user actually picks a resolution from the <select>.
  const [exportHeightChoice, setExportHeightChoice] = useState<number>(0)
  const exportHeight = exportHeightChoice || edl?.canvas?.h || 1080
  const [exportCrf, setExportCrf] = useState<number>(18)
  const [exportContainer, setExportContainer] = useState<'mp4' | 'mov'>('mp4')
  const exportBtnRef = useRef<HTMLButtonElement>(null)
  const [exportOptsPos, setExportOptsPos] = useState<{ left: number; top: number } | null>(null)

  useEffect(() => {
    // `res.ok` matters: without it a 4xx/5xx body goes to .json(), throws, and
    // the badge silently vanishes with no clue why. Log instead of swallowing.
    fetch('/api/version')
      .then((r) => {
        if (!r.ok) throw new Error(`/api/version -> HTTP ${r.status}`)
        return r.json()
      })
      // This ONE request answers two questions — the version badge and whether
      // the phone affordance exists (`phone_pairing`). Deliberately not a second
      // probe against /api/pair/*: that route is a 404 in the shipped build, and
      // deciding UI from a 404 makes the button appear and then disappear.
      .then((d) => setAppInfo(parseVersionInfo(d)))
      .catch((e) => console.warn('[TopBar] version fetch failed:', e))
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
    setSaved(null)
    try {
      const r = await api.saveProject(sid)
      setSaved(savedProject(sid, r.url, useStore.getState().ops.length))
    } catch (e) {
      // Save used to fail in TOTAL silence: no toast, no console line, and the
      // "Saved" link simply never appeared — indistinguishable from a slow save.
      // Losing a project export without being told is the worst kind of quiet.
      toast.error(`Couldn't save the project: ${errorMessage(e)}`)
    } finally {
      setSaving(false)
    }
  }

  // The "Saved" link is an `<a download>` for the browser, but the packaged
  // WKWebView ignores the `download` attribute and navigates instead (see the
  // export button's comment below — the same trap that moved exports onto the
  // native bridge). Navigating to a .vae, which the backend serves as
  // text/plain;attachment, either did nothing or saved "<sid>.vae.txt" — the
  // file Open then refused with 415. The bridge copies the very file the link
  // points at: save_project writes exports/<sid>.vae, and save_export reads
  // exports/<filename>. In a browser the click is left alone.
  //
  // `link.sid`, never the live `sid`: the bridge must be asked for the session
  // the file was actually written from. Handing it the current session id was
  // how a link left over from another project turned into a silent no-op (the
  // file does not exist there, and a missing source reads back as "cancelled",
  // which this handler deliberately does not toast).
  const onSavedLinkClick = (link: SavedProject) => (e: ReactMouseEvent<HTMLAnchorElement>) => {
    const pending = claimClickForNativeSave(e, link.sid, projectFilename(link.sid))
    if (!pending) return
    void pending.then((outcome) => {
      if (outcome.kind === 'saved') toast.success(`Saved to ${outcome.path}`)
      // Cancelled: nothing was written, so no toast — a success would lie and
      // an error would nag about a choice the user just made.
      else if (outcome.kind === 'failed') toast.error(`Couldn't save the project file: ${errorMessage(outcome.error)}`)
    })
  }

  const onLoadProject = async (file: File) => {
    try {
      const r = await api.loadProject(file)
      // Switch to the new session and refresh
      useStore.setState({ sessionId: r.id, sessionName: r.id })
      await refresh()
    } catch (e) {
      toast.error(`Couldn't open that .vae project: ${errorMessage(e)}`)
    }
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
    // A swallowed failure here left the picker showing "Loading…" forever.
    api.listSessions()
      .then((r) => setSessions(r.sessions ?? []))
      .catch((e) => {
        setSessions([])
        toast.error(`Couldn't list sessions: ${errorMessage(e)}`)
      })
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

  const confirmExport = () => {
    setExportOptsOpen(false)
    void doExport({ height: exportHeight, crf: exportCrf, container: exportContainer })
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
        {/* Undo/Redo MOVED to the timeline toolbar (Timeline.tsx). They were
            in `.topbar-scroll`, which scrolls horizontally once enough presets
            are added — so the app's two most-used buttons could end up
            off-screen with no cue that the toolbar itself scrolls. */}
        {(['9:16', '16:9', '1:1', '4:5'] as const).map((r) => (
          <button
            key={r}
            title={`Set canvas aspect ratio to ${r} — overlays reposition to fit`}
            onClick={() => dispatch('set_aspect_ratio', { ratio: r })}
          >{r}</button>
        ))}
        <SafeZoneToggle />
        <span style={{ width: 1, height: 20, background: 'var(--line)', margin: '0 4px' }} />
        <TextTool />
        <CaptionsButton />
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
        {/* The iPhone-pairing affordance, rendered ONLY when this build reports
            `phone_pairing: true` on /api/version.

            WHY it is gated rather than deleted: the desktop editor ships as a
            normal standalone editor, so the local-network pairing feature is
            TEMPORARILY off behind one reversible flag — `VAE_PHONE_PAIRING` /
            `PHONE_PAIRING_ENABLED` in api/pairing.py. With it off there must be
            no button, no panel and no phone wording anywhere in the UI; with it
            on, this is exactly the old behaviour. PhonePanel.tsx and
            phonePanel.css stay in the tree for the release that flips it back.

            Both the button and the panel live in .topbar-scroll rather than
            .topbar-pinned: the pinned cluster's invariant is that Export is the
            right-most, always-visible control, and pairing a phone is a
            once-a-month action that has no business competing with it. The
            button is the only element in this fragment that occupies layout —
            PhonePanel portals to document.body — so when the flag is off the
            toolbar simply closes up, with no gap and no stray separator (the
            nearest separators sit further left, before TextTool). */}
        {appInfo.phonePairing && (
          <>
            <button
              onClick={() => setPhoneOpen(true)}
              title="Connect an iPhone to this Mac — the phone edits, this Mac does the work"
              style={{ fontSize: 11 }}
            >📱 Phone</button>
            {phoneOpen && <PhonePanel onClose={() => setPhoneOpen(false)} />}
          </>
        )}
        {appInfo.version && (
          <span title={appInfo.build ? `App version ${appInfo.version} · build ${appInfo.build}` : 'App version'}
                style={{ fontSize: 10, color: 'var(--text-dim, #888)', opacity: 0.7 }}>
            v{appInfo.version}{appInfo.build ? ` · ${appInfo.build}` : ''}
          </span>
        )}
      </div>
      {/* Pinned right-side cluster: never scrolls away, regardless of how
          much content is in .topbar-scroll above. Export is always the
          right-most, always-visible element. */}
      <div className="topbar-pinned">
        <button
          onClick={onSaveProject}
          disabled={saving || !edl?.duration}
          title={!edl?.duration
            ? 'Nothing to save yet — add a video to the timeline first'
            : saving
              ? 'Saving the project file…'
              : 'Save an editable project file (.vae) you can reopen later'}
        >
          {saving ? 'Saving…' : '💾 Save'}
        </button>
        <button onClick={() => importRef.current?.click()} title="Open a saved .vae project">
          📂 Open
        </button>
        <input ref={importRef} type="file" accept=".vae,.zip" hidden
          onChange={(e) => { const f = e.target.files?.[0]; if (f) void onLoadProject(f) }} />
        {savedHere && (
          <a href={savedHere.url} download onClick={onSavedLinkClick(savedHere)}
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
            title={!edl?.duration
              ? 'Nothing to export yet — add a video to the timeline first'
              : exporting
                ? 'Export is already running — the button shows elapsed time'
                : 'Render the final flattened video (MP4/MOV) to share'}
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
                {/* Bound to the CHOICE, with 0 as the "source" sentinel — not to
                    the resolved height. Giving the Source option the canvas
                    height emitted two options with the same value whenever the
                    canvas matched a preset (a 1080-tall project had 1080 twice),
                    and a <select> resolves a duplicate value to the FIRST match,
                    so picking "1080p" silently snapped back to "Source". */}
                <select
                  value={exportHeightChoice}
                  onChange={(e) => setExportHeightChoice(Number(e.target.value))}
                  style={{ fontSize: 12, padding: '3px 4px' }}
                >
                  {edl?.canvas?.h && (
                    <option value={0}>Source ({edl.canvas.w}×{edl.canvas.h})</option>
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
              <label style={{ fontSize: 11, color: 'var(--text-dim)', display: 'flex', flexDirection: 'column', gap: 3 }}>
                Format
                <select
                  value={exportContainer}
                  onChange={(e) => setExportContainer(e.target.value as 'mp4' | 'mov')}
                  style={{ fontSize: 12, padding: '3px 4px' }}
                >
                  <option value="mp4">MP4</option>
                  <option value="mov">MOV</option>
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
          // Deliberately a <button>, NOT an <a href={exportUrl} download>. In the
          // packaged app (pywebview WKWebView/WebView2) the `download` attribute is
          // ignored, so clicking an anchor NAVIGATES the webview to the inline
          // .mp4 — which macOS opens as a borderless native fullscreen player with
          // no Escape/back affordance, trapping the user (force-quit only). Routing
          // through downloadExport() → the native save bridge avoids any navigation
          // and pops a real Save-As dialog instead.
          <button
            type="button"
            onClick={() => downloadExport()}
            className={exportStale ? 'stale-dl' : ''}
            title={exportStale ? 'This render predates your latest edits — re-export for an up-to-date file' : `Save exported ${exportUrl.split('.').pop()?.toUpperCase()}`}
            style={{ background: 'none', border: 'none', padding: 0, cursor: 'pointer', color: exportStale ? undefined : 'var(--good)', fontSize: 12 }}>
            ↓ {exportUrl.split('.').pop()?.toUpperCase()}{exportStale ? ' (outdated)' : ''}
          </button>
        )}
        {exportError && (
          // Show the REASON, not just "failed". The backend now maps ffmpeg
          // stderr through _render_failure_message, so this is a sentence a
          // user can act on ("…an audio-only file on the video track. Move
          // that clip to the Music lane") — it used to be reachable only by
          // hovering for a 2000-char raw ffmpeg dump. Strip the RuntimeError:
          // prefix jobs.py adds, and cap the width so a long tail (an
          // unmapped ffmpeg error) can't blow out the toolbar.
          <span
            style={{
              color: 'var(--accent)', fontSize: 12, cursor: 'pointer',
              maxWidth: 340, overflow: 'hidden', textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
            title={`${exportError} (click to dismiss)`}
            onClick={() => clearExportError()}
          >
            ⚠ {exportError.replace(/^\w*Error:\s*/, '')} ✕
          </span>
        )}
      </div>
    </header>
  )
}
