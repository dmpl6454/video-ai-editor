// Export (design §5; brief §8 "Local export dialog"): a cover preview with
// Edit cover on the left and, while a render runs, the progress card
// (Rendering… / Export complete, %, a 4 px bar, Show in Finder); on the right
// Name, Export to, the collapsible Video section (Resolution, Bit rate, Codec,
// Format, Frame rate, Optical flow, Color space), the Audio checkbox (a
// separate audio-only file) and Export GIF; a fixed footer with Duration,
// Size ≈, the dimensions, and Cancel / Export → Cancel export → Done.
//
// Every choice maps onto POST /export (lib/exportOptions.exportBody) and the
// store's export job (real ffmpeg progress, cancel, the native Save-As at the
// end). Where the render engine stops, the option says so and is not offered
// as a silent fallback (brief §8 "Export engine requirements"): the codec is
// H.264 (HEVC / ProRes are listed, disabled, with the reason), Optical flow
// and GIF are not in this build, the audio-only file is M4A or WAV.
import { useEffect, useId, useRef, useState } from 'react'
import { useStore } from '../../store'
import { toast } from '../../toast'
import { humanBytes } from '../../lib/promptEvents'
import {
  FRAME_RATES, estimateAudioOnlyBytes, estimateBytes, estimateVideoKbps, exportBody, exportDimensions, exportFileName, sameRate,
  type CanvasLike, type QualityChoice,
} from '../../lib/exportOptions'
import { BITRATES, CODECS, EXPORT_RES, exportSpec, resolutionLabel, type Bitrate, type Codec, type ExportRes } from '../../lib/exportSpec'
import { formatTimecode } from '../../lib/timecode'
import { ETA_START, etaLeft, etaText, sampleEta, type EtaState } from '../../lib/exportEta'
import { Dialog } from '../Dialog'
import { Toggle } from '../ui/Toggle'
import { Checkbox } from '../ui/Checkbox'
import { PosterFrame } from './PosterFrame'
import { Icon } from '../Icon'
import './dialogs.css'

export function ExportButton() {
  const edl = useStore((s) => s.edl)
  const exporting = useStore((s) => s.exporting)
  const exportProgress = useStore((s) => s.exportProgress)
  const btnRef = useRef<HTMLButtonElement>(null)
  const [open, setOpen] = useState(false)
  // ⌘E (keymap/uiTargets): `.topbar-pinned button.primary[aria-haspopup="dialog"]`.
  return (
    <>
      <button ref={btnRef} type="button" className="primary" aria-haspopup="dialog" aria-expanded={open} onClick={() => setOpen(true)}
              disabled={!edl?.duration && !exporting}
              title={!edl?.duration ? 'Nothing to export yet — add a video to the timeline first' : 'Render the finished video'}>
        <Icon name="export" />{exporting ? `Exporting… ${Math.round(exportProgress * 100)}%` : 'Export'}
      </button>
      {open && edl && <ExportForm canvas={edl.canvas as CanvasLike} duration={edl.duration} triggerRef={btnRef} onClose={() => setOpen(false)} />}
    </>
  )
}

