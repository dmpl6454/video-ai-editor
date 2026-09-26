import { useEffect, useRef, useState } from 'react'
import { useStore } from '../store'
import { Dialog } from './Dialog'
import { ETA_START, etaLeft, etaText, sampleEta, type EtaState } from '../lib/exportEta'

/**
 * Export progress modal. Shows while a background export job runs: a live
 * progress bar (real ffmpeg progress from /api/jobs/:id), a time-left estimate
 * from the current rate (lib/exportEta, QA-097), and a Cancel button. Auto-download + success toast happen
 * in the store's doExport on completion; this just visualises the job.
 */
export function ExportModal() {
  const exporting = useStore((s) => s.exporting)
  const progress = useStore((s) => s.exportProgress)
  const status = useStore((s) => s.exportStatus)
  const cancelExport = useStore((s) => s.cancelExport)

  const [elapsed, setElapsed] = useState(0)
  const [cancelling, setCancelling] = useState(false)
  const t0Ref = useRef(0)
  const [eta, setEta] = useState<EtaState>(ETA_START)

  useEffect(() => {
    if (!exporting) {
      setElapsed(0)
      setCancelling(false)
      setEta(ETA_START)
      return
    }
    const t0 = Date.now()
    t0Ref.current = t0
    const iv = window.setInterval(() => setElapsed((Date.now() - t0) / 1000), 250)
    return () => window.clearInterval(iv)
  }, [exporting])

  // Every progress poll is one sample of the rate (lib/exportEta).
  useEffect(() => {
    if (!exporting || !t0Ref.current) return
    setEta((s) => sampleEta(s, (Date.now() - t0Ref.current) / 1000, progress))
  }, [exporting, progress])

  if (!exporting) return null

  const pct = Math.round(progress * 100)
  const indeterminate = pct <= 0
  // Silent until 10 % and 3 s, then the current rate, counted down (QA-097).
  const left = etaText(etaLeft(eta, elapsed))
  // 'reconnecting' (QA-034): the engine stopped answering the status poll;
  // the store waits a bounded time, and Cancel still closes at once.
  const phase =
    status === 'reconnecting' ? 'Waiting for the editor engine…'
      : status === 'queued' ? 'Preparing…' : progress >= 1 ? 'Finishing…' : 'Rendering…'

  const cancel = () => {
    if (cancelling) return
    setCancelling(true)
    void cancelExport()
  }

  // On THE app dialog (components/Dialog): focus lands on Cancel, Tab stays
  // inside, the editor behind is inert, and Escape means Cancel — it used to
  // take no focus at all, so Tab walked to the Media panel under the backdrop.
  return (
    <Dialog open title="Exporting video" labelId="export-progress-title" onClose={cancel}
            showClose={false} closeOnBackdrop={false} className="export-progress-dialog"
            footer={<>
              <span className="export-modal-phase" role="status">{phase}</span>
              <span className="spacer" />
              <button className="export-cancel" disabled={cancelling} onClick={cancel}>
                {cancelling ? 'Cancelling…' : 'Cancel'}
              </button>
            </>}>
      <div className={`export-progress-track${indeterminate ? ' indeterminate' : ''}`}
           role="progressbar" aria-label="Export progress" aria-valuemin={0} aria-valuemax={100}
           aria-valuenow={indeterminate ? undefined : pct}>
        <div
          className="export-progress-fill"
          style={indeterminate ? undefined : { width: `${Math.max(3, pct)}%` }}
        />
      </div>

      <div className="export-modal-meta">
        <span className="export-pct">{indeterminate ? '…' : `${pct}%`}</span>
        <span className="export-eta">
          {left || `${elapsed.toFixed(0)} s elapsed`}
        </span>
      </div>
    </Dialog>
  )
}
