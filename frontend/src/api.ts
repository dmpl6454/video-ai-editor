// Fetch wrappers around the FastAPI backend.

import type { EDL, SessionInfo, Op } from './types'
import { responseError } from './lib/errorMessage'

const BASE = '/api'

// The Anthropic API key settings routes, written ONCE so a path that differs
// from the backend is a one-line fix rather than a hunt. Both calls TOLERATE a
// 404 (`supported: false`) instead of throwing: a build of this frontend can be
// served by a backend that predates the endpoints, and a settings affordance
// that crashes the app is worse than one that quietly isn't there.
const API_KEY_PATH = '/settings/api-key'

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

// GET /api/features — ai/features.py::feature_report over HTTP (the same
// payload the `check_features` chat tool returns). The AI panel greys a tool
// out BEFORE the click and shows `fix` verbatim instead of a 422 afterwards.
// `fix` is only present on `unavailable` entries; `packaged_app_excluded`
// marks features the .app build deliberately leaves out.
export interface FeatureEntry {
  key: string; feature: string; tools: string[]
  note?: string; fix?: string; packaged_app_excluded?: boolean
}
export interface FeatureReport {
  packaged_app: boolean; python: string; anthropic_key_set: boolean
  available: FeatureEntry[]; unavailable: FeatureEntry[]; summary: string
}

// GET /api/tools — every dispatch tool with its Anthropic-style input schema.
// `cancellable` / `reports_progress` are derived server-side from the handler
// signature (only a handler that takes `cancel_event` stops on Cancel; only one
// that takes `set_progress` ever moves off 0.0), so a UI must read them here
// rather than promise a Cancel the backend can't honour.
export interface JsonSchemaProp {
  type?: string | string[]; description?: string; default?: unknown
  enum?: unknown[]; items?: { type?: string }; minimum?: number; maximum?: number
}
export interface ToolSchema {
  name: string; description: string; category?: string
  cancellable: boolean; reports_progress: boolean
  input_schema: { type: 'object'; properties: Record<string, JsonSchemaProp>; required: string[] }
}

// Answer of both /settings/api-key calls. `supported: false` means the backend
// has no such route (404) — the UI hides the whole affordance in that case.
export interface ApiKeyStatus { configured: boolean; supported: boolean }

async function http<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method,
    headers: body ? { 'content-type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  })
  // responseError is the ONE reader of api/hardening.py's error envelope
  // (lib/errorMessage.ts) — the backend's sentence, never a bare status line.
  if (!res.ok) throw await responseError(res)
  return res.json()
}

