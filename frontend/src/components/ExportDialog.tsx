// Export ▾ → a proper dark Export dialog (QA-100, with the frame-rate choice
// wave A left for the UI, QA-009).
//
// It replaced a light popover of three unstyled native <select>s with no file
// name, frame rate, loudness, audio or size information — while the project
// silently applied a −16 LUFS loudness pass and the backend's YouTube preset
// was unreachable. Every choice here maps onto POST /export (lib/exportOptions
// .exportBody) except two that are PROJECT settings and say so:
//   * Loudness is the canvas' `loudness_lufs` (set_loudness_target — one undo
//     step, applied before the render when it changed);
//   * File name is what the file is SAVED as (the native Save-As box or the
//     browser download), not the render's internal `export_<hash>.mp4`.
// The location is chosen in the Save-As box after the render (in a browser,
// the downloads folder).
//
// Format also offers the SOUND alone (wave C, QA-100 remainder): Audio M4A /
// Audio WAV render no picture at all — the backend masters the mix to the
// same loudness target and −1 dBTP ceiling as a video export — so the picture
// rows (resolution, frame rate, quality) step aside while one is chosen.

import { useEffect, useId, useRef, useState } from 'react'
import { useStore } from '../store'
import { humanBytes } from '../lib/promptEvents'
import {
  EXPORT_FORMATS, LOUDNESS_TARGETS, audioDescription, defaultQuality, estimateAudioOnlyBytes, estimateBytes,
  estimateVideoKbps, exportBody, exportDimensions, exportFileName, frameRateOptions, isAudioOnly, qualityOptions,
  resolutionOptions, type CanvasLike, type ExportContainer, type QualityChoice,
} from '../lib/exportOptions'
import { formatTimecode } from '../lib/timecode'
import { Dialog } from './Dialog'
import { Icon } from './Icon'

export function ExportButton() {
  const edl = useStore((s) => s.edl)
  const sessionName = useStore((s) => s.sessionName)
  const exporting = useStore((s) => s.exporting)
  const exportStatus = useStore((s) => s.exportStatus)
  const doExport = useStore((s) => s.doExport)
  const dispatch = useStore((s) => s.dispatch)
  const btnRef = useRef<HTMLButtonElement>(null)
  const [open, setOpen] = useState(false)
  const [elapsed, setElapsed] = useState(0)

  // Live elapsed seconds on the button while an export runs (reset derived
  // during render when it ends, so the effect only ever ticks).
  if (!exporting && elapsed !== 0) setElapsed(0)
  useEffect(() => {
    if (!exporting) return
    const startedAt = Date.now()
    const id = window.setInterval(() => setElapsed(Math.floor((Date.now() - startedAt) / 1000)), 1000)
    return () => window.clearInterval(id)
  }, [exporting])

  const canvas = edl?.canvas as CanvasLike | undefined
  return (
    <>
      <button
        ref={btnRef}
        className="primary"
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen(true)}
        disabled={exporting || !edl?.duration}
        title={!edl?.duration
          ? 'Nothing to export yet — add a video to the timeline first'
          : exporting ? 'Export is already running' : 'Render the finished video'}
      >
        {exporting
          ? `Exporting${exportStatus === 'queued' ? ' (queued)' : ''}… ${elapsed}s`
          : <>Export<Icon name="chevronDown" /></>}
      </button>
      {open && canvas && (
        <ExportForm
          canvas={canvas}
          duration={edl?.duration ?? 0}
          defaultName={sessionName}
          triggerRef={btnRef}
          onClose={() => setOpen(false)}
          onExport={async (req, lufs) => {
            setOpen(false)
            btnRef.current?.focus()
            const current = canvas.loudness_lufs ?? null
            if (lufs !== undefined && lufs !== current) {
              const r = await dispatch('set_loudness_target', { lufs })
              if (!r) return   // the failure toast already fired
            }
            await doExport(req)
          }}
        />
      )}
    </>
  )
}

type Req = Parameters<ReturnType<typeof useStore.getState>['doExport']>[0]

