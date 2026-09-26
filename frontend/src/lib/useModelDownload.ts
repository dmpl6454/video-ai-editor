// One local-model download (or removal) as a job the UI can watch: start it,
// poll /api/jobs/{id} for progress, cancel it. Shared by the brain popover
// (components/BrainBadge) and Settings › Models, which used to be one inline
// copy in BrainBadge. The routes are loopback-only on the backend.

import { useCallback, useState } from 'react'
import { api } from '../api'
import { errorMessage } from '../store'

const JOB_POLL_MS = 700

export type DlState =
  | { status: 'idle' }
  | { status: 'running'; jobId: string; progress: number; id: string; action: 'download' | 'delete' }
  | { status: 'done'; id: string; action: 'download' | 'delete' }
  | { status: 'error'; message: string }

export function useModelDownload(onDone: () => unknown) {
  const [dl, setDl] = useState<DlState>({ status: 'idle' })

  const run = useCallback(async (id: string, action: 'download' | 'delete') => {
    setDl({ status: 'running', jobId: '', progress: 0, id, action })
    try {
      const { job_id } = action === 'download' ? await api.downloadModel(id) : await api.deleteModel(id)
      setDl({ status: 'running', jobId: job_id, progress: 0, id, action })
      for (;;) {
        await new Promise((r) => setTimeout(r, JOB_POLL_MS))
        const job = await api.getJob(job_id)
        if (job.status === 'completed') { setDl({ status: 'done', id, action }); await onDone(); return }
        if (job.status === 'failed') { setDl({ status: 'error', message: job.error || `${action} failed` }); return }
        if (job.status === 'cancelled') { setDl({ status: 'idle' }); return }
        setDl({ status: 'running', jobId: job_id, progress: job.progress ?? 0, id, action })
      }
    } catch (e) {
      setDl({ status: 'error', message: errorMessage(e) })
    }
  }, [onDone])

  const cancel = useCallback(async () => {
    if (dl.status !== 'running' || !dl.jobId) return
    try { await api.cancelJob(dl.jobId) } catch (e) { setDl({ status: 'error', message: errorMessage(e) }) }
  }, [dl])

  return { dl, start: (id: string) => run(id, 'download'), remove: (id: string) => run(id, 'delete'), cancel }
}
