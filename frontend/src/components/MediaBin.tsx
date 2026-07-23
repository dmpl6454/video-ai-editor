import { useEffect, useRef, useState } from 'react'
import { useStore } from '../store'
import { isMediaClip } from '../types'
import { baseName } from '../lib/paths'
import { StickerPanel } from './StickerPanel'
import { EffectsPanel } from './EffectsPanel'
import { VoRecorder } from './VoRecorder'

const AUDIO_EXTS = /\.(mp3|wav|m4a|aac|flac|ogg|oga|opus|aif|aiff)$/i

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

  const onFiles = async (files: FileList | null) => {
    if (!files) return
    for (const f of Array.from(files)) {
      // Route audio files to the music endpoint, video to the timeline.
      if (AUDIO_EXTS.test(f.name) || f.type.startsWith('audio/')) {
        await uploadAudio(f)
      } else {
        await upload(f)
      }
    }
  }

  // Unique source paths → the clip ids that reference them (for delete + count).
  const sources = new Map<string, string[]>()
  for (const t of edl?.tracks ?? []) {
    for (const c of t.clips) {
      if (isMediaClip(c)) {
        const arr = sources.get(c.src) ?? []
        arr.push(c.id)
        sources.set(c.src, arr)
      }
    }
  }

  return (
    <div className="media-bin">
      <h2>Media</h2>
      <div
        className={`dropzone${dragOver ? ' over' : ''}`}
        title="Video lands on the main video track; audio lands on the Music track"
        onDragOver={(e) => { e.preventDefault(); setDragOver(true) }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => { e.preventDefault(); setDragOver(false); void onFiles(e.dataTransfer.files) }}
        onClick={() => fileRef.current?.click()}
      >
        {uploading ? `Uploading ${progress}…` : 'Drop video or audio · or click'}
        {/* Wider accept list so HEIC/HEVC/.mov sources from iPhone & cameras pass the picker. */}
        <input
          ref={fileRef}
          type="file"
          accept="video/*,audio/*,.mov,.MOV,.mp4,.MP4,.m4v,.mkv,.webm,.avi,.MTS,.mp3,.wav,.m4a,.aac,.flac"
          hidden
          onChange={(e) => onFiles(e.target.files)}
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

      {sources.size === 0 && !uploading && (
        <div style={{ color: 'var(--text-dim)', fontSize: 11, marginBottom: 10, lineHeight: 1.5 }}>
          Your clips appear here after upload — drag one onto a timeline row
          (v1 main, v2 for picture-in-picture) to add it again.
        </div>
      )}
      {[...sources.entries()].map(([src, ids]) => (
        <div
          key={src}
          className="item"
          title={`${src}\n\nDrag onto the timeline to add another instance.`}
          draggable
          onDragStart={(e) => {
            e.dataTransfer.effectAllowed = 'copy'
            e.dataTransfer.setData('application/x-vai-src', src)
            e.dataTransfer.setData('text/plain', src)  // fallback for sniffers
          }}
          style={{ cursor: 'grab' }}
        >
          {baseName(src)}
          <div style={{ color: 'var(--text-dim)', fontSize: 10, marginTop: 4 }}>
            ×{ids.length} on timeline · drag to add
          </div>
          <button
            className="media-remove"
            title="Remove this media and all its clips from the timeline"
            onClick={async (e) => {
              e.stopPropagation()
              if (!window.confirm(`Remove ${baseName(src)} and its ${ids.length} clip(s) from the timeline?`)) return
              await dispatch('bulk_delete', { clip_ids: ids })
            }}
          >×</button>
        </div>
      ))}

      <VoRecorder />
      <MusicPanel />
      <StickerPanel />
      <EffectsPanel />
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