function ExportForm({ canvas, duration, defaultName, triggerRef, onClose, onExport }: {
  canvas: CanvasLike
  duration: number
  defaultName: string
  triggerRef: React.RefObject<HTMLElement | null>
  onClose: () => void
  onExport: (req: Req, lufs: number | null | undefined) => void
}) {
  const id = useId()
  const [name, setName] = useState(defaultName)
  // 0 = the project size ("Source"), else a NAMED resolution (the short side).
  const [shortSide, setShortSide] = useState(0)
  const [fps, setFps] = useState('')            // '' = the project rate
  const [quality, setQuality] = useState<string>(String(defaultQuality(canvas)))
  const [container, setContainer] = useState<ExportContainer>('mp4')
  const [lufs, setLufs] = useState<string>(canvas.loudness_lufs == null ? 'off' : String(canvas.loudness_lufs))

  const q: QualityChoice = quality === 'platform' ? 'platform' : Number(quality)
  const rate = fps ? Number(fps) : null
  const [w, h] = exportDimensions(canvas.w, canvas.h, shortSide || null)
  const kbps = estimateVideoKbps(canvas, shortSide, q, rate)
  const audioOnly = isAudioOnly(container)
  const bytes = audioOnly ? estimateAudioOnlyBytes(container, duration) : estimateBytes(kbps, duration)
  const lufsValue = lufs === 'off' ? null : Number(lufs)
  const lufsOptions = LOUDNESS_TARGETS.some((t) => t.lufs === canvas.loudness_lufs)
    ? LOUDNESS_TARGETS
    : [{ lufs: canvas.loudness_lufs ?? null, label: `${canvas.loudness_lufs} LUFS · this project` }, ...LOUDNESS_TARGETS]

  const submit = () => onExport({
    // Audio-only: nothing about the picture applies, so nothing of it is sent.
    ...(audioOnly ? {} : exportBody(canvas, shortSide, q, rate)),
    container,
    saveAs: exportFileName(name, container),
  }, lufsValue)

  return (
    <Dialog
      open
      title={audioOnly ? 'Export audio' : 'Export video'}
      labelId={`${id}-title`}
      triggerRef={triggerRef}
      onClose={onClose}
      className="export-dialog"
      footer={(
        <>
          <span className="export-dialog-estimate" aria-live="polite">
            About <b>{humanBytes(bytes)}</b> · {audioOnly ? 'sound only' : `${w}×${h}`} · {formatTimecode(duration, canvas.fps ?? 30)}
          </span>
          <button type="button" onClick={onClose}>Cancel</button>
          <button type="button" className="primary" onClick={submit}>Export</button>
        </>
      )}
    >
      <form className="export-dialog-grid" onSubmit={(e) => { e.preventDefault(); submit() }}>
        <label htmlFor={`${id}-name`}>File name</label>
        <div className="export-dialog-name">
          <input id={`${id}-name`} type="text" value={name} spellCheck={false}
                 onChange={(e) => setName(e.target.value)} />
          <span className="export-dialog-ext">.{container}</span>
        </div>
        <span className="export-dialog-help export-dialog-full">You choose where to save it when the render finishes.</span>

        <span className="export-dialog-label" id={`${id}-fmt`}>Format</span>
        <div className="export-dialog-seg" role="radiogroup" aria-labelledby={`${id}-fmt`}>
          {EXPORT_FORMATS.map((f) => (
            <button key={f.value} type="button" role="radio" aria-checked={container === f.value}
                    title={f.title} onClick={() => setContainer(f.value)}>{f.label}</button>
          ))}
        </div>
        {audioOnly && (
          <span className="export-dialog-help export-dialog-full">
            The sound only — every lane mixed and mastered, no picture.
          </span>
        )}

        {!audioOnly && (
          <>
            <label htmlFor={`${id}-res`}>Resolution</label>
            <select id={`${id}-res`} value={shortSide} onChange={(e) => setShortSide(Number(e.target.value))}>
              {resolutionOptions(canvas).map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>

            <label htmlFor={`${id}-fps`}>Frame rate</label>
            <select id={`${id}-fps`} value={fps} onChange={(e) => setFps(e.target.value)}>
              {frameRateOptions(canvas).map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>

            <label htmlFor={`${id}-q`}>Quality</label>
            <select id={`${id}-q`} value={quality} onChange={(e) => setQuality(e.target.value)}>
              {qualityOptions(canvas).map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
            <span className="export-dialog-help export-dialog-full">
              {q === 'platform' ? 'Average' : 'About'} {(kbps / 1000).toFixed(kbps >= 10000 ? 0 : 1)} Mbps video
            </span>
          </>
        )}

        <label htmlFor={`${id}-lufs`}>Loudness</label>
        <select id={`${id}-lufs`} value={lufs} onChange={(e) => setLufs(e.target.value)}>
          {lufsOptions.map((t) => (
            <option key={String(t.lufs)} value={t.lufs == null ? 'off' : String(t.lufs)}>{t.label}</option>
          ))}
        </select>
        <span className="export-dialog-help export-dialog-full">Saved with the project; Undo reverts it. Peaks are held under −1 dBTP.</span>

        <span className="export-dialog-label">Audio</span>
        <span className="export-dialog-value">{audioDescription(container)}</span>
      </form>
    </Dialog>
  )
}
