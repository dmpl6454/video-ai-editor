import { useEffect, useRef, useState, type MouseEvent as ReactMouseEvent } from 'react'
import { createPortal } from 'react-dom'
import { useStore, errorMessage } from '../store'
import { api } from '../api'
import { toast } from '../toast'
import { ActivityChip } from './topbar/ActivityChip'
import { densityClass, useTopBarFit } from './topbar/useTopBarFit'
import { RatioMenu } from './RatioMenu'
import { claimClickForNativeSave } from '../lib/nativeSave'
import { isSavedProjectStale, savedProject, visibleSavedProject, type SavedProject } from '../lib/savedProject'
import { exportKind, exportLinkView } from '../lib/exportLink'
import { useActivityStore } from '../lib/activityStore'
import { ExportButton } from './ExportDialog'
import { useMenuA11y } from '../lib/useMenuA11y'
import { editedLabel, projectLabel } from '../lib/projectName'
import { ConfirmDialog } from './ConfirmDialog'
import { Icon } from './Icon'
import { ProjectPoster } from './ProjectPoster'

// The top bar (docs/design/LEFT_RAIL_SPEC.md §0, §1.4, R3): three jobs in a
// `minmax(0,1fr) auto minmax(max-content,1fr)` grid —
//   left    which project I'm in and what is running: the brand mark over the
//           rail, the wordmark (the page's h1), the project chip, "Applying",
//           the activity chip (recording Stop, captions Cancel);
//   centre  what canvas I'm making: the Ratio menu (aspect, platform presets,
//           the safe-zone overlay), exactly centred while the right fits;
//   right   getting it out: Save · Open · the .vae / MP4 links · the export
//           error · Export, always the right-most control (QA-012).
// Everything that ADDS content lives in the left tool rail; Help, Customize
// shortcuts and Settings sit at the rail's foot (ToolRail.tsx RailFoot); the
// iPhone affordance is the Media panel's header action (ToolPanel.tsx). There
// is no "⋯" menu, no separator and no flex-shrink budget any more: when the
// window is narrow the bar steps up a DENSITY (useTopBarFit) that hides words,
// never controls, and only the project name truncates.

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
  const barRef = useRef<HTMLElement>(null)
  const leftRef = useRef<HTMLDivElement>(null)
  // The activity chip's width inputs (lib/activityStore), for the fit key.
  const recordingOn = useActivityStore((s) => !!s.recording)
  const captionsKey = useActivityStore((s) => (s.captions
    ? `${s.captions.cancelling ? 'stop' : 'run'}:${s.captions.progress != null}:${s.captions.etaS != null}`
    : ''))
  const ratioKey = edl ? `${edl.canvas.w}x${edl.canvas.h}@${edl.canvas.fps}` : ''
  const [sessions, setSessions] = useState<SessionRow[]>([])
  const [sessionsListed, setSessionsListed] = useState(false)
  const [pickerOpen, setPickerOpen] = useState(false)
  // QA-099: inline rename of the open project (double-click the chip, or
  // "Rename…" in the picker), and the in-app delete confirm.
  const [renaming, setRenaming] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState<SessionRow | null>(null)
  const renameCommitted = useRef(false)
  const shownName = projectLabel(name, sid)
  // The session-picker dropdown is rendered via a portal to document.body
  // (positioned from this ref's rect) instead of as a normal absolutely-
  // positioned child of .topbar. .topbar clips overflow on both axes to keep
  // the toolbar on one line (its left group clips too, see useTopBarFit), so a child
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

  // What can change the bar's width, as one key: a change re-fits the density
  // before paint (useTopBarFit). Ticking widths (the recording clock, the
  // Export button's elapsed seconds) are caught by the hook's ResizeObserver.
  const fitKey = [
    shownName, renaming, saving, pendingOps > 0, recordingOn, captionsKey, savedHere?.url ?? '', savedStale,
    exportView && !exporting ? exportView.label : '', exporting, exportError ?? '', ratioKey,
  ].join('|')
  const density = useTopBarFit(barRef, leftRef, fitKey)
  const densityCls = densityClass(density)
  const errorText = exportError ? exportError.replace(/^\w*Error:\s*/, '') : ''

  return (
    <header ref={barRef} className={densityCls ? `topbar ${densityCls}` : 'topbar'} aria-label="Project" data-density={density}>
      <div ref={leftRef} className="tb-left">
      {/* The mark sits exactly over the rail, so the rail reads as one column
          from top to bottom; the wordmark stays the page's h1 and is only
          visually hidden from density 1 on (never display:none). */}
      <span className="tb-mark" aria-hidden="true"><Icon name="brand" /></span>
      <h1 className="topbar-brand">Video AI Editor</h1>
      <div data-session-picker className="tb-project">
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
          data-tip={`${shownName} — switch project (double-click to rename)`}
          aria-haspopup="menu"
          aria-expanded={pickerOpen}
          onClick={() => setPickerOpen((o) => !o)}
          onDoubleClick={(e) => { e.preventDefault(); startRename() }}
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
        // A spinner plus words; at density 3 the words give way and the
        // sr-only sentence stays the status text.
        <span className="pill tb-applying" role="status" data-tip="An edit is being applied">
          <Icon name="loading" className="icon-spin" />
          <span className="tb-applying-word" aria-hidden="true">Applying</span>
          <span className="tb-sr-only">An edit is being applied</span>
        </span>
      )}
      {/* What is running (LEFT_RAIL_SPEC §2.8): recording Stop and captions
          Cancel, whatever tool panel shows. Always rendered — its live region
          must exist before the first message. Its words hide at density 4. */}
      <ActivityChip />
      </div>
      {/* The canvas: aspect ratios, platform presets and the safe-zone overlay
          in one menu (§2.10). The facts ride on the trigger at density 0 only;
          they are always in its name, its tooltip and the menu's footer. */}
      <div className="tb-center">
        <RatioMenu />
      </div>
      {/* Pinned right cluster: its column is at least max-content wide, so
          nothing here ever clips, and Export is its last child — always the
          toolbar's right-most, always-visible control (QA-012). */}
      <div className="tb-right topbar-pinned">
        <button
          className="tb-labelled"
          onClick={onSaveProject}
          disabled={saving || !edl?.duration}
          aria-label={saving ? 'Saving…' : 'Save'}
          data-tip={!edl?.duration
            ? 'Nothing to save yet — add a video to the timeline first'
            : saving
              ? 'Saving the project file…'
              : 'Save an editable project file (.vae) you can reopen later'}
        >
          {/* At density 3 the words give way; the icon, name and tooltip stay. */}
          {saving ? 'Saving…' : <><Icon name="save" /><span className="topbar-btn-label">Save</span></>}
        </button>
        <button className="tb-labelled" onClick={() => importRef.current?.click()} data-tip="Open a saved .vae project" aria-label="Open">
          <Icon name="open" /><span className="topbar-btn-label">Open</span>
        </button>
        <input ref={importRef} type="file" accept=".vae,.zip" hidden
          onChange={(e) => { const f = e.target.files?.[0]; if (f) void onLoadProject(f) }} />
        {savedHere && (
          // "(outdated)" is in the NAME at every density; from density 2 the
          // visible words become a warn dot.
          <a href={savedHere.url} download={savedHere.filename} onClick={onSavedLinkClick(savedHere)}
            className={savedStale ? 'tb-dl stale-dl' : 'tb-dl'}
            aria-label={`Download the saved .vae project${savedStale ? ' (outdated)' : ''}`}
            data-tip={savedStale ? 'This .vae predates your latest edits' : 'Download saved project'}>
            <Icon name="download" /> .vae{savedStale && <StaleMark />}
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
            className={exportView.stale ? 'tb-dl stale-dl' : 'tb-dl'}
            aria-label={`Save exported ${exportKind(exportView.link)}${exportView.stale ? ' (outdated)' : ''}`}
            data-tip={exportView.stale ? 'This render is not the timeline you have now — re-export for an up-to-date file' : `Save exported ${exportKind(exportView.link)}`}>
            <Icon name="download" /> {exportKind(exportView.link)}{exportView.stale && <StaleMark />}
          </button>
        )}
        {exportError && (
          // Show the REASON, not just "failed". The backend maps ffmpeg stderr
          // through _render_failure_message, so this is a sentence a user can
          // act on ("…an audio-only file on the video track. Move that clip to
          // the Music lane"). The RuntimeError: prefix jobs.py adds is
          // stripped; the text is capped per density (220 / 180 / 140 px) and
          // icon-only at density 3 — the whole message is always in the name
          // and the tooltip. A <button>, so the keyboard can dismiss it (QA-102).
          <button
            type="button"
            className="topbar-export-error"
            data-tip={`${errorText} (click to dismiss)`}
            aria-label={`Export failed: ${errorText}. Dismiss`}
            onClick={() => clearExportError()}
          >
            <Icon name="warning" /><span className="tb-err-text">{errorText}</span><Icon name="close" className="tb-err-x" />
          </button>
        )}
        {/* The right-most pinned control (QA-012): Export ▾ → the dialog. */}
        <ExportButton />
      </div>
    </header>
  )
}

/** A stale link's marker: the words " (outdated)" through density 1, a 6 px
 *  warn dot from density 2. Both decorative — the link's name says it. */
function StaleMark() {
  return (
    <>
      <span className="tb-stale-dot" aria-hidden="true" />
      <span className="tb-stale-word" aria-hidden="true"> (outdated)</span>
    </>
  )
}
