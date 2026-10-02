// The project file menu in the top bar: Save project file (.vae), Open a
// project file or media, Import media, Rename. These lived in the old top
// bar (Save · Open · the .vae link) and the design has no row for them, so
// they sit under one ghost button rather than disappearing — a project file
// you can save and reopen is the product's own portability (storage_project).
import { useEffect, useRef, useState, type MouseEvent as ReactMouseEvent } from 'react'
import { createPortal } from 'react-dom'
import { api } from '../../api'
import { errorMessage, useStore } from '../../store'
import { toast } from '../../toast'
import { useMenuA11y } from '../../lib/useMenuA11y'
import { claimClickForNativeSave } from '../../lib/nativeSave'
import { importFiles } from '../../lib/fileDrop'
import { classifyOpenedFile, mediaInsteadOfProject } from '../../lib/openPick'
import { MEDIA_PICKER_ACCEPT, PROJECT_PICKER_ACCEPT } from '../../lib/paths'
import { isSavedProjectStale, savedProject, visibleSavedProject, type SavedProject } from '../../lib/savedProject'
import { Icon } from '../Icon'

export function FileMenu({ onRename }: { onRename: () => void }) {
  const sid = useStore((s) => s.sessionId)
  const edl = useStore((s) => s.edl)
  const opsLen = useStore((s) => s.ops.length)
  const [open, setOpen] = useState(false)
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState<SavedProject | null>(null)
  const btnRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const projectRef = useRef<HTMLInputElement>(null)
  const mediaRef = useRef<HTMLInputElement>(null)
  const a11y = useMenuA11y({ open, ready: !!pos, mode: 'menu', menuRef, triggerRef: btnRef, onClose: () => setOpen(false) })
  const savedHere = visibleSavedProject(saved, sid)
  const stale = !!savedHere && isSavedProjectStale(savedHere, opsLen)

  useEffect(() => {
    if (!open) return
    const close = (e: MouseEvent) => {
      const t = e.target as HTMLElement
      if (menuRef.current?.contains(t) || btnRef.current?.contains(t)) return
      setOpen(false)
    }
    const id = window.setTimeout(() => window.addEventListener('mousedown', close), 0)
    return () => { window.clearTimeout(id); window.removeEventListener('mousedown', close) }
  }, [open])

  const toggle = () => {
    if (open) { setOpen(false); return }
    const r = btnRef.current?.getBoundingClientRect()
    if (r) setPos({ left: Math.max(8, r.right - 260), top: r.bottom + 4 })
    setOpen(true)
  }
  const save = async () => {
    if (!sid) return
    setSaving(true)
    setSaved(null)
    try {
      const r = await api.saveProject(sid)
      setSaved(savedProject(sid, r.url, useStore.getState().ops.length, r.filename))
      if (r.warning) toast.error(r.warning, 9000)
      else toast.success('Project file saved — download it from the File menu')
    } catch (e) { toast.error(`Couldn't save the project: ${errorMessage(e)}`) } finally { setSaving(false) }
  }
  const onSavedLinkClick = (link: SavedProject) => (e: ReactMouseEvent<HTMLAnchorElement>) => {
    const pending = claimClickForNativeSave(e, link.sid, link.filename)
    if (!pending) return
    void pending.then((o) => {
      if (o.kind === 'saved') toast.success(`Saved to ${o.path}`)
      else if (o.kind === 'failed') toast.error(`Couldn't save the project file: ${errorMessage(o.error)}`)
    })
  }
  const importPicked = async (files: File[]) => {
    const { upload, uploadAudio } = useStore.getState()
    await importFiles(files, { upload, uploadAudio })
  }
  const onOpen = async (file: File) => {
    if (classifyOpenedFile(file) === 'media') { toast.info(mediaInsteadOfProject(file)); await importPicked([file]); return }
    try {
      const r = await api.loadProject(file)
      await useStore.getState().openSession(r.id)
    } catch (e) { toast.error(`Couldn't open that .vae project: ${errorMessage(e)}`) }
  }

  return (
    <>
      <button ref={btnRef} type="button" className="ui-btn-ghost ed-file-btn" aria-haspopup="menu" aria-expanded={open} aria-label="File" title="Save or open a project file, import media" onClick={toggle}>
        <Icon name="save" /><span className="ed-layout-word">File</span><Icon name="chevronDown" />
      </button>
      <input ref={projectRef} type="file" accept={`${PROJECT_PICKER_ACCEPT},${MEDIA_PICKER_ACCEPT}`} hidden data-testid="open-file"
             onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ''; if (f) void onOpen(f) }} />
      <input ref={mediaRef} type="file" accept={MEDIA_PICKER_ACCEPT} multiple hidden data-testid="import-media-file"
             onChange={(e) => { const picked = Array.from(e.target.files ?? []); e.target.value = ''; void importPicked(picked) }} />
      {open && pos && createPortal(
        <div ref={menuRef} role="menu" aria-label="File" className="ui-menu" data-keymap-ignore onKeyDown={a11y.onKeyDown} style={{ left: pos.left, top: pos.top, width: 260 }}>
          <button type="button" role="menuitem" className="ui-menu-item" disabled={saving || !edl?.duration} onClick={() => { a11y.close(); void save() }}
                  title={!edl?.duration ? 'Nothing to save yet — add a video to the timeline first' : 'Save an editable project file (.vae) you can reopen later'}>
            <Icon name="save" />{saving ? 'Saving…' : 'Save project file (.vae)'}
          </button>
          {savedHere && (
            <a role="menuitem" className="ui-menu-item" href={savedHere.url} download={savedHere.filename} onClick={onSavedLinkClick(savedHere)}
               title={stale ? 'This .vae predates your latest edits' : 'Download the saved project file'}>
              <Icon name="download" />Download .vae{stale ? ' (outdated)' : ''}
            </a>
          )}
          <button type="button" role="menuitem" className="ui-menu-item" onClick={() => { a11y.close(false); projectRef.current?.click() }} title="Open a .vae project — or pick a video to add it to this project">
            <Icon name="open" />Open project file…
          </button>
          <button type="button" role="menuitem" className="ui-menu-item" onClick={() => { a11y.close(false); mediaRef.current?.click() }} title="Add videos, audio or photos to this project">
            <Icon name="upload" />Import media…
          </button>
          <div className="ui-menu-sep" role="separator" />
          <button type="button" role="menuitem" className="ui-menu-item" onClick={() => { a11y.close(false); onRename() }}>
            <Icon name="rename" />Rename project…
          </button>
        </div>,
        document.body,
      )}
    </>
  )
}
