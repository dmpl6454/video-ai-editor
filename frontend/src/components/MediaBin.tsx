import { useEffect, useRef, useState } from 'react'
import { useStore, errorMessage } from '../store'
import { api } from '../api'
import { toast } from '../toast'
import type { MediaItem } from '../types'
import { dropzoneHandlers, importFiles } from '../lib/fileDrop'
import { binMeta, binRows, type BinRow } from '../lib/mediaLibrary'
import { nameBreaks, useMediaNames } from '../lib/mediaNames'
import { batchLabel, etaLabel, uploadEtaSeconds, uploadStageLabel, type UploadItem } from '../lib/uploadQueue'
import { Icon } from './Icon'
import { insertAtPlayhead } from '../lib/mediaInsert'
import { ConfirmDialog } from './ConfirmDialog'
import { DeepLinks } from './rail/DeepLinkRow'

// The Media tool panel (docs/design/LEFT_RAIL_SPEC.md §2.5): the dropzone, the
// import switch, imports in flight and the project's media library. Music,
// voiceover and the mix moved to the Audio panel, stickers and effects to
// their own panels (rail R1); the panel header (ToolPanel) is its heading.
// Last, Find & search (R5): deep links to the AI panel's footage search cards.
export function MediaBin() {
  const upload = useStore((s) => s.upload)
  const uploadAudio = useStore((s) => s.uploadAudio)
  const uploading = useStore((s) => s.uploading)
  // QA-044/094: one placeholder row per file still importing, in drop order.
  const uploads = useStore((s) => s.uploads)
  const uploadBatch = useStore((s) => s.uploadBatch)
  const cancelUpload = useStore((s) => s.cancelUpload)
  // QA-010: import to the library only, or also onto the timeline.
  const addToTimeline = useStore((s) => s.importAddToTimeline)
  const setAddToTimeline = useStore((s) => s.setImportAddToTimeline)
  const uploadError = useStore((s) => s.uploadError)
  const clearUploadError = useStore((s) => s.clearUploadError)
  const edl = useStore((s) => s.edl)
  const dispatch = useStore((s) => s.dispatch)
  const fileRef = useRef<HTMLInputElement>(null)
  const [dragOver, setDragOver] = useState(false)

  const sid = useStore((s) => s.sessionId)

  // The click-to-pick input; a DROP is imported by FileDropOverlay's window
  // listener alone (lib/fileDrop — two importers imported every drop twice).
  const onFiles = (files: ArrayLike<File> | null) => importFiles(files, { upload, uploadAudio })
  const zone = dropzoneHandlers(setDragOver)

  // The project's media library (QA-010) — every import, used or not. Fetched
  // per session and again whenever the EDL object is replaced (every refresh,
  // i.e. after uploads, edits, undo) or an upload finishes; the rows' used-
  // counts come from the live EDL in binRows, so they never lag the timeline.
  const [library, setLibrary] = useState<{ sid: string; items: MediaItem[] } | null>(null)
  const [libraryTick, setLibraryTick] = useState(0)
  useEffect(() => {
    if (!sid) return
    let live = true
    api.listMedia(sid)
      .then((r) => {
        if (!live) return
        setLibrary({ sid, items: r.media })
        // QA-045/095: every other surface names clips (and flags offline
        // ones) from this same listing — see lib/mediaNames.
        useMediaNames.getState().publish(sid, r.media)
      })
      .catch((e) => console.warn('[MediaBin] media library fetch failed:', e))
    return () => { live = false }
  }, [sid, edl, uploading, libraryTick])
  const rows = binRows(library && library.sid === sid ? library.items : null, edl)

  // Enter / double-click / the row's button: at the playhead (lib/mediaInsert).
  const insertRow = (row: BinRow) => {
    const st = useStore.getState()
    const ins = insertAtPlayhead(row, st.edl, st.playhead)
    if (!ins) return
    void dispatch(ins.tool, ins.args)
  }

  // Removing asks first in the app's own dialog (QA-129) — it was the native
  // window.confirm, a light system sheet over the dark editor.
  const [confirmRemove, setConfirmRemove] = useState<BinRow | null>(null)
  const removeRow = async (row: BinRow) => {
    if (!sid) return
    setConfirmRemove(null)
    try {
      if (row.uses) await dispatch('bulk_delete', { clip_ids: row.clipIds })
      if (row.id) await api.removeMedia(sid, row.id)
    } catch (e) {
      toast.error(`Couldn't remove ${row.name}: ${errorMessage(e)}`)
    }
    setLibraryTick((n) => n + 1)
  }

  return (
    <div className="media-bin">
      {/* A <button> (QA-102): it was a clickable <div> with no tabindex, so Tab
          skipped it and there was no keyboard way to import video at all.
          Enter/Space open the same picker a click does. The file <input> sits
          beside it — an input nested inside a button is invalid HTML. */}
      <button
        type="button"
        className={`dropzone${dragOver ? ' over' : ''}`}
        title={addToTimeline
          ? 'Video lands on the main video track; audio lands on the Music track'
          : 'Imports go to this media list only — drag them onto the timeline when you need them'}
        onDragOver={zone.onDragOver}
        onDragLeave={zone.onDragLeave}
        onDrop={zone.onDrop}
        onClick={() => fileRef.current?.click()}
      >
        {uploading ? (batchLabel(uploads, uploadBatch) || 'Importing…') : 'Drop video, audio or photos · or click'}
      </button>
      {/* Wider accept list so HEIC/HEVC/.mov sources from iPhone & cameras pass the picker. */}
      <input
        ref={fileRef}
        type="file"
        accept="video/*,audio/*,image/*,.mov,.MOV,.mp4,.MP4,.m4v,.mkv,.webm,.avi,.MTS,.mp3,.wav,.m4a,.aac,.flac,.png,.jpg,.jpeg,.heic,.HEIC,.heif,.webp"
        multiple
        hidden
        onChange={(e) => {
          // QA-094: several files at once, imported in the order picked. The
          // value is cleared so picking the same file again still imports.
          const picked = Array.from(e.target.files ?? [])
          e.target.value = ''
          void onFiles(picked)
        }}
      />
      <label className="import-toggle">
        <input type="checkbox" checked={addToTimeline} onChange={(e) => setAddToTimeline(e.target.checked)} />
        Add imports to the timeline
      </label>

      {uploadError && (
        <div style={{
          background: 'var(--error-bg)',
          border: '1px solid var(--error-line)',
          color: 'var(--error-text)',
          padding: '8px 10px',
          borderRadius: 6,
          fontSize: 11,
          marginBottom: 10,
          whiteSpace: 'pre-wrap',
          maxHeight: 220,
          overflow: 'auto',
        }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 4 }}>
            <b>Upload failed</b>
            <button
              onClick={clearUploadError}
              aria-label="Dismiss upload error"
              style={{ background: 'transparent', border: 'none', color: 'inherit', padding: 0, cursor: 'pointer', lineHeight: 0 }}
            >
              <Icon name="close" />
            </button>
          </div>
          {uploadError}
        </div>
      )}

      {uploads.map((u) => (
        <UploadRow key={u.id} item={u} onCancel={() => cancelUpload(u.id)} />
      ))}

      {rows.length === 0 && !uploading && (
        <div style={{ color: 'var(--text-dim)', fontSize: 11, marginBottom: 10, lineHeight: 1.5 }}>
          Imported media stays here until you remove it. Double-click an item
          (or press Enter) to add it at the playhead, or drag it onto a
          timeline lane — the Video lane, or an overlay lane for picture-in-picture.
        </div>
      )}
      {rows.map((row) => (
        <MediaRow key={row.id ?? row.src} row={row} sid={sid} onRemove={() => setConfirmRemove(row)}
                  onInsert={() => insertRow(row)}
                  onRelinked={() => setLibraryTick((n) => n + 1)} />
      ))}

      <DeepLinks from="media" />

      {confirmRemove && (
        <ConfirmDialog
          title={`Remove “${confirmRemove.name}”?`}
          body={removeMediaBody(confirmRemove.uses)}
          confirmLabel="Remove"
          danger
          onConfirm={() => void removeRow(confirmRemove)}
          onCancel={() => setConfirmRemove(null)}
        />
      )}
    </div>
  )
}

