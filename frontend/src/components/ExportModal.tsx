import { useEffect, useState } from 'react'
import { useStore } from '../store'

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
  const phase =
    status === 'queued' ? 'Preparing…' : progress >= 1 ? 'Finishing…' : 'Rendering…'

  return (
    <div className="modal-backdrop export-backdrop">
      <div className="export-modal" role="dialog" aria-label="Exporting video">
        <div className="export-modal-head">
          <span className="export-modal-title">Exporting video</span>
          <span className="export-modal-phase">{phase}</span>
        </div>

        <div className={`export-progress-track${indeterminate ? ' indeterminate' : ''}`}>
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

        <div className="export-modal-actions">
          <button
            className="export-cancel"
            disabled={cancelling}
            onClick={() => {
              setCancelling(true)
              void cancelExport()
            }}
          >
            {cancelling ? 'Cancelling…' : 'Cancel'}
          </button>
        </div>
      </div>
    </div>
  )
}
