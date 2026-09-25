import { useEffect, useState } from 'react'
import { useStore } from '../store'
import { Dialog } from './Dialog'

/**
 * Export progress modal. Shows while a background export job runs: a live
 * progress bar (real ffmpeg progress from /api/jobs/:id), an ETA derived from
 * elapsed/progress, and a Cancel button. Auto-download + success toast happen
 * in the store's doExport on completion; this just visualises the job.
 */
export function ExportModal() {
  const exporting = useStore((s) => s.exporting)
  const progress = useStore((s) => s.exportProgress)
  const status = useStore((s) => s.exportStatus)
  const cancelExport = useStore((s) => s.cancelExport)

  const [elapsed, setElapsed] = useState(0)
  const [cancelling, setCancelling] = useState(false)

  useEffect(() => {
    if (!exporting) {
      setElapsed(0)
      setCancelling(false)
      return
    }
    const t0 = Date.now()
    const iv = window.setInterval(() => setElapsed((Date.now() - t0) / 1000), 250)
    return () => window.clearInterval(iv)
  }, [exporting])

  if (!exporting) return null

  const pct = Math.round(progress * 100)
  const indeterminate = pct <= 0
  // ETA only becomes meaningful once a little real progress has landed.
  const eta =
    progress > 0.02 && progress < 1
      ? Math.max(0, Math.round((elapsed / progress) * (1 - progress)))
      : null
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
          {eta != null ? `~${eta}s remaining` : `${elapsed.toFixed(0)}s elapsed`}
        </span>
      </div>
    </Dialog>
  )
}
