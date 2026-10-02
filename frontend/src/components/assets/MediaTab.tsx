// Media (design §2a "Media / Audio import"; brief §2 "Asset behavior"):
// Import / Record / search, the import progress strip, the dashed drop zone,
// and the asset cards — every import of the project (GET /media), used or
// not. Clicking a card previews the source in the Player; its ＋ appends it
// to the end of the matching track and marks it "Added".
import { useRef, useState } from 'react'
import { api } from '../../api'
import { useStore, errorMessage } from '../../store'
import { toast } from '../../toast'
import { dropzoneHandlers, importFiles } from '../../lib/fileDrop'
import { MEDIA_PICKER_ACCEPT } from '../../lib/paths'
import { clockDuration, type BinRow } from '../../lib/mediaLibrary'
import { batchLabel, uploadStageLabel } from '../../lib/uploadQueue'
import { appendToTimeline, useMediaLibrary } from '../../lib/mediaTabLib'
import { useInspector } from '../inspector/inspectorStore'
import { Icon } from '../Icon'
import { AiPanel } from '../AiPanel'
import { PanelState, Unavailable } from './Catalogue'
import { ConfirmDialog } from '../ConfirmDialog'

export function MediaTab({ sub }: { sub: string }) {
  switch (sub) {
    case 'Import': case 'Media': case 'Library': return <MediaGrid filter="all" />
    case 'Yours': return <MediaGrid filter="yours" />
    case 'Subprojects':
      return <Unavailable title="No subprojects yet" body="A compound clip (right-click a clip › Create compound clip) would appear here. Compound clips are not part of this build." />
    case 'AI media': return <div className="ab-ai"><AiPanel active /></div>
    case 'Spaces':
      return <Unavailable title="Sign in to use Spaces" body="Shared cloud Spaces need an account. This build keeps every project and its media on this computer." />
    default: return null
  }
}