/** What removing a media item does, in words (QA-129; no "clip(s)", QA-101). */
function removeMediaBody(uses: number): string {
  if (uses === 1) return 'Its clip is deleted from the timeline too — Undo brings it back.'
  if (uses > 1) return `Its ${uses} clips are deleted from the timeline too — Undo brings them back.`
  return 'It leaves this project’s media. The file stays on disk; import it again to use it.'
}

/**
 * A file still importing (QA-044): a placeholder where its row will land, with
 * a progress ring, the stage ("Uploading 42%", "Preparing 17%"), a time
 * estimate and Cancel. It replaces one static "Uploading <name>…" line that
 * sat unchanged for minutes on a long file.
 */
function UploadRow({ item, onCancel }: { item: UploadItem; onCancel: () => void }) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [])
  const label = uploadStageLabel(item)
  const eta = etaLabel(uploadEtaSeconds(item, now))
  const where = item.addToTimeline ? '' : ' · library only'
  const determinate = item.stage === 'uploading' || item.stage === 'processing'
  return (
    <div className="item media-row is-pending" data-upload-row role="status" aria-live="polite"
         aria-label={`Importing ${item.name}: ${label}${eta ? `, ${eta}` : ''}`}>
      <div className="media-thumb" aria-hidden="true">
        <span className={`upload-ring${determinate ? '' : ' is-waiting'}`}
              style={{ '--p': String(Math.min(1, Math.max(0, item.progress))) } as React.CSSProperties} />
      </div>
      <div className="media-text">
        <div className="media-name" title={item.name}><BreakableName name={item.name} /></div>
        <div className="media-meta">{label}{eta ? ` · ${eta}` : ''}{where}</div>
      </div>
      <button className="media-remove" type="button" title="Cancel this import"
              aria-label={`Cancel importing ${item.name}`} onClick={onCancel}>
        <Icon name="close" />
      </button>
    </div>
  )
}