function ExportForm({ canvas, duration, triggerRef, onClose }: { canvas: CanvasLike; duration: number; triggerRef: React.RefObject<HTMLElement | null>; onClose: () => void }) {
  const id = useId()
  const sessionName = useStore((s) => s.sessionName)
  const exporting = useStore((s) => s.exporting)
  const progress = useStore((s) => s.exportProgress)
  const status = useStore((s) => s.exportStatus)
  const exportError = useStore((s) => s.exportError)
  const exportLinks = useStore((s) => s.exportLinks)
  const doExport = useStore((s) => s.doExport)
  const cancelExport = useStore((s) => s.cancelExport)
  const downloadExport = useStore((s) => s.downloadExport)
  const [name, setName] = useState(sessionName)
  const [res, setRes] = useState<ExportRes>(() => EXPORT_RES.find((r) => r.short === Math.min(canvas.w, canvas.h))?.id ?? '1080P')
  const [bitrate, setBitrate] = useState<Bitrate>('Higher')
  const [customKbps, setCustomKbps] = useState(String(canvas.bitrate_kbps ?? 12000))
  const [codec, setCodec] = useState<Codec>('H.264')
  const [format, setFormat] = useState<'mp4' | 'mov'>('mp4')
  const [fps, setFps] = useState(String(FRAME_RATES.find((r) => sameRate(r.fps, canvas.fps))?.fps ?? ''))
  const [videoOpen, setVideoOpen] = useState(true)
  const [audioOpen, setAudioOpen] = useState(false)
  const [audio, setAudio] = useState(false)
  const [audioFmt, setAudioFmt] = useState<'m4a' | 'wav'>('m4a')
  const [gif, setGif] = useState(false)
  const [gifOpen, setGifOpen] = useState(false)
  // Run bookkeeping: started/finished flags flip in response to the store's
  // `exporting`; the clock and the ETA sample tick inside one interval.
  const [run, setRun] = useState<{ started: boolean; finished: boolean }>({ started: false, finished: false })
  const [clock, setClock] = useState<{ elapsed: number; eta: EtaState }>({ elapsed: 0, eta: ETA_START })
  const t0 = useRef(0)
  const progressRef = useRef(progress)
  useEffect(() => { progressRef.current = progress }, [progress])
  useEffect(() => {
    if (!exporting) {
      if (t0.current) { t0.current = 0; queueMicrotask(() => setRun((r) => ({ ...r, finished: true }))) }
      return
    }
    const start = Date.now()
    t0.current = start
    queueMicrotask(() => { setRun({ started: true, finished: false }); setClock({ elapsed: 0, eta: ETA_START }) })
    const id = window.setInterval(() => {
      const elapsed = (Date.now() - start) / 1000
      setClock((c) => ({ elapsed, eta: sampleEta(c.eta, elapsed, progressRef.current) }))
    }, 500)
    return () => window.clearInterval(id)
  }, [exporting])
  const { elapsed, eta } = clock
  const started = run.started
  const done = started && run.finished && !exportError

  const spec = exportSpec(canvas, res, bitrate, Number(customKbps))
  const rate = fps ? Number(fps) : null
  const [w, h] = exportDimensions(canvas.w, canvas.h, spec.shortSide || null)
  const quality: QualityChoice = spec.crf
  const kbps = spec.bitrateKbps ?? estimateVideoKbps(canvas, spec.shortSide, quality, rate)
  const bytes = estimateBytes(kbps, duration) + (audio ? estimateAudioOnlyBytes(audioFmt, duration) : 0)
  const alphaWarn = codec === 'HEVC (Alpha)' && format === 'mp4'
  const codecOk = codec === 'H.264'
  const pct = Math.round(progress * 100)
  const phase = status === 'reconnecting' ? 'Waiting for the editor engine…' : status === 'queued' ? 'Preparing…' : progress >= 1 ? 'Finishing…' : 'Rendering…'
  const left = etaText(etaLeft(eta, elapsed))

  const start = async () => {
    if (!codecOk || alphaWarn) return
    const body = { ...exportBody(canvas, spec.shortSide, quality, rate), ...(spec.bitrateKbps ? { bitrate_kbps: spec.bitrateKbps } : {}) }
    await doExport({ ...body, container: format, saveAs: exportFileName(name, format) })
    if (audio && !useStore.getState().exportError) {
      toast.info(`Rendering the audio-only ${audioFmt.toUpperCase()}…`)
      await doExport({ container: audioFmt, saveAs: exportFileName(name, audioFmt) })
    }
  }
  const latest = Object.values(exportLinks ?? {}).find((l) => l) as { url?: string } | undefined

  return (
    <Dialog open title="Export" labelId={`${id}-title`} triggerRef={triggerRef} onClose={onClose} className="dlg dlg-export" closeOnBackdrop={!exporting}
            footer={<>
              <span className="dlg-foot-fact">Duration <b>{formatTimecode(duration, canvas.fps)}</b></span>
              <span className="dlg-foot-fact">Size <b>≈ {humanBytes(bytes)}</b></span>
              <span className="ui-faint">{w} × {h}{rate ? ` · ${rate} fps` : ''}</span>
              <span className="spacer" />
              {exporting ? <button type="button" className="ui-btn-secondary dlg-btn" onClick={() => void cancelExport()}>Cancel export</button>
                : done ? <button type="button" className="ui-btn-primary dlg-btn" onClick={onClose}>Done</button>
                : <><button type="button" className="ui-btn-secondary dlg-btn" onClick={onClose}>Cancel</button>
                    <button type="button" className="ui-btn-primary dlg-btn" disabled={!codecOk || alphaWarn || !duration} onClick={() => void start()}>Export</button></>}
            </>}>
      <div className="dlg-export-grid">
        <div className="dlg-export-left">
          <PosterFrame />
          <button type="button" className="ui-small-btn" disabled title="Not available in this build: the cover is the first frame of the main track"><Icon name="image" />Edit cover</button>
          {(exporting || done || exportError) && (
            <div className="dlg-progress" role="status" aria-live="polite">
              <div className="dlg-progress-head"><span>{exportError ? 'Export failed' : done ? 'Export complete' : phase}</span><span style={{ fontVariantNumeric: 'tabular-nums' }}>{done ? '100' : pct}%</span></div>
              <div className="dlg-progress-bar"><div className="dlg-progress-fill" style={{ width: `${done ? 100 : Math.max(2, pct)}%` }} /></div>
              {exporting && left && <span className="ui-faint">{left}</span>}
              {exportError && <span className="ui-warn">{exportError.replace(/^\w*Error:\s*/, '')}</span>}
              {done && latest && (
                <button type="button" className="ui-small-btn" onClick={() => void downloadExport()} title="Save the rendered file"><Icon name="folder" />Save file…</button>
              )}
            </div>
          )}
        </div>
        <div className="dlg-export-right">
          <label className="dlg-row dlg-row-100"><span className="dlg-label">Name</span><input className="ui-input on-modal" value={name} onChange={(e) => setName(e.target.value)} disabled={exporting} /></label>
          <div className="dlg-row dlg-row-100"><span className="dlg-label">Export to</span>
            <div className="dlg-inline"><input className="ui-input on-modal" value="Chosen when the render finishes" readOnly aria-label="Export to" />
              <button type="button" className="ui-icon-btn is-raised" aria-label="Choose folder" disabled title="The save location is chosen in the Save As box after the render (in a browser, the downloads folder)"><Icon name="folder" /></button></div></div>
          <div className="ui-hairline" />
          <button type="button" className="dlg-section" aria-expanded={videoOpen} onClick={() => setVideoOpen((v) => !v)}>
            <span className="dlg-section-check is-on"><Icon name="check" /></span><span className="dlg-section-name">Video</span><Icon name={videoOpen ? 'chevronUp' : 'chevronDown'} className="ui-faint" />
          </button>
          {videoOpen && (
            <div className="dlg-section-body">
              <label className="dlg-row dlg-row-100"><span className="dlg-label">Resolution</span>
                <select className="ui-select on-modal" value={res} onChange={(e) => setRes(e.target.value as ExportRes)} disabled={exporting}>
                  {EXPORT_RES.map((r) => <option key={r.id} value={r.id}>{resolutionLabel(r, canvas)}</option>)}
                </select></label>
              <label className="dlg-row dlg-row-100"><span className="dlg-label">Bit rate</span>
                <div className="dlg-inline">
                  <select className="ui-select on-modal" value={bitrate} onChange={(e) => setBitrate(e.target.value as Bitrate)} disabled={exporting}>
                    {BITRATES.map((b) => <option key={b.id} value={b.id}>{b.label}</option>)}
                  </select>
                  {bitrate === 'Custom' && <><input className="ui-input on-modal dlg-num" type="number" min={100} step={100} value={customKbps} onChange={(e) => setCustomKbps(e.target.value)} aria-label="Bit rate (kbps)" /><span className="ui-faint">kbps</span></>}
                </div></label>
              <label className="dlg-row dlg-row-100"><span className="dlg-label">Codec</span>
                <select className="ui-select on-modal" value={codec} onChange={(e) => setCodec(e.target.value as Codec)} disabled={exporting}>
                  {CODECS.map((c) => <option key={c.id} value={c.id} disabled={!c.available}>{c.id}{c.available ? '' : ' — not available'}</option>)}
                </select></label>
              {!codecOk && <span className="ui-warn dlg-indent"><Icon name="warning" />{CODECS.find((c) => c.id === codec)?.why}</span>}
              <label className="dlg-row dlg-row-100"><span className="dlg-label">Format</span>
                <select className="ui-select on-modal" value={format} onChange={(e) => setFormat(e.target.value as 'mp4' | 'mov')} disabled={exporting}>
                  <option value="mov">mov</option><option value="mp4">mp4</option>
                </select></label>
              {alphaWarn && <span className="ui-warn dlg-indent"><Icon name="warning" />HEVC (Alpha) needs the mov container. Change the format to continue.</span>}
              <label className="dlg-row dlg-row-100"><span className="dlg-label">Frame rate</span>
                <select className="ui-select on-modal" value={fps} onChange={(e) => setFps(e.target.value)} disabled={exporting}>
                  <option value="">Project ({canvas.fps} fps)</option>
                  {FRAME_RATES.map((r) => <option key={r.fps} value={r.fps}>{r.label}</option>)}
                </select></label>
              <div className="dlg-row dlg-row-100"><span className="dlg-label">Optical flow</span>
                <Toggle on={false} label="Optical flow" disabled title="Not available in this build: frame-rate conversion duplicates or drops frames; Smooth slow-mo (RIFE) interpolates a clip from the inspector" onChange={() => undefined} /></div>
              <div className="dlg-row dlg-row-100"><span className="dlg-label">Color space</span><span className="ui-muted" style={{ fontSize: 12 }}>Rec. 709 SDR</span></div>
            </div>
          )}
          <div className="ui-hairline" />
          <div className="dlg-section-row">
            <Checkbox checked={audio} onChange={setAudio} disabled={exporting}><span className="dlg-section-name">Audio</span><span className="ui-faint">separate audio-only file</span></Checkbox>
            <button type="button" className="ui-icon-btn is-tiny" aria-label="Audio options" aria-expanded={audioOpen} onClick={() => setAudioOpen((v) => !v)}><Icon name={audioOpen ? 'chevronUp' : 'chevronDown'} /></button>
          </div>
          {audioOpen && (
            <label className="dlg-row dlg-row-100 dlg-indent"><span className="dlg-label">Format</span>
              <select className="ui-select on-modal" value={audioFmt} onChange={(e) => setAudioFmt(e.target.value as 'm4a' | 'wav')} disabled={exporting}>
                <option value="m4a">M4A (AAC)</option><option value="wav">WAV (24-bit)</option><option value="mp3" disabled>MP3 — not available (no MP3 encoder in this build)</option>
              </select></label>
          )}
          <div className="ui-hairline" />
          <div className="dlg-section-row">
            <Checkbox checked={gif} onChange={setGif} disabled title="Not available in this build: GIF export needs a palette pass the render pipeline does not have yet"><span className="dlg-section-name">Export GIF</span></Checkbox>
            <button type="button" className="ui-icon-btn is-tiny" aria-label="GIF options" aria-expanded={gifOpen} onClick={() => setGifOpen((v) => !v)}><Icon name={gifOpen ? 'chevronUp' : 'chevronDown'} /></button>
          </div>
          {gifOpen && (
            <label className="dlg-row dlg-row-100 dlg-indent"><span className="dlg-label">Size</span>
              <select className="ui-select on-modal" disabled><option>480 px wide</option><option>320 px wide</option></select></label>
          )}
        </div>
      </div>
    </Dialog>
  )
}
