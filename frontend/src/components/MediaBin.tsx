import { useEffect, useRef, useState } from 'react'
import { useStore, errorMessage } from '../store'
import { api } from '../api'
import { toast } from '../toast'
import type { MediaItem } from '../types'
import { baseName } from '../lib/paths'
import { dropzoneHandlers, importFiles } from '../lib/fileDrop'
import { binMeta, binRows, type BinRow } from '../lib/mediaLibrary'
import { StickerPanel } from './StickerPanel'
import { EffectsPanel } from './EffectsPanel'
import { VoRecorder } from './VoRecorder'


export function MediaBin() {
  const upload = useStore((s) => s.upload)
  const uploadAudio = useStore((s) => s.uploadAudio)
  const uploading = useStore((s) => s.uploading)
  const progress = useStore((s) => s.uploadProgress)
  const uploadError = useStore((s) => s.uploadError)
  const clearUploadError = useStore((s) => s.clearUploadError)
  const edl = useStore((s) => s.edl)
  const dispatch = useStore((s) => s.dispatch)
  const fileRef = useRef<HTMLInputElement>(null)
  const audioRef = useRef<HTMLInputElement>(null)
  const [dragOver, setDragOver] = useState(false)

  const sid = useStore((s) => s.sessionId)

  // The click-to-pick input; a DROP is imported by FileDropOverlay's window
  // listener alone (lib/fileDrop — two importers imported every drop twice).
  const onFiles = (files: FileList | null) => importFiles(files, { upload, uploadAudio })
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
      .then((r) => { if (live) setLibrary({ sid, items: r.media }) })
      .catch((e) => console.warn('[MediaBin] media library fetch failed:', e))
    return () => { live = false }
  }, [sid, edl, uploading, libraryTick])
  const rows = binRows(library && library.sid === sid ? library.items : null, edl)

  const removeRow = async (row: BinRow) => {
    if (!sid) return
    const msg = row.uses
      ? `Remove ${row.name} from the project? Its ${row.uses} clip(s) are deleted from the timeline too (Undo brings them back).`
      : `Remove ${row.name} from the project's media? The file stays on disk; re-import it to use it again.`
    if (!window.confirm(msg)) return
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
      <h2>Media</h2>
      <div
        className={`dropzone${dragOver ? ' over' : ''}`}
        title="Video lands on the main video track; audio lands on the Music track"
        onDragOver={zone.onDragOver}
        onDragLeave={zone.onDragLeave}
        onDrop={zone.onDrop}
        onClick={() => fileRef.current?.click()}
      >
        {uploading ? `Uploading ${progress}…` : 'Drop video or audio · or click'}
        {/* Wider accept list so HEIC/HEVC/.mov sources from iPhone & cameras pass the picker. */}
        <input
          ref={fileRef}
          type="file"
          accept="video/*,audio/*,.mov,.MOV,.mp4,.MP4,.m4v,.mkv,.webm,.avi,.MTS,.mp3,.wav,.m4a,.aac,.flac"
          hidden
          onChange={(e) => { void onFiles(e.target.files) }}
        />
      </div>
      <button
        style={{ width: '100%', marginBottom: 10, fontSize: 11 }}
        onClick={() => audioRef.current?.click()}
        disabled={uploading}
        title={uploading
          ? 'Wait for the current upload to finish'
          : 'Pick an audio file — it lands on the Music track'}
      >
        🎵 Add music…
      </button>
      <input
        ref={audioRef}
        type="file"
        accept="audio/*,.mp3,.wav,.m4a,.aac,.flac,.ogg"
        hidden
        onChange={(e) => {
          const f = e.target.files?.[0]
          if (f) void uploadAudio(f)
        }}
      />

      {uploadError && (
        <div style={{
          background: '#311',
          border: '1px solid #533',
          color: '#fbb',
          padding: '8px 10px',
          borderRadius: 6,
          fontSize: 11,
          marginBottom: 10,
          whiteSpace: 'pre-wrap',
          maxHeight: 220,
          overflow: 'auto',
        }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 4 }}>
            <b style={{ color: '#fcc' }}>Upload failed</b>
            <button
              onClick={clearUploadError}
              style={{ background: 'transparent', border: 'none', color: '#fbb', padding: 0, cursor: 'pointer' }}
            >
              ×
            </button>
          </div>
          {uploadError}
        </div>
      )}

      {rows.length === 0 && !uploading && (
        <div style={{ color: 'var(--text-dim)', fontSize: 11, marginBottom: 10, lineHeight: 1.5 }}>
          Imported media stays here until you remove it — drag an item onto a
          timeline row (v1 main, v2 for picture-in-picture) to use it again.
        </div>
      )}
      {rows.map((row) => (
        <MediaRow key={row.id ?? row.src} row={row} sid={sid} onRemove={() => void removeRow(row)} />
      ))}

      <VoRecorder />
      <MusicPanel />
      <StickerPanel />
      <EffectsPanel />
    </div>
  )
}