/** A file name whose line breaks fall between words, never inside the
 *  extension (lib/mediaNames.nameBreaks). */
function BreakableName({ name }: { name: string }) {
  const parts = nameBreaks(name)
  return <>{parts.map((p, i) => <span key={i}>{i > 0 && <wbr />}{p}</span>)}</>
}

export function MediaRow({ row, sid, onRemove, onInsert, onRelinked }: {
  row: BinRow; sid: string | null; onRemove: () => void; onInsert: () => void; onRelinked: () => void
}) {
  const relinkRef = useRef<HTMLInputElement>(null)
  const [relinking, setRelinking] = useState(false)
  // One JPEG frame from /thumb (the timeline filmstrip's route); a frame a
  // second in (or mid-clip for a short one) rather than the often-black first.
  const t = row.duration ? Math.min(1, row.duration / 2) : 0
  // Only once the library has listed it (row.id): before that a row may be a
  // missing file or an audio-only .mp4, and its thumbnail can only fail.
  const thumb = row.kind === 'video' && sid && row.id && !row.missing
    ? `/api/sessions/${sid}/thumb?src=${encodeURIComponent(row.src)}&t=${t.toFixed(2)}&h=54`
    : null
  // QA-095: replace a missing file; every clip that played it follows.
  const relink = async (file: File) => {
    if (!sid || !row.id) return
    setRelinking(true)
    try {
      const r = await api.relinkMedia(sid, row.id, file)
      toast.success(r.summary)
      await useStore.getState().refresh()
      onRelinked()
    } catch (e) {
      toast.error(`Couldn't relink ${row.name}: ${errorMessage(e)}`)
    } finally {
      setRelinking(false)
    }
  }
  // The user's name only (QA-045) — never the disk name or a path.
  const hint = row.missing
    ? `${row.name}\n\nThis file is missing from disk. Relink it to the original (or a copy) to bring back ${row.uses === 1 ? 'its clip' : `its ${row.uses} clips`}.`
    : `${row.name}\n\nDouble-click or press Enter to add ${row.uses ? 'another instance' : 'it'} at the playhead, or drag it onto a lane.`
  return (
    <div
      className={`item media-row${row.uses ? '' : ' is-unused'}${row.missing ? ' is-offline' : ''}`}
      data-media-row
      data-offline={row.missing || undefined}
      title={hint}
      // A focusable control (wave-B review): Enter inserts at the playhead,
      // like a double-click. data-keymap-own keeps Enter, and Delete /
      // Backspace (no ripple delete of the timeline's clip behind the row),
      // from the timeline shortcuts; ⌘Z, J/K/L, N and Space still run (Final
      // QA r3: an ignore scope here made ⌘Z after "+" do nothing).
      tabIndex={0}
      role="group"
      aria-label={`${row.name}, ${binMeta(row)}`}
      data-keymap-own="Enter Delete Backspace"
      onDoubleClick={() => { if (!row.missing) onInsert() }}
      onKeyDown={(e) => {
        if (e.target !== e.currentTarget || row.missing) return
        if (e.key === 'Enter') { e.preventDefault(); onInsert() }
      }}
      draggable={!row.missing}
      onDragStart={(e) => {
        e.dataTransfer.effectAllowed = 'copy'
        e.dataTransfer.setData('application/x-vai-src', row.src)
        if (row.duration) e.dataTransfer.setData('application/x-vai-duration', String(row.duration))
        e.dataTransfer.setData('text/plain', row.src)  // fallback for sniffers
      }}
    >
      <div className="media-thumb" aria-hidden="true">
        {thumb ? <img src={thumb} alt="" width={96} height={54} loading="lazy" draggable={false} />
          : row.missing ? <span className="media-offline-mark"><Icon name="warning" /></span> : <Icon name="music" size={20} />}
      </div>
      <div className="media-text">
        <div className="media-name"><BreakableName name={row.name} /></div>
        <div className="media-meta">{binMeta(row)}</div>
        {row.missing && row.id && (
          <>
            <button type="button" className="media-relink" disabled={relinking || !sid}
                    onClick={(e) => { e.stopPropagation(); relinkRef.current?.click() }}
                    aria-label={`Relink ${row.name}`}>
              {relinking ? 'Relinking…' : 'Relink…'}
            </button>
            <input ref={relinkRef} type="file" hidden
                   accept={row.kind === 'audio' ? 'audio/*,.mp3,.wav,.m4a,.aac,.flac,.ogg,.mp4,.mov'
                     : 'video/*,image/*,.mov,.mp4,.m4v,.mkv,.webm,.avi,.MTS,.png,.jpg,.jpeg,.heic'}
                   onChange={(e) => {
                     const f = e.target.files?.[0]
                     e.target.value = ''
                     if (f) void relink(f)
                   }} />
          </>
        )}
      </div>
      {!row.missing && (
        <button
          className="media-add"
          type="button"
          title="Add to the timeline at the playhead (Enter)"
          aria-label={`Add ${row.name} to the timeline`}
          onClick={(e) => { e.stopPropagation(); onInsert() }}
        ><Icon name="addToTimeline" /></button>
      )}
      <button
        className="media-remove"
        title={row.uses
          ? `Remove from the project, with ${row.uses === 1 ? 'its clip' : `its ${row.uses} clips`} on the timeline`
          : 'Remove from the project (the file stays on disk)'}
        aria-label={`Remove ${row.name}`}
        onClick={(e) => { e.stopPropagation(); onRemove() }}
      ><Icon name="close" /></button>
    </div>
  )
}