export function MediaGrid({ filter, kinds = ['video', 'audio'], searchPlaceholder = 'Search media', recordSlot }: {
  filter: 'all' | 'yours'
  kinds?: readonly ('video' | 'audio')[]
  searchPlaceholder?: string
  /** Audio's Record (the voiceover recorder) renders here instead of the button. */
  recordSlot?: React.ReactNode
}) {
  const sid = useStore((s) => s.sessionId)
  const upload = useStore((s) => s.upload)
  const uploadAudio = useStore((s) => s.uploadAudio)
  const uploading = useStore((s) => s.uploading)
  const uploads = useStore((s) => s.uploads)
  const uploadBatch = useStore((s) => s.uploadBatch)
  const uploadError = useStore((s) => s.uploadError)
  const clearUploadError = useStore((s) => s.clearUploadError)
  const addToTimeline = useStore((s) => s.importAddToTimeline)
  const setAddToTimeline = useStore((s) => s.setImportAddToTimeline)
  const preview = useInspector((s) => s.preview)
  const previewAsset = useInspector((s) => s.previewAsset)
  const { rows, reload } = useMediaLibrary()
  const fileRef = useRef<HTMLInputElement>(null)
  const [over, setOver] = useState(false)
  const [query, setQuery] = useState('')
  const [confirmRemove, setConfirmRemove] = useState<BinRow | null>(null)
  const zone = dropzoneHandlers(setOver)
  const onFiles = (files: ArrayLike<File> | null) => importFiles(files, { upload, uploadAudio })

  const shown = rows.filter((r) => kinds.includes(r.kind))
    .filter((r) => !query.trim() || r.name.toLowerCase().includes(query.trim().toLowerCase()))
  const pct = uploadBatch.total ? Math.round((uploadBatch.done / uploadBatch.total) * 100) : 0
  const live = uploads[0]

  const removeRow = async (row: BinRow) => {
    if (!sid) return
    setConfirmRemove(null)
    try {
      if (row.uses) await useStore.getState().dispatch('bulk_delete', { clip_ids: row.clipIds })
      if (row.id) await api.removeMedia(sid, row.id)
      if (preview?.src === row.src) previewAsset(null)
    } catch (e) {
      toast.error(`Couldn't remove ${row.name}: ${errorMessage(e)}`)
    }
    reload()
  }

  return (
    <div className="ab-media">
      <div className="ab-toolbar">
        <button type="button" className="ui-btn-primary" onClick={() => fileRef.current?.click()} title="Add video, image or audio files to this project">
          <Icon name="download" />Import
        </button>
        {recordSlot ?? (
          <button type="button" className="ui-btn-secondary" disabled title="Screen and camera recording are not part of this build — record a voiceover from the Audio tab">
            <Icon name="record" />Record
          </button>
        )}
        <span className="ab-grow" />
        <label className="ui-search ab-search-sm">
          <Icon name="search" />
          <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder={searchPlaceholder} aria-label={searchPlaceholder} />
        </label>
      </div>
      <input ref={fileRef} type="file" accept={MEDIA_PICKER_ACCEPT} multiple hidden
             onChange={(e) => { const picked = Array.from(e.target.files ?? []); e.target.value = ''; void onFiles(picked) }} />
      <label className="ab-import-toggle">
        <input type="checkbox" checked={addToTimeline} onChange={(e) => setAddToTimeline(e.target.checked)} />
        Add imports to the timeline
      </label>
      {uploading && (
        <div className="ab-progress" role="status" aria-live="polite">
          <span className="ui-spinner"><Icon name="loading" className="icon-spin" /></span>
          <div className="ab-progress-text">
            <span>{batchLabel(uploads, uploadBatch) || 'Importing…'}{live ? ` · ${uploadStageLabel(live)}` : ''}</span>
            <div className="ab-bar"><div className="ab-bar-fill" style={{ width: `${Math.max(3, live ? Math.round(live.progress * 100) : pct)}%` }} /></div>
          </div>
          {live && <button type="button" className="ui-icon-btn is-small" aria-label={`Cancel importing ${live.name}`} onClick={() => useStore.getState().cancelUpload(live.id)}><Icon name="close" /></button>}
        </div>
      )}
      {uploadError && (
        <div className="ab-error" role="alert">
          <div className="ab-error-head"><b>Import failed</b>
            <button type="button" className="ui-icon-btn is-tiny" aria-label="Dismiss import error" onClick={clearUploadError}><Icon name="close" /></button>
          </div>
          {uploadError}
        </div>
      )}
      {shown.length === 0 && !uploading && (
        <button type="button" className={`ab-dropzone${over ? ' is-over' : ''}`} onDragOver={zone.onDragOver} onDragLeave={zone.onDragLeave}
                onDrop={zone.onDrop} onClick={() => fileRef.current?.click()}>
          <Icon name="upload" />
          <span>{query ? `Nothing matches “${query}”` : 'Drag video, image or audio here'}</span>
          <span className="ab-dropzone-sub">or click Import</span>
        </button>
      )}
      {shown.length > 0 && (
        <div className="ab-cards" onDragOver={zone.onDragOver} onDragLeave={zone.onDragLeave} onDrop={zone.onDrop}>
          {shown.map((row) => (
            <AssetCard key={row.id ?? row.src} row={row} sid={sid} previewed={preview?.src === row.src}
                       onPreview={() => previewAsset(row.missing ? null : {
                         src: row.src, name: row.name, kind: row.kind, duration: row.duration, width: row.width, height: row.height,
                         still: row.still, id: row.id })}
                       onAdd={() => void appendToTimeline(row)} onRemove={() => setConfirmRemove(row)} onRelinked={reload} />
          ))}
        </div>
      )}
      {filter === 'yours' && shown.length === 0 && rows.length > 0 && !uploading && (
        <PanelState kind="empty" title="Nothing imported by you yet" body="Everything you import lands here." />
      )}
      {confirmRemove && (
        <ConfirmDialog title={`Remove “${confirmRemove.name}”?`}
          body={confirmRemove.uses ? `Its ${confirmRemove.uses === 1 ? 'clip is' : `${confirmRemove.uses} clips are`} deleted from the timeline too — Undo brings ${confirmRemove.uses === 1 ? 'it' : 'them'} back.`
            : 'It leaves this project’s media. The file stays on disk; import it again to use it.'}
          confirmLabel="Remove" danger onConfirm={() => void removeRow(confirmRemove)} onCancel={() => setConfirmRemove(null)} />
      )}
    </div>
  )
}

