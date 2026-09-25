import { useEffect, useState } from 'react'
import { useStore } from '../store'
import { importFiles, installWindowFileDrop } from '../lib/fileDrop'

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
 *   3. Is the ONLY importer of an OS file drop — including one that lands on
 *      the Media panel's own dropzone, which just highlights (lib/fileDrop
 *      explains the double import that two importers caused).
 *
 * It only reacts to FILE drags (dataTransfer has a "Files" type); internal
 * clip/emoji drags within the timeline are ignored so they still work.
 */
export function FileDropOverlay() {
  const upload = useStore((s) => s.upload)
  const uploadAudio = useStore((s) => s.uploadAudio)
  const [active, setActive] = useState(false)

  useEffect(() => installWindowFileDrop(window, {
    setActive,
    importFiles: (files) => { void importFiles(files, { upload, uploadAudio }) },
  }), [upload, uploadAudio])

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