export const api = {
  health: () => http<{ ok: boolean }>('GET', '/health'),

  // GET → is a key configured? The key itself is NEVER returned; the backend
  // answers a boolean, so nothing here can leak it into the DOM or a log.
  getApiKeyStatus: async (): Promise<ApiKeyStatus> => {
    const res = await fetch(`${BASE}${API_KEY_PATH}`)
    if (res.status === 404) return { configured: false, supported: false }
    if (!res.ok) throw await responseError(res)
    try {
      const body = (await res.json()) as { configured?: boolean }
      return { configured: !!body.configured, supported: true }
    } catch {
      // A body that isn't JSON means this path is being answered by something
      // other than the settings route (the SPA static mount, a proxy) — treat
      // it exactly like a 404.
      return { configured: false, supported: false }
    }
  },

  // POST the key. Nothing echoes it back — the response is the same boolean.
  setApiKey: async (key: string): Promise<ApiKeyStatus> => {
    const res = await fetch(`${BASE}${API_KEY_PATH}`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ key }),
    })
    if (res.status === 404) return { configured: false, supported: false }
    if (!res.ok) throw await responseError(res)
    try {
      const body = (await res.json()) as { configured?: boolean }
      return { configured: body.configured ?? true, supported: true }
    } catch {
      // A 2xx whose body is not JSON did NOT come from the settings route (an
      // older backend's SPA catch-all answers 200 with index.html). Reporting
      // success here would tell the user their key was saved when it was not,
      // so mirror the 404 arm and let the caller show 'unsupported'.
      return { configured: false, supported: false }
    }
  },

  listSessions: () => http<{ sessions: { id: string; name: string }[] }>('GET', '/sessions'),

  createSession: (name?: string) =>
    http<{ id: string; name: string }>('POST', '/sessions', { name }),

  getSession: (sid: string) => http<SessionInfo>('GET', `/sessions/${sid}`),

  deleteSession: (sid: string) => http<{ deleted: string }>('DELETE', `/sessions/${sid}`),

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
    if (!res.ok) throw await responseError(res)
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
    if (!res.ok) throw await responseError(res)
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

  // Async dispatch (202 + job id), for the handful of tools that load an ML
  // model and process every frame. Held on the sync path they pin a request
  // worker for minutes, which starves the rest of the app — the round-5
  // "becomes unresponsive" report. Poll `getJob` until status is terminal;
  // the completed job's `result` is the same payload the sync path returns.
  dispatchAsync: (sid: string, tool: string, args: Record<string, unknown> = {}) =>
    http<{ job_id: string; status: JobStatus; status_url: string }>(
      'POST',
      `/sessions/${sid}/dispatch?wait=0`,
      { tool, args }
    ),

  preview: (sid: string) =>
    http<{ path: string; cached: boolean; edl_hash: string; url: string }>(
      'POST',
      `/sessions/${sid}/preview`
    ),

  previewURL: (sid: string, hash?: string) =>
    `${BASE}/sessions/${sid}/preview.mp4${hash ? `?h=${hash}` : ''}`,

  export: (sid: string, opts: { height?: number; fps?: number; crf?: number; container?: 'mp4' | 'mov' } = {}) =>
    http<{ path: string; filename: string; url: string }>(
      'POST',
      `/sessions/${sid}/export`,
      opts
    ),

  // Async export: returns a job id immediately (202) instead of blocking the
  // request until the render finishes. Poll `getJob` until status is terminal.
  // Exports of long clips take minutes — the sync path can outlive a browser's
  // fetch timeout, which is exactly what made Export appear to "hang forever".
  exportAsync: (sid: string, opts: { height?: number; fps?: number; crf?: number; container?: 'mp4' | 'mov' } = {}) =>
    http<{ job_id: string; status: JobStatus; status_url: string }>(
      'POST',
      `/sessions/${sid}/export?wait=0`,
      opts
    ),

  getJob: (jobId: string) => http<Job>('GET', `/jobs/${jobId}`),

  cancelJob: (jobId: string) => http<Job>('POST', `/jobs/${jobId}/cancel`),

  // `refresh` re-probes the installed optional features (the panel's Refresh
  // button); otherwise the backend serves its process-lifetime cache — the
  // probes import six ai.* modules and cost ~2s cold.
  getFeatures: (refresh = false) =>
    http<FeatureReport>('GET', `/features${refresh ? '?refresh=1' : ''}`),

  getTools: () => http<{ tools: ToolSchema[] }>('GET', '/tools'),

  // Stores a .srt/.vtt/.ass in the session so `import_srt` can be dispatched
  // with a real path from the browser. Does NOT import by itself — the caller
  // follows with dispatch('import_srt', {path}) so the op log and undo see it.
  // The generic /upload can't take this: it ffmpeg-normalises everything and
  // 422s on a non-video file.
  uploadSubtitle: async (sid: string, file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    const res = await fetch(`${BASE}/sessions/${sid}/subtitle_upload`, { method: 'POST', body: fd })
    // Same reader as http(): api/hardening.py rewrites every HTTPException
    // into {error:{message, details}} — there is no `detail` key on the wire —
    // so the old detail.error parse found nothing and the user saw
    // "422 Unprocessable Entity" instead of
    // "expected a .srt, .vtt or .ass file".
    if (!res.ok) throw await responseError(res)
    return res.json() as Promise<{ path: string; name: string }>
  },

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
    if (!res.ok) throw await responseError(res)
    return res.json() as Promise<{ clip_id: string; src: string; duration: number; summary: string }>
  },

  stickerUpload: async (sid: string, file: File, addAtPlayhead = true, playhead = 0) => {
    const fd = new FormData()
    fd.append('file', file)
    fd.append('add_at_playhead', String(addAtPlayhead))
    fd.append('playhead', String(playhead))
    const res = await fetch(`${BASE}/sessions/${sid}/sticker_upload`, { method: 'POST', body: fd })
    if (!res.ok) throw await responseError(res)
    return res.json() as Promise<{ src: string; filename: string; edl_hash?: string }>
  },

  loadProject: async (file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    const res = await fetch(`${BASE}/load_project`, { method: 'POST', body: fd })
    if (!res.ok) throw await responseError(res)
    return res.json() as Promise<{ id: string }>
  },
}
