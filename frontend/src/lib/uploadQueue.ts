// The import queue's pure parts (QA-044 / QA-094): what each pending item
// says, how long it has left, and how the whole batch reads.
//
// A long import used to show one static line — "Uploading long_12min.mp4…" —
// for minutes, with no percent, no stage, no ETA, no way to stop it and no
// sign in the bin that anything was coming. A three-file drop cleared that
// line after the first file. The store keeps one item per file (in drop
// order) and these helpers turn an item into what the Media panel shows.

export type UploadStage = 'queued' | 'uploading' | 'processing' | 'failed'

export interface UploadItem {
  id: string
  name: string
  kind: 'video' | 'audio'
  stage: UploadStage
  /** 0..1 of the CURRENT stage (bytes sent, then the server's normalise). */
  progress: number
  /** When the current stage began (ms) — the ETA is measured from here. */
  stageStartedAt: number
  /** Whether this import also lands on the timeline (QA-010 import-only). */
  addToTimeline: boolean
  error?: string
}

export interface UploadBatch { total: number; done: number }

const pct = (p: number) => `${Math.round(Math.min(1, Math.max(0, p)) * 100)}%`

/** "Waiting" / "Uploading 42%" / "Preparing 17%" / "Couldn't import". */
export function uploadStageLabel(item: UploadItem): string {
  switch (item.stage) {
    case 'queued': return 'Waiting'
    case 'uploading': return `Uploading ${pct(item.progress)}`
    case 'processing':
      if (item.kind === 'audio') return 'Adding…'
      return item.progress >= 0.999 ? 'Finishing…' : `Preparing ${pct(item.progress)}`
    case 'failed': return "Couldn't import"
  }
}

/** Seconds left in the current stage, or null until there is enough signal
 *  (3 % done and a second elapsed) for the estimate to mean anything. */
export function uploadEtaSeconds(item: UploadItem, now: number): number | null {
  if (item.stage !== 'uploading' && item.stage !== 'processing') return null
  const p = item.progress
  const elapsed = (now - item.stageStartedAt) / 1000
  if (!(p >= 0.03) || p >= 1 || elapsed < 1) return null
  return Math.max(0, Math.round((elapsed / p) * (1 - p)))
}

/** "about 40 s left" / "about 3 min left" / "" when unknown. */
export function etaLabel(seconds: number | null): string {
  if (seconds === null) return ''
  if (seconds < 60) return `about ${Math.max(1, seconds)} s left`
  return `about ${Math.round(seconds / 60)} min left`
}

/** The dropzone's line while a batch runs: "Importing 2 of 3 · clip.mp4". */
export function batchLabel(items: readonly UploadItem[], batch: UploadBatch): string {
  const active = items.find((i) => i.stage === 'uploading' || i.stage === 'processing')
  if (!active) return ''
  const n = Math.min(batch.total, batch.done + 1)
  return batch.total > 1 ? `Importing ${n} of ${batch.total} · ${active.name}` : `Importing ${active.name}`
}
