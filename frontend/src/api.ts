// Fetch wrappers around the FastAPI backend.

import type { EDL, SessionInfo, Op } from './types'

const BASE = '/api'

export type JobStatus = 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'

export interface Job {
  id: string
  kind: string
  status: JobStatus
  progress: number          // 0..1; export reports live ffmpeg progress
  result: { path: string; filename: string; url: string } | null
  error: string | null
  created_at: number
  started_at: number | null
  completed_at: number | null
  session_id: string | null
}

async function http<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method,
    headers: body ? { 'content-type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(`${res.status} ${res.statusText}: ${text}`)
  }
  return res.json()
}

export const api = {
  health: () => http<{ ok: boolean }>('GET', '/health'),

  listSessions: () => http<{ sessions: { id: string; name: string }[] }>('GET', '/sessions'),

  createSession: (name?: string) =>
    http<{ id: string; name: string }>('POST', '/sessions', { name }),

  getSession: (sid: string) => http<SessionInfo>('GET', `/sessions/${sid}`),

  getEDL: (sid: string) => http<EDL>('GET', `/sessions/${sid}/edl`),

  getOps: (sid: string, since = 0) =>
    http<{ ops: Op[] }>('GET', `/sessions/${sid}/ops?since=${since}`),

  audioUpload: async (sid: string, file: File, opts: { addToMusic?: boolean; duck?: boolean; volumeDb?: number } = {}) => {
    const fd = new FormData()
    fd.append('file', file)
    fd.append('add_to_music', String(opts.addToMusic ?? true))
    fd.append('duck', String(opts.duck ?? true))
    fd.append('volume_db', String(opts.volumeDb ?? -12))
    const res = await fetch(`${BASE}/sessions/${sid}/audio_upload`, { method: 'POST', body: fd })
    if (!res.ok) {
      let msg = `${res.status} ${res.statusText}`
      try {
        const body = await res.json()
        if (body?.detail?.error) msg = body.detail.error
        else if (typeof body?.detail === 'string') msg = body.detail
      } catch {}
      throw new Error(msg)
    }
    return res.json() as Promise<{ src: string; duration: number; edl_hash: string }>
  },

  upload: async (sid: string, file: File, addToTimeline = true,
                 opts: { transcribe?: boolean; whisperModel?: string } = {}) => {
    const fd = new FormData()
    fd.append('file', file)
    fd.append('add_to_timeline', String(addToTimeline))
    fd.append('transcribe', String(opts.transcribe ?? true))
    if (opts.whisperModel) fd.append('whisper_model', opts.whisperModel)
    const res = await fetch(`${BASE}/sessions/${sid}/upload`, { method: 'POST', body: fd })
    if (!res.ok) {
      let msg = `${res.status} ${res.statusText}`
      try {
        const body = await res.json()
        if (body?.detail?.error) msg = body.detail.error
        else if (typeof body?.detail === 'string') msg = body.detail
      } catch {}
      throw new Error(msg)
    }
    return res.json() as Promise<{
      src: string
      normalized: string
      duration: number
      probe: { duration: number }
      edl_hash: string
    }>
  },

  dispatch: <T = unknown>(sid: string, tool: string, args: Record<string, unknown> = {}) =>
    http<{ result: T; edl_hash: string; op: Op | null }>(
      'POST',
      `/sessions/${sid}/dispatch`,
      { tool, args }
    ),

  preview: (sid: string) =>
    http<{ path: string; cached: boolean; edl_hash: string; url: string }>(
      'POST',
      `/sessions/${sid}/preview`
    ),

  previewURL: (sid: string, hash?: string) =>
    `${BASE}/sessions/${sid}/preview.mp4${hash ? `?h=${hash}` : ''}`,

  export: (sid: string, opts: { height?: number; fps?: number; crf?: number } = {}) =>
    http<{ path: string; filename: string; url: string }>(
      'POST',
      `/sessions/${sid}/export`,
      opts
    ),

  // Async export: returns a job id immediately (202) instead of blocking the
  // request until the render finishes. Poll `getJob` until status is terminal.
  // Exports of long clips take minutes — the sync path can outlive a browser's
  // fetch timeout, which is exactly what made Export appear to "hang forever".
  exportAsync: (sid: string, opts: { height?: number; fps?: number; crf?: number } = {}) =>
    http<{ job_id: string; status: JobStatus; status_url: string }>(
      'POST',
      `/sessions/${sid}/export?wait=0`,
      opts
    ),

  getJob: (jobId: string) => http<Job>('GET', `/jobs/${jobId}`),

  cancelJob: (jobId: string) => http<Job>('POST', `/jobs/${jobId}/cancel`),

  waveform: (sid: string, src: string, peaksPerSec = 50) =>
    http<{ peaks: number[]; peaks_per_sec: number; duration: number }>(
      'GET',
      `/sessions/${sid}/waveform?src=${encodeURIComponent(src)}&peaks_per_sec=${peaksPerSec}`
    ),

  saveProject: (sid: string) =>
    http<{ path: string; filename: string; url: string; size: number }>(
      'POST', `/sessions/${sid}/save_project`
    ),

  voRecord: async (sid: string, blob: Blob, start: number, gainDb = 0) => {
    const fd = new FormData()
    const filename = blob.type.includes('webm') ? 'vo.webm'
                   : blob.type.includes('wav')  ? 'vo.wav'
                   : 'vo.m4a'
    fd.append('file', new File([blob], filename, { type: blob.type || 'audio/webm' }))
    fd.append('start', String(start))
    fd.append('gain_db', String(gainDb))
    const res = await fetch(`${BASE}/sessions/${sid}/vo_record`, { method: 'POST', body: fd })
    if (!res.ok) {
      let msg = `${res.status} ${res.statusText}`
      try {
        const body = await res.json()
        if (body?.detail?.error) msg = body.detail.error
        else if (typeof body?.detail === 'string') msg = body.detail
      } catch {}
      throw new Error(msg)
    }
    return res.json() as Promise<{ clip_id: string; src: string; duration: number; summary: string }>
  },

  stickerUpload: async (sid: string, file: File, addAtPlayhead = true, playhead = 0) => {
    const fd = new FormData()
    fd.append('file', file)
    fd.append('add_at_playhead', String(addAtPlayhead))
    fd.append('playhead', String(playhead))
    const res = await fetch(`${BASE}/sessions/${sid}/sticker_upload`, { method: 'POST', body: fd })
    if (!res.ok) {
      let msg = `${res.status} ${res.statusText}`
      try {
        const body = await res.json()
        if (body?.detail) msg = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
      } catch {}
      throw new Error(msg)
    }
    return res.json() as Promise<{ src: string; filename: string; edl_hash?: string }>
  },

  loadProject: async (file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    const res = await fetch(`${BASE}/load_project`, { method: 'POST', body: fd })
    if (!res.ok) {
      let msg = `${res.status} ${res.statusText}`
      try {
        const body = await res.json()
        if (body?.detail) msg = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
      } catch {}
      throw new Error(msg)
    }
    return res.json() as Promise<{ id: string }>
  },
}
