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
import { RatioMenu } from './RatioMenu'
import { TopBarMore } from './TopBarMore'
import { parseVersionInfo, VERSION_UNKNOWN, type VersionInfo } from '../lib/versionInfo'
import { claimClickForNativeSave } from '../lib/nativeSave'
import { isSavedProjectStale, savedProject, visibleSavedProject, type SavedProject } from '../lib/savedProject'
import { exportKind, exportLinkView } from '../lib/exportLink'
import { canvasFacts } from '../lib/frameStep'
import { ExportButton } from './ExportDialog'
import { useMenuA11y } from '../lib/useMenuA11y'
import { editedLabel, projectLabel } from '../lib/projectName'
import { ConfirmDialog } from './ConfirmDialog'
import { Icon } from './Icon'
import { ProjectPoster } from './ProjectPoster'
import { openSettings } from '../lib/settingsOpen'
import { chordLabel, useKeymapStore } from '../keymap/engine'

interface SessionRow { id: string; name: string; modified_at?: number; poster?: string | null }

export function TopBar() {
  const name = useStore((s) => s.sessionName)
  const pendingOps = useStore((s) => s.pendingOps)
  const exporting = useStore((s) => s.exporting)
  const exportLinks = useStore((s) => s.exportLinks)
  const edlHash = useStore((s) => s.edlHash)
  const opsLen = useStore((s) => s.ops.length)
  const exportError = useStore((s) => s.exportError)
  const clearExportError = useStore((s) => s.clearExportError)
  const downloadExport = useStore((s) => s.downloadExport)
  const edl = useStore((s) => s.edl)
  const sid = useStore((s) => s.sessionId)
  const [saving, setSaving] = useState(false)
  // url + sid + generation as ONE record — see lib/savedProject for why the
  // session id belongs in it. `saved` is only shown while it belongs to the
  // session on screen, so the link can never point at one project while the
  // native bridge is handed another's id.
  const [saved, setSaved] = useState<SavedProject | null>(null)
  const savedHere = visibleSavedProject(saved, sid)
  // The export link belongs to the project it was rendered from and is shown
  // only there (lib/exportLink). It is "outdated" whenever the timeline on
  // screen is not the one it rendered — by EDL hash, so Undo past the export
  // marks it and Redo back un-marks it. We keep the link (you can still grab
  // the last render) but mark it so nobody ships a stale file by mistake.
  const exportView = exportLinkView(exportLinks, sid, edlHash)
  const savedStale = !!savedHere && isSavedProjectStale(savedHere, opsLen)
  const importRef = useRef<HTMLInputElement>(null)
  const [sessions, setSessions] = useState<SessionRow[]>([])
  const [sessionsListed, setSessionsListed] = useState(false)
  const [pickerOpen, setPickerOpen] = useState(false)
  // QA-099: inline rename of the open project (double-click the chip, or
  // "Rename…" in the picker), and the in-app delete confirm.
  const [renaming, setRenaming] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState<SessionRow | null>(null)
  const renameCommitted = useRef(false)
  const shownName = projectLabel(name, sid)
  // The live binding (rebindable; ⌘, / Ctrl+, by default) for the gear's tooltip.
  const settingsChord = useKeymapStore((s) => s.effectiveMap)().openSettings?.[0] ?? null
  // One object from the single GET /api/version below, replaced wholesale (never
  // mutated): the semantic version, the git short-sha / baked BUILD_ID shown next
  // to it so a bug report identifies the exact bits (which "v0.3.7" did not), and
  // `phonePairing` — whether this build has the iPhone affordance at all.
  // Starts at VERSION_UNKNOWN, whose phonePairing is false, so the phone button
  // cannot flash in and out while the request is in flight.
  const [appInfo, setAppInfo] = useState<VersionInfo>(VERSION_UNKNOWN)
  const versionText = appInfo.version ? `v${appInfo.version}${appInfo.build ? ` · ${appInfo.build}` : ''}` : null
  // The phone-pairing panel. Closed by default and mounted only while open:
  // it shows a live credential, and a panel that is merely hidden is one
  // stylesheet mistake away from being a code left on screen.
  const [phoneOpen, setPhoneOpen] = useState(false)
  // The session-picker dropdown is rendered via a portal to document.body
  // (positioned from this ref's rect) instead of as a normal absolutely-
  // positioned child of .topbar. .topbar clips overflow on both axes to keep
  // the toolbar on one line (see .topbar-tools/.topbar-pinned), so a child
  // positioned `top:100%` — below the 44px toolbar row — was always cut off
  // by that same clip (issue 11, "dropdown is half-cut when clicked").
  const pickerBtnRef = useRef<HTMLButtonElement>(null)
  const [pickerPos, setPickerPos] = useState<{ left: number; top: number } | null>(null)

  // Export ▾ opens components/ExportDialog (QA-100): name, resolution as real
  // WxH, frame rate, quality, format, loudness, audio and a size estimate.
  // Keyboard: both popovers take focus when they open, Escape closes them
  // and focus goes back to the trigger (lib/useMenuA11y, QA-102). They used
  // to close only on an outside mousedown.
  const pickerMenuRef = useRef<HTMLDivElement>(null)
  const pickerA11y = useMenuA11y({
    // Ready once the project list has arrived: the checked row (the project
    // on screen) is where focus lands, and it is not there while "Loading…".
    open: pickerOpen, ready: !!pickerPos && sessionsListed, mode: 'menu',
    menuRef: pickerMenuRef, triggerRef: pickerBtnRef, onClose: () => setPickerOpen(false),
  })

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


  const onSaveProject = async () => {
    if (!sid) return
    setSaving(true)
    setSaved(null)
    try {
      const r = await api.saveProject(sid)
      setSaved(savedProject(sid, r.url, useStore.getState().ops.length, r.filename))
      // QA-096: saved, but without media whose file is gone — never silently.
      if (r.warning) toast.error(r.warning, 9000)
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
    // The file save_project named after the project (QA-098), not `<sid>.vae`.
    const pending = claimClickForNativeSave(e, link.sid, link.filename)
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
      // Switch to the new session (loaded first, then swapped in atomically)
      await useStore.getState().openSession(r.id)
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
    setSessionsListed(false)
    api.listSessions()
      .then((r) => setSessions(r.sessions ?? []))
      .catch((e) => {
        setSessions([])
        toast.error(`Couldn't list sessions: ${errorMessage(e)}`)
      })
      .finally(() => setSessionsListed(true))
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
    await useStore.getState().openSession(newId)
  }

  const newSession = async () => {
    setPickerOpen(false)
    // No client-made name: the server names it "Untitled project N" (QA-099)
    // — it used to be "project 9/25/2026, 7:03:47 PM".
    try {
      const r = await api.createSession()
      await useStore.getState().openSession(r.id)
    } catch (e) {
      toast.error(`Couldn't create a project: ${errorMessage(e)}`)
    }
  }

  const removeSession = (row: SessionRow, e: React.MouseEvent) => {
    e.stopPropagation()  // don't trigger switchSession
    setPickerOpen(false)
    setConfirmDelete(row)
  }

  const reallyRemoveSession = async (row: SessionRow) => {
    setConfirmDelete(null)
    try {
      await api.deleteSession(row.id)
      const list = await api.listSessions()
      setSessions(list.sessions)
      toast.info(`Deleted “${projectLabel(row.name, row.id)}”.`)
      // If we deleted the active session, switch to the newest remaining, or create one.
      if (row.id === sid) {
        const next = list.sessions[0]?.id ?? (await api.createSession()).id
        await switchSession(next)
      }
    } catch (e) {
      toast.error(`Couldn't delete the project: ${errorMessage(e)}`)
    }
  }

  const startRename = () => {
    setPickerOpen(false)
    renameCommitted.current = false
    setRenaming(true)
  }
  const finishRename = async (value: string | null) => {
    if (renameCommitted.current) return
    renameCommitted.current = true
    setRenaming(false)
    // Back onto the chip, not <body> (keyboard users keep their place).
    requestAnimationFrame(() => pickerBtnRef.current?.focus())
    if (value === null) return
    const next = value.replace(/\s+/g, ' ').trim()
    if (!next || next === name) return
    await useStore.getState().renameSession(next)
  }

  return (
    <header className="topbar">
      <h1 className="topbar-brand">Video AI Editor</h1>
      <div data-session-picker style={{ position: 'relative' }}>
        {renaming ? (
          <input
            className="topbar-session-rename"
            aria-label="Project name"
            defaultValue={shownName}
            maxLength={120}
            autoFocus
            data-keymap-ignore
            onFocus={(e) => e.currentTarget.select()}
            onKeyDown={(e) => {
              if (e.key === 'Enter') { e.preventDefault(); void finishRename(e.currentTarget.value) }
              if (e.key === 'Escape') { e.preventDefault(); void finishRename(null) }
            }}
            onBlur={(e) => { void finishRename(e.currentTarget.value) }}
          />
        ) : (
        <button
          ref={pickerBtnRef}
          className="pill topbar-session"
          title={`${shownName} — switch project (double-click to rename)`}
          aria-haspopup="menu"
          aria-expanded={pickerOpen}
          onClick={() => setPickerOpen((o) => !o)}
          onDoubleClick={(e) => { e.preventDefault(); startRename() }}
          style={{ cursor: 'pointer', padding: '3px 10px', fontSize: 11 }}
        >
          <span className="topbar-session-name">{shownName}</span><Icon name="chevronDown" />
        </button>
        )}
        {pickerOpen && pickerPos && createPortal(
          <div
            ref={pickerMenuRef}
            data-session-picker
            data-keymap-ignore
            role="menu"
            aria-label="Projects"
            onKeyDown={pickerA11y.onKeyDown}
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
            {/* Real <button role="menuitem">s (QA-102): these were click-only
                <div>s with tabIndex -1, so the picker had no keyboard path. */}
            <button type="button" role="menuitem" className="menu-item" onClick={newSession}>
              <Icon name="plus" /> New project
            </button>
            {/* Every item carries its icon, so the labels line up (wave C
                review); the file extension is for the tooltip, not the label. */}
            <button type="button" role="menuitem" className="menu-item"
              title="Open a Video AI Editor project file (.vae)"
              onClick={() => { pickerA11y.close(false); importRef.current?.click() }}>
              <Icon name="open" /> Open project file…
            </button>
            <button type="button" role="menuitem" className="menu-item" onClick={startRename}>
              <Icon name="rename" /> Rename this project…
            </button>
            <div role="separator" style={{ height: 1, background: 'var(--line)', margin: '4px 0' }} />
            {sessions.length === 0 && (
              <div role="none" style={{ padding: '6px 10px', fontSize: 11, color: 'var(--text-dim)' }}>
                Loading…
              </div>
            )}
            {sessions.map((s) => (
              // A row is two menu items: open the project (checked = the one
              // on screen, where focus lands on open) and delete it. Nesting
              // the delete button inside a clickable row made it unreachable.
              <div key={s.id} role="none" className="menu-row">
                <button
                  type="button"
                  role="menuitemradio"
                  aria-checked={s.id === sid}
                  className="menu-item"
                  onClick={() => switchSession(s.id)}
                  title={projectLabel(s.name, s.id)}
                  style={{ flex: 1, minWidth: 0 }}
                >
                  <span className="menu-check" aria-hidden="true">{s.id === sid && <Icon name="check" />}</span>
                  {/* QA-099-THUMBS: a frame of the project, cached per project. */}
                  <ProjectPoster key={s.poster ?? 'none'} src={s.poster} />
                  <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>
                    {projectLabel(s.name, s.id)}
                  </span>
                  {/* When, not the raw id (QA-099): two copies of one project
                      are told apart by when they were last edited. */}
                  <span className="menu-item-meta">{editedLabel(s.modified_at)}</span>
                </button>
                <button
                  type="button"
                  role="menuitem"
                  className="menu-item-icon"
                  onClick={(e) => removeSession(s, e)}
                  title={`Delete “${projectLabel(s.name, s.id)}”`}
                  aria-label={`Delete project ${projectLabel(s.name, s.id)}`}
                >
                  <Icon name="close" />
                </button>
              </div>
            ))}
          </div>,
          document.body,
        )}
        {confirmDelete && (
          <ConfirmDialog
            title={`Delete “${projectLabel(confirmDelete.name, confirmDelete.id)}”?`}
            body={`This removes the project${confirmDelete.modified_at ? ` (${editedLabel(confirmDelete.modified_at)})` : ''}, its imported media and its edit history from this Mac. It can’t be undone.`}
            confirmLabel="Delete project"
            danger
            onConfirm={() => void reallyRemoveSession(confirmDelete)}
            onCancel={() => setConfirmDelete(null)}
          />
        )}
      </div>
      {pendingOps > 0 && (
        <span className="pill" title="An edit is being applied" style={{ color: 'var(--text-dim)' }}>
          <Icon name="more" /> Applying
        </span>
      )}
      {edl && (
        // Hidden below 1280 px (styles.css) — the same facts head the Ratio menu.
        <span className="pill topbar-canvas">
          {canvasFacts(edl.canvas, edl.duration)}
        </span>
      )}
      <div className="grow" />
      {/* The tools: every core action is VISIBLE at every supported width
          (1024–1920; QA-012). This used to be `.topbar-scroll`, an overflow-x
          strip with a 0 px scrollbar and no fade: at 1440 wide TikTok, the
          IG presets, Help and Shortcuts sat past its edge, and at 1024 so did
          Text and Captions — reachable only by a sideways scroll nobody could
          see. Now the four aspect buttons and five platform presets are ONE
          "Ratio ▾" menu that checks the canvas' current choice, the safe-zone
          picker and the version badge move into "⋯" below 1440 px, and the
          brand / canvas pills give way first (styles.css). Nothing here
          scrolls, and .topbar-pinned (Save/Open/Export) still never moves. */}
      <div className="topbar-tools">
        {/* Undo/Redo MOVED to the timeline toolbar (Timeline.tsx): the app's
            two most-used buttons must never be the ones a narrow bar hides. */}
        <RatioMenu />
        <span className="topbar-wide"><SafeZoneToggle /></span>
        <span style={{ width: 1, height: 20, background: 'var(--line)', margin: '0 2px' }} />
        <TextTool />
        <CaptionsButton />
        <span style={{ width: 1, height: 20, background: 'var(--line)', margin: '0 2px' }} />
        {/* Glyph buttons carry a NAME (QA-102): screen readers announced "?" and "⌨". */}
        <button className="icon-btn" onClick={openHelp} title="Keyboard shortcuts (?)" aria-label="Keyboard shortcuts" aria-keyshortcuts="?">
          <Icon name="help" />
        </button>
        <button className="icon-btn" onClick={openShortcuts} title="Customize keyboard shortcuts (CapCut / Premiere / Final Cut)" aria-label="Customize keyboard shortcuts">
          <Icon name="keyboard" />
        </button>
        {/* Settings (QA-063-SETTINGS): the Anthropic key, brains, models, render cache. */}
        <button className="icon-btn" onClick={openSettings}
                title={`Settings${settingsChord ? ` (${chordLabel(settingsChord)})` : ''}`} aria-label="Settings">
          <Icon name="settings" />
        </button>
        {/* The iPhone-pairing affordance, rendered ONLY when this build reports
            `phone_pairing: true` on /api/version.

            WHY it is gated rather than deleted: the desktop editor ships as a
            normal standalone editor, so the local-network pairing feature is
            TEMPORARILY off behind one reversible flag — `VAE_PHONE_PAIRING` /
            `PHONE_PAIRING_ENABLED` in api/pairing.py. With it off there must be
            no button, no panel and no phone wording anywhere in the UI; with it
            on, this is exactly the old behaviour. PhonePanel.tsx and
            phonePanel.css stay in the tree for the release that flips it back.

            Both the button and the panel live in .topbar-tools rather than
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
            ><Icon name="phone" /> Phone</button>
            {phoneOpen && <PhonePanel onClose={() => setPhoneOpen(false)} />}
          </>
        )}
        <TopBarMore version={versionText} />
        {appInfo.version && (
          <span className="topbar-wide" title={appInfo.build ? `App version ${appInfo.version} · build ${appInfo.build}` : 'App version'}
                style={{ fontSize: 10, color: 'var(--text-dim)' }}>
            {/* The version only; the build id is in its title and in "⋯" (QA-101). */}
            {`v${appInfo.version}`}
          </span>
        )}
      </div>
      {/* Pinned right-side cluster: never moves, regardless of how much is in
          .topbar-tools above. Export is always the right-most, always-visible
          element. */}
      <div className="topbar-pinned">
        <button
          onClick={onSaveProject}
          disabled={saving || !edl?.duration}
          aria-label={saving ? 'Saving…' : 'Save'}
          title={!edl?.duration
            ? 'Nothing to save yet — add a video to the timeline first'
            : saving
              ? 'Saving the project file…'
              : 'Save an editable project file (.vae) you can reopen later'}
        >
          {/* Below 1100 px the words give way and the icon stays (styles.css
              .topbar-btn-label), so the tools keep their room (QA-012). */}
          {saving ? 'Saving…' : <><Icon name="save" /><span className="topbar-btn-label"> Save</span></>}
        </button>
        <button onClick={() => importRef.current?.click()} title="Open a saved .vae project" aria-label="Open">
          <Icon name="open" /><span className="topbar-btn-label"> Open</span>
        </button>
        <input ref={importRef} type="file" accept=".vae,.zip" hidden
          onChange={(e) => { const f = e.target.files?.[0]; if (f) void onLoadProject(f) }} />
        {savedHere && (
          <a href={savedHere.url} download={savedHere.filename} onClick={onSavedLinkClick(savedHere)}
            className={savedStale ? 'stale-dl' : ''}
            title={savedStale ? 'This .vae predates your latest edits' : 'Download saved project'}
            style={{ color: savedStale ? undefined : 'var(--good)', fontSize: 12 }}>
            <Icon name="download" /> .vae{savedStale ? ' (outdated)' : ''}
          </a>
        )}
        {/* The export's result — its download link, or why it failed — sits
            BEFORE the Export button, so Export stays the cluster's right-most
            control at every width (QA-012); it used to trail it. */}
        {exportView && !exporting && (
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
            className={exportView.stale ? 'stale-dl' : ''}
            title={exportView.stale ? 'This render is not the timeline you have now — re-export for an up-to-date file' : `Save exported ${exportKind(exportView.link)}`}
            style={{ background: 'none', border: 'none', padding: 0, cursor: 'pointer', color: exportView.stale ? undefined : 'var(--good)', fontSize: 12 }}>
            <Icon name="download" /> {exportView.label}
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
          <button
            // Width capped per breakpoint in styles.css (.topbar-export-error):
            // at 1024 px a 340 px chip pushed Help and Shortcuts out of the
            // bar (QA-012). The title has the whole message. A <button>, not a
            // clickable <span>, so the keyboard can dismiss it too (QA-102).
            type="button"
            className="topbar-export-error"
            style={{
              color: 'var(--accent)', fontSize: 12, cursor: 'pointer',
              overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
              background: 'none', border: 'none', padding: 0,
            }}
            title={`${exportError} (click to dismiss)`}
            aria-label={`Export failed: ${exportError.replace(/^\w*Error:\s*/, '')}. Dismiss`}
            onClick={() => clearExportError()}
          >
            <Icon name="warning" /> {exportError.replace(/^\w*Error:\s*/, '')} <Icon name="close" />
          </button>
        )}
        {/* The right-most pinned control (QA-012): Export ▾ → the dialog. */}
        <ExportButton />
      </div>
    </header>
  )
}