function AssetCard({ row, sid, previewed, onPreview, onAdd, onRemove, onRelinked }: {
  row: BinRow; sid: string | null; previewed: boolean; onPreview: () => void; onAdd: () => void; onRemove: () => void; onRelinked: () => void
}) {
  const relinkRef = useRef<HTMLInputElement>(null)
  const [relinking, setRelinking] = useState(false)
  const t = row.duration ? Math.min(1, row.duration / 2) : 0
  const thumb = row.kind === 'video' && sid && row.id && !row.missing
    ? `/api/sessions/${sid}/thumb?src=${encodeURIComponent(row.src)}&t=${t.toFixed(2)}&h=90` : null
  const relink = async (file: File) => {
    if (!sid || !row.id) return
    setRelinking(true)
    try {
      const r = await api.relinkMedia(sid, row.id, file)
      toast.success(r.summary)
      await useStore.getState().refresh()
      onRelinked()
    } catch (e) { toast.error(`Couldn't relink ${row.name}: ${errorMessage(e)}`) } finally { setRelinking(false) }
  }
  return (
    <div className={`ab-card${previewed ? ' is-previewed' : ''}${row.missing ? ' is-offline' : ''}`} data-media-row data-offline={row.missing || undefined}
         draggable={!row.missing}
         onDragStart={(e) => {
           e.dataTransfer.effectAllowed = 'copy'
           e.dataTransfer.setData('application/x-vai-src', row.src)
           if (row.duration) e.dataTransfer.setData('application/x-vai-duration', String(row.duration))
           e.dataTransfer.setData('text/plain', row.src)
         }}>
      <button type="button" className="ab-card-thumb" onClick={onPreview} onDoubleClick={onAdd}
              aria-label={`${row.missing ? 'Offline: ' : 'Preview '}${row.name}`}
              title={row.missing ? `${row.name} is missing from disk — relink it` : `${row.name} — click to preview, double-click or ＋ to add to the timeline, or drag it onto a lane`}
              data-keymap-own="Enter Delete Backspace"
              onKeyDown={(e) => { if (e.key === 'Enter' && !row.missing) { e.preventDefault(); onAdd() } }}>
        {thumb ? <img src={thumb} alt="" loading="lazy" draggable={false} />
          : <span className="ab-card-glyph">{row.missing ? <Icon name="warning" size={20} /> : <Icon name={row.kind === 'audio' ? 'audioFile' : 'image'} size={20} />}</span>}
        {!row.missing && <span className="ab-card-dur">{row.still ? 'photo' : clockDuration(row.duration) || (row.kind === 'audio' ? 'audio' : '')}</span>}
        {row.uses > 0 && <span className="ab-card-added">Added{row.uses > 1 ? ` ×${row.uses}` : ''}</span>}
        {row.missing && <span className="ab-card-offline">Offline</span>}
      </button>
      {!row.missing ? (
        <button type="button" className="ab-card-plus" aria-label={`Add ${row.name} to the timeline`} title="Add to timeline" onClick={onAdd}><Icon name="plus" /></button>
      ) : row.id && (
        <>
          <button type="button" className="ab-card-relink" disabled={relinking} onClick={() => relinkRef.current?.click()} aria-label={`Relink ${row.name}`}>
            {relinking ? 'Relinking…' : 'Relink…'}
          </button>
          <input ref={relinkRef} type="file" hidden accept={row.kind === 'audio' ? 'audio/*,.mp3,.wav,.m4a,.aac,.flac,.ogg,.mp4,.mov' : MEDIA_PICKER_ACCEPT}
                 onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ''; if (f) void relink(f) }} />
        </>
      )}
      <button type="button" className="ab-card-x" aria-label={`Remove ${row.name}`} title="Remove from the project" onClick={onRemove}><Icon name="close" /></button>
      <span className="ab-card-name" title={row.name}>{row.name}</span>
    </div>
  )
}
