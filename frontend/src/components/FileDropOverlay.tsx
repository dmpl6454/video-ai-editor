import { useEffect, useRef, useState } from 'react'
import { useStore } from '../store'

const AUDIO_EXTS = /\.(mp3|wav|m4a|aac|flac|ogg|oga|opus|aif|aiff)$/i

/**
 * Global file drag-and-drop.
 *
 * Without window-level handlers, dropping a file anywhere except the small
 * Media-bin box makes the browser navigate to / open the file, blowing away
 * the SPA — which reads as "drag and drop is broken". This component:
 *   1. Adds window dragover/drop preventDefault so a stray drop never
 *      navigates.
 *   2. Shows a full-window overlay while files are dragged in, so the WHOLE
 *      window is a drop target — drop anywhere to import.
 *   3. Routes each dropped file to the right uploader (audio vs video).
 *
 * It only reacts to FILE drags (dataTransfer has a "Files" type); internal
 * clip/emoji drags within the timeline are ignored so they still work.
 */
export function FileDropOverlay() {
  const upload = useStore((s) => s.upload)
  const uploadAudio = useStore((s) => s.uploadAudio)
  const [active, setActive] = useState(false)
  // dragenter/dragleave fire for every child element, so track depth to know
  // when the cursor has truly left the window.
  const depth = useRef(0)

  useEffect(() => {
    const isFileDrag = (e: DragEvent) =>
      !!e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files')

    const onDragEnter = (e: DragEvent) => {
      if (!isFileDrag(e)) return
      e.preventDefault()
      depth.current += 1
      setActive(true)
    }
    const onDragOver = (e: DragEvent) => {
      if (!isFileDrag(e)) return
      e.preventDefault()
      if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy'
    }
    const onDragLeave = (e: DragEvent) => {
      if (!isFileDrag(e)) return
      depth.current = Math.max(0, depth.current - 1)
      if (depth.current === 0) setActive(false)
    }
    const onDrop = (e: DragEvent) => {
      if (!isFileDrag(e)) return
      e.preventDefault()
      depth.current = 0
      setActive(false)
      const files = e.dataTransfer?.files
      if (!files || !files.length) return
      for (const f of Array.from(files)) {
        if (AUDIO_EXTS.test(f.name) || f.type.startsWith('audio/')) {
          void uploadAudio(f)
        } else {
          void upload(f)
        }
      }
    }

    window.addEventListener('dragenter', onDragEnter)
    window.addEventListener('dragover', onDragOver)
    window.addEventListener('dragleave', onDragLeave)
    window.addEventListener('drop', onDrop)
    return () => {
      window.removeEventListener('dragenter', onDragEnter)
      window.removeEventListener('dragover', onDragOver)
      window.removeEventListener('dragleave', onDragLeave)
      window.removeEventListener('drop', onDrop)
    }
  }, [upload, uploadAudio])

  if (!active) return null
  return (
    <div
      style={{
        position: 'fixed', inset: 0, zIndex: 9999,
        background: 'rgba(10,12,20,0.78)', backdropFilter: 'blur(3px)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        pointerEvents: 'none',
      }}
    >
      <div style={{
        border: '3px dashed var(--accent, #6c8cff)', borderRadius: 18,
        padding: '48px 72px', textAlign: 'center', color: '#fff',
        background: 'rgba(0,0,0,0.35)',
      }}>
        <div style={{ fontSize: 44, marginBottom: 10 }}>🎬</div>
        <div style={{ fontSize: 20, fontWeight: 700 }}>Drop to import</div>
        <div style={{ fontSize: 13, opacity: 0.7, marginTop: 6 }}>
          video or audio · anywhere in the window
        </div>
      </div>
    </div>
  )
}