function MediaRow({ row, sid, onRemove }: { row: BinRow; sid: string | null; onRemove: () => void }) {
  // One JPEG frame from /thumb (the timeline filmstrip's route); a frame a
  // second in (or mid-clip for a short one) rather than the often-black first.
  const t = row.duration ? Math.min(1, row.duration / 2) : 0
  const thumb = row.kind === 'video' && sid
    ? `/api/sessions/${sid}/thumb?src=${encodeURIComponent(row.src)}&t=${t.toFixed(2)}&h=54`
    : null
  return (
    <div
      className={`item media-row${row.uses ? '' : ' is-unused'}`}
      data-media-row
      title={`${row.name}\n${baseName(row.src)}\n\nDrag onto the timeline to add ${row.uses ? 'another instance' : 'it'}.`}
      draggable
      onDragStart={(e) => {
        e.dataTransfer.effectAllowed = 'copy'
        e.dataTransfer.setData('application/x-vai-src', row.src)
        if (row.duration) e.dataTransfer.setData('application/x-vai-duration', String(row.duration))
        e.dataTransfer.setData('text/plain', row.src)  // fallback for sniffers
      }}
    >
      <div className="media-thumb" aria-hidden="true">
        {thumb ? <img src={thumb} alt="" width={96} height={54} loading="lazy" draggable={false} /> : <span>🎵</span>}
      </div>
      <div className="media-text">
        <div className="media-name">{row.name}</div>
        <div className="media-meta">{binMeta(row)}</div>
      </div>
      <button
        className="media-remove"
        title={row.uses
          ? `Remove from the project, with its ${row.uses} clip(s) on the timeline`
          : 'Remove from the project (the file stays on disk)'}
        aria-label={`Remove ${row.name}`}
        onClick={(e) => { e.stopPropagation(); onRemove() }}
      >×</button>
    </div>
  )
}

function MusicPanel() {
  const edl = useStore((s) => s.edl)
  const dispatch = useStore((s) => s.dispatch)
  const music = edl?.tracks.find((t) => t.id === 'music')
  const clip = music?.clips.find((c) => 'src' in c) as { id: string; src: string } | undefined
  const ducking = !!(music as unknown as { duck?: unknown })?.duck

  // Approximate current gain by reading the (typed-loose) audio.gain_db
  const gain = clip
    ? ((clip as unknown as { audio?: { gain_db?: number } }).audio?.gain_db ?? -12)
    : -12
  // Commit-on-release, same pattern as Properties' sliders: the thumb +
  // readout track the drag locally, but set_volume dispatches ONCE on
  // pointer-up / key-release / blur. This used to dispatch (and kick a
  // preview re-render) on every onChange tick of the drag.
  const [localGain, setLocalGain] = useState(gain)
  const draggingGain = useRef(false)
  // Re-seed from the stored value when it changes from outside (undo, chat
  // edits) — but never stomp the value mid-drag.
  useEffect(() => { if (!draggingGain.current) setLocalGain(gain) }, [gain])

  if (!clip) return null

  const commitGain = (v: number) => {
    if (v !== gain) dispatch('set_volume', { target: 'music', db: v })
  }

  return (
    <div className="item" style={{ background: 'var(--bg-3)', borderColor: '#3a3a44' }}>
      <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 4 }}>🎵 Music</div>
      <div style={{ wordBreak: 'break-all' }}>{baseName(clip.src)}</div>
      <div className="row" style={{ marginTop: 6, gap: 6, alignItems: 'center' }}>
        <label style={{ fontSize: 11, color: 'var(--text-dim)', minWidth: 38 }}>Vol</label>
        <input
          type="range" min={-30} max={6} step={0.5} value={localGain}
          onChange={(e) => { draggingGain.current = true; setLocalGain(Number(e.target.value)) }}
          onPointerUp={(e) => { draggingGain.current = false; commitGain(Number((e.target as HTMLInputElement).value)) }}
          onPointerCancel={() => { draggingGain.current = false }}
          onKeyUp={(e) => { draggingGain.current = false; commitGain(Number((e.target as HTMLInputElement).value)) }}
          onBlur={(e) => { draggingGain.current = false; commitGain(Number((e.target as HTMLInputElement).value)) }}
          style={{ flex: 1 }}
        />
        <span style={{ fontSize: 11, fontVariantNumeric: 'tabular-nums', minWidth: 36, textAlign: 'right' }}>
          {localGain.toFixed(0)} dB
        </span>
      </div>
      <div className="row" style={{ marginTop: 6, gap: 6, alignItems: 'center' }}>
        <label
          style={{ fontSize: 11, color: 'var(--text-dim)' }}
          title="Automatically lower the music whenever someone is speaking"
        >
          <input
            type="checkbox" checked={ducking}
            onChange={() => dispatch('set_duck', { track: 'music', enabled: !ducking })}
            style={{ marginRight: 4 }}
          />
          Duck under speech
        </label>
      </div>
      <button
        style={{ marginTop: 6, width: '100%', fontSize: 11 }}
        title="Remove this music from the timeline (the file stays uploaded)"
        onClick={() => dispatch('ripple_delete', { clip_id: clip.id })}
      >
        Remove music
      </button>
    </div>
  )
}
