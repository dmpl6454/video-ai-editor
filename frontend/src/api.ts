// Fetch wrappers around the FastAPI backend.

import type { ImportAnswer } from './lib/importFollowUp'
import type { DownloadReport } from './lib/modelDownloads'
import type { MediaToolsStatus } from './lib/mediaTools'
import type { KeyStatus } from './lib/settingsModel'
import type { EDL, SessionInfo, Op, MediaItem } from './types'
import {
  EngineOfflineError, isAbort, isGatewayFailure, reportEngineReachable, reportEngineUnreachable,
} from './lib/connection'

const BASE = '/api'

export type JobStatus = 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'

export interface Job {
  id: string
  kind: string
  status: JobStatus
  progress: number          // 0..1; export reports live ffmpeg progress
  // `edl_hash` (export jobs): the timeline the file was rendered from — the
  // toolbar's "↓ MP4 (outdated)" check compares it with the session's current one.
  result: { path: string; filename: string; url: string; edl_hash?: string } | null
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

// GET /api/version — the only endpoint the TopBar hits on boot, and therefore
// the only channel that tells the frontend which optional affordances this
// build actually has. `version` and `build` feed the version badge.
//
// `phone_pairing` mirrors the backend's `phone_pairing_enabled()`
// (api/pairing.py, env `VAE_PHONE_PAIRING`). It is FALSE in the shipped
// desktop build: the iPhone / local-network pairing feature is TEMPORARILY
// gated off behind that one reversible flag so the editor ships as a normal
// standalone editor, and a future release turns it back on. Optional in the
// type because an older backend omits it, and a missing key must read as off
// — lib/versionInfo.ts owns that normalization.
export interface AppVersion {
  version: string
  build: string
  phone_pairing?: boolean
}

// --- Phone pairing (api/pairing.py) ---------------------------------------
// KEPT ON PURPOSE, unreachable in the shipped build but NOT dead. These types
// and the pair* client functions below describe /api/pair/*, which the backend
// answers with 404 while `phone_pairing` is false. Their only consumer is
// PhonePanel.tsx, and TopBar mounts PhonePanel exactly when /api/version reports
// `phone_pairing: true` — so with the shipped flag off nothing here is called,
// and the release that flips `VAE_PHONE_PAIRING` back on brings the panel and
// these calls back with no frontend change at all. That conditional, not a
// deletion, is the whole design: do not remove them as "dead code".
//
// One paired iPhone, as the Mac has it recorded. `last_seen` is 0 until the
// device makes its first authenticated request.
export interface PairDevice {
  id: string; name: string; created_at: number; last_seen: number
}

// GET /api/pair/info — everything the Phone panel renders. `hosts` is
// private-address-only and best-first; empty means this Mac is not on a LAN
// right now, which is a real state the panel has to explain rather than hide.
export interface PairInfo {
  version: string; lan_enabled: boolean; auth_required: boolean
  job_workers: number; max_upload_bytes: number; media_token_ttl_s: number
  bound_public: boolean; hosts: string[]; devices: PairDevice[]
  pending_codes: number; settings_path: string; code_ttl_s: number
}

// POST /api/pair/lan. `allowed_roots_hint` is written by the backend to be
// shown VERBATIM — the panel must not paraphrase which folders a connected
// phone can reach, because that sentence is the security promise.
export interface PairLanResult {
  lan_enabled: boolean; bound_public: boolean; restart_required: boolean
  auth_required: boolean; hosts: string[]; allowed_roots_hint: string
}

// POST /api/pair/new. `payload` is the exact string to put in the QR — built
// by api/pairing.py::pair_payload, which is the single source of truth for the
// grammar. Never reassemble it here; a phone that parses a paraphrase is a
// phone pointed at the wrong Mac.
export interface PairCode {
  code: string; host: string; hosts: string[]; port: number
  payload: string; expires_in_s: number
}

// --- Prompt Editor (api/prompt_routes.py; spec §4.7) ------------------------
// The SSE routes return the raw `Response` so lib/promptEvents.readSseStream
// can consume the body; the JSON routes go through http() like everything
// else. Event shapes live in lib/promptEvents.ts.

export interface PromptBody {
  message: string
  selection?: string | null
  multi_selection?: string[]
  playhead?: number | null
  brain?: string | null
  // Re-attach to a run that is already executing (reload, second tab): the
  // route replays its event bus from the start, so the client sees the plan
  // again and every step that has landed since. Execution never depended on
  // this connection — a run outlives its stream (spec §4.2).
  resume_run?: string | null
}

// `<session>/prompt_run.json` (agent/prompt/runlog.py) — the current or last
// run, for reconnect after a reload. Every field is optional on the client
// side: an older or partial record must render as "a run happened" rather
// than crash the bar. Statuses: planning | running | verifying | done |
// failed | cancelled | clarify.
export interface PromptRunRecord {
  run_id?: string
  plan_id?: string
  status?: string
  started?: number
  ended?: number | null
  prompt?: string
  brain?: string
  steps?: unknown[]
  verify?: unknown
  reply?: string | null
  op?: unknown
  error?: string | null
  events?: number
}

// `GET …/prompt/run` wraps the record: `live` says the process still holds
// the run (its event bus can be replayed with `resume_run`); a record whose
// status is still "running" with `live:false` is a run the backend lost —
// a restart mid-run — and the bar must say so rather than spin forever.
export interface PromptRunEnvelope {
  run: PromptRunRecord | null
  live?: boolean
  replayable?: boolean
}

// `GET …/prompt/pending` — the clarification waiting for an answer, if any
// (agent/prompt/pending.py). Restored on reload so the card reappears.
export interface PromptPending {
  token: string
  plan_id?: string
  prompt?: string | null
  brain?: string | null
  questions: unknown[]
  expires_in_s?: number
}

export interface PromptModelRow {
  id: string; installed: boolean; snapshot_path?: string | null
  bytes_on_disk?: number; expected_bytes?: number; free_bytes?: number
}
export interface PromptModels { tier?: string | null; models: PromptModelRow[] }

// `X-VAE-Client: 1` is a SECURITY CONTROL, not a label. api/auth.py requires it
// on every non-media request once LAN mode is armed, for one reason: no <img>,
// <form>, or plain <script> can set a custom header, so demanding one forces a
// CORS preflight that the backend's origin allowlist then denies. That is what
// stops a web page the user happens to be visiting from driving their editor
// through the same LAN endpoint the phone uses. The desktop sends it too — it
// is trusted by address rather than by this header, but a single code path is
// worth more than an exemption nobody remembers.
const CLIENT_HEADERS: Record<string, string> = { 'X-VAE-Client': '1' }

// Every non-2xx thrown from this module carries ONE shape:
//   Error("<status> <statusText>: <raw response body>")
// store.errorMessage() parses the JSON tail out of that string and prefers
// the hardening envelope's sentence ({error:{message}} / {error:{details:
// {message}}}) before a legacy FastAPI {detail}. The multipart helpers below
// each used to carry a private copy of an older parse that read `body.detail`
// only — but api/hardening.py rewrites EVERY HTTPException into {error:{...}},
// so there is no `detail` on the wire and every one of them fell back to the
// bare status line. That is how Open on a rejected .vae toasted
// "415 Unsupported Media Type" instead of the backend's reason. One helper,
// one contract; the body rides along RAW (not pre-parsed) so errorMessage()
// stays the single place that knows the envelope. An empty body yields the
// bare status line rather than a dangling colon.
async function apiError(res: Response): Promise<Error> {
  const head = `${res.status} ${res.statusText}`
  const text = await res.text().catch(() => '')
  // QA-109: a proxy's 502/504 (or the dev proxy's bare 500) means the engine
  // is not there — not a server error to toast verbatim.
  if (isGatewayFailure(res.status, text)) {
    reportEngineUnreachable()
    return new EngineOfflineError()
  }
  reportEngineReachable()
  return new Error(text ? `${head}: ${text}` : head)
}

/**
 * fetch() that reports reachability (QA-109): a network failure marks the
 * engine offline and throws EngineOfflineError instead of the browser's raw
 * "Failed to fetch"; any answer from the engine marks it online. An abort
 * (a superseded preview) is passed through untouched.
 */
async function send(url: string, init: RequestInit): Promise<Response> {
  let res: Response
  try {
    res = await fetch(url, init)
  } catch (e) {
    if (isAbort(e)) throw e
    reportEngineUnreachable()
    throw new EngineOfflineError()
  }
  if (res.status < 500) reportEngineReachable()   // a 5xx is judged by apiError
  return res
}

async function http<T>(method: string, path: string, body?: unknown,
                       signal?: AbortSignal): Promise<T> {
  const res = await send(`${BASE}${path}`, {
    method,
    headers: body ? { ...CLIENT_HEADERS, 'content-type': 'application/json' } : CLIENT_HEADERS,
    body: body ? JSON.stringify(body) : undefined,
    signal,
  })
  if (!res.ok) throw await apiError(res)
  return res.json()
}

/** Progress of the BYTES of an upload (QA-044). */
export interface UploadBytes { loaded: number; total: number }

/**
 * POST a multipart body and report upload progress. fetch() cannot report
 * request-body progress, which is why a 12-minute import showed one static
 * line for minutes; XMLHttpRequest can. Falls back to fetch where there is no
 * XHR (tests in node). Resolves with a Response so callers keep one contract.
 */
function postForm(url: string, fd: FormData,
                  opts: { onBytes?: (p: UploadBytes) => void; signal?: AbortSignal } = {}): Promise<Response> {
  if (typeof XMLHttpRequest === 'undefined') return send(url, { method: 'POST', body: fd, signal: opts.signal })
  return new Promise<Response>((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', url)
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) opts.onBytes?.({ loaded: e.loaded, total: e.total })
    }
    const onAbort = () => xhr.abort()
    opts.signal?.addEventListener('abort', onAbort, { once: true })
    const done = () => opts.signal?.removeEventListener('abort', onAbort)
    xhr.onload = () => {
      done()
      const res = new Response(xhr.status === 204 ? null : xhr.responseText,
                               { status: xhr.status, statusText: xhr.statusText,
                                 headers: { 'content-type': xhr.getResponseHeader('content-type') ?? 'application/json' } })
      if (res.ok || res.status < 500) reportEngineReachable()
      resolve(res)
    }
    xhr.onerror = () => { done(); reportEngineUnreachable(); reject(new EngineOfflineError()) }
    xhr.onabort = () => { done(); reject(new DOMException('The import was cancelled.', 'AbortError')) }
    xhr.send(fd)
  })
}

const UPLOAD_POLL_MS = 400

export const api = {
  // `media_tools` (QA-108): is ffmpeg/ffprobe installed — MediaToolsBanner.
  // `upload_limit` (QA-114): "free_space" for the desktop's own imports.
  health: () => http<{
    ok: boolean; max_upload_bytes?: number; upload_limit?: 'fixed' | 'free_space'
    media_tools?: MediaToolsStatus
  }>('GET', '/health'),

  // `poster` (QA-099-THUMBS): the picker row's frame URL, versioned by the
  // project's EDL, or null when the project has nothing to show yet.
  listSessions: () =>
    http<{ sessions: { id: string; name: string; modified_at?: number; poster?: string | null }[] }>('GET', '/sessions'),

  createSession: (name?: string) =>
    http<{ id: string; name: string }>('POST', '/sessions', { name }),

  getSession: (sid: string) => http<SessionInfo>('GET', `/sessions/${sid}`),

  deleteSession: (sid: string) => http<{ deleted: string }>('DELETE', `/sessions/${sid}`),

  // QA-099: rename a project. 400 for an empty or over-long name.
  renameSession: (sid: string, name: string) =>
    http<{ id: string; name: string }>('PATCH', `/sessions/${sid}`, { name }),

  // QA-105: the session's current EDL hash, cheaply — polled (and asked on
  // focus) so a window notices edits made elsewhere. Doubles as the liveness
  // signal (QA-109).
  sessionHead: (sid: string) =>
    http<{ id: string; edl_hash: string; ops: number; redo_available: boolean }>('GET', `/sessions/${sid}/head`),

  getEDL: (sid: string) => http<EDL>('GET', `/sessions/${sid}/edl`),

  getOps: (sid: string, since = 0) =>
    http<{ ops: Op[] }>('GET', `/sessions/${sid}/ops?since=${since}`),

  // The project's media library (media_library.py): every import, with how
  // many timeline clips use it. Independent of the timeline (QA-010).
  listMedia: (sid: string) => http<{ media: MediaItem[] }>('GET', `/sessions/${sid}/media`),

  // Take an item out of the bin; its file stays on disk. 409 while clips use it.
  removeMedia: (sid: string, mediaId: string) =>
    http<{ removed: string; name: string }>('DELETE', `/sessions/${sid}/media/${mediaId}`),

  audioUpload: async (sid: string, file: File, opts: { addToMusic?: boolean; duck?: boolean; volumeDb?: number
                                                     onBytes?: (p: UploadBytes) => void; signal?: AbortSignal } = {}) => {
    const fd = new FormData()
    fd.append('file', file)
    fd.append('add_to_music', String(opts.addToMusic ?? true))
    // QA-083: sent only when the caller chose. Ducking is a lane setting the
    // user may have turned off; the server keeps it unless told otherwise.
    if (opts.duck !== undefined) fd.append('duck', String(opts.duck))
    fd.append('volume_db', String(opts.volumeDb ?? -12))
    // QA-044: XHR for byte progress + cancel (postForm).
    const res = await postForm(`${BASE}/sessions/${sid}/audio_upload`, fd, { onBytes: opts.onBytes, signal: opts.signal })
    if (!res.ok) throw await apiError(res)
    // `past_video_s` etc.: where it landed and how far it runs past the
    // picture (QA-083) — lib/importFollowUp turns that into "Trim to video".
    return res.json() as Promise<{ src: string; duration: number; edl_hash: string } & ImportAnswer>
  },

  // QA-095: replace an offline item's file. The server imports the new file
  // like any import of its kind and relinks every clip that played the
  // missing one — one undoable edit.
  relinkMedia: async (sid: string, mediaId: string, file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    const res = await fetch(`${BASE}/sessions/${sid}/media/${mediaId}/relink`, { method: 'POST', body: fd })
    if (!res.ok) throw await apiError(res)
    return res.json() as Promise<{ relinked: string[]; name: string; summary: string; edl_hash: string }>
  },

  upload: async (sid: string, file: File, addToTimeline = true,
                 opts: { transcribe?: boolean; whisperModel?: string;
                         onProgress?: (fraction: number) => void
                         // QA-044: bytes-on-the-wire progress, the server job
                         // id once the bytes are in (so Cancel can reach the
                         // normalise), and an abort for the upload itself.
                         onBytes?: (p: UploadBytes) => void
                         onJob?: (jobId: string) => void
                         signal?: AbortSignal } = {}) => {
    type UploadResult = {
      src: string
      normalized: string
      display_name?: string
      duration: number
      probe: { duration: number }
      edl_hash: string
      notices?: string[]
    } & ImportAnswer
    const fd = new FormData()
    fd.append('file', file)
    fd.append('add_to_timeline', String(addToTimeline))
    fd.append('transcribe', String(opts.transcribe ?? true))
    if (opts.whisperModel) fd.append('whisper_model', opts.whisperModel)
    // QA-007: `wait=0` — the server answers 202 {job_id} as soon as the bytes
    // are on disk and normalises in a background job (it used to normalise on
    // its event loop, freezing every other request for the whole import).
    // Polling the job gives the bin real progress. A server that predates the
    // job path answers 200 with the result, handled the same way.
    const res = await postForm(`${BASE}/sessions/${sid}/upload?wait=0`, fd,
                               { onBytes: opts.onBytes, signal: opts.signal })
    if (!res.ok) throw await apiError(res)
    if (res.status !== 202) return res.json() as Promise<UploadResult>
    const { job_id } = (await res.json()) as { job_id: string }
    opts.onJob?.(job_id)
    for (;;) {
      await new Promise((r) => setTimeout(r, UPLOAD_POLL_MS))
      const job = await api.getJob(job_id)
      opts.onProgress?.(job.progress ?? 0)
      if (job.status === 'completed') return job.result as unknown as UploadResult
      if (job.status === 'failed' || job.status === 'cancelled') {
        throw new Error(job.error || `${file.name}: import ${job.status}`)
      }
    }
  },

  // `baseHash` (QA-105): the EDL hash this window's view was built from; the
  // server answers 409 `stale_edl` instead of editing a timeline that changed
  // underneath it. Omitted → no check (the legacy contract).
  dispatch: <T = unknown>(sid: string, tool: string, args: Record<string, unknown> = {},
                          baseHash?: string | null) =>
    http<{ result: T; edl_hash: string; op: Op | null }>(
      'POST',
      `/sessions/${sid}/dispatch`,
      baseHash ? { tool, args, base_hash: baseHash } : { tool, args }
    ),

  // Async dispatch (202 + job id), for the handful of tools that load an ML
  // model and process every frame. Held on the sync path they pin a request
  // worker for minutes, which starves the rest of the app — the round-5
  // "becomes unresponsive" report. Poll `getJob` until status is terminal;
  // the completed job's `result` is the same payload the sync path returns.
  dispatchAsync: (sid: string, tool: string, args: Record<string, unknown> = {},
                  baseHash?: string | null) =>
    http<{ job_id: string; status: JobStatus; status_url: string }>(
      'POST',
      `/sessions/${sid}/dispatch?wait=0`,
      baseHash ? { tool, args, base_hash: baseHash } : { tool, args }
    ),

  // `signal` aborts the request when a newer render supersedes it (QA-004).
  // The server cancels the superseded render itself when the newer request
  // arrives; aborting here just stops the browser waiting on (and holding a
  // connection for) an answer nobody will use.
  preview: (sid: string, signal?: AbortSignal) =>
    http<{ path: string; cached: boolean; edl_hash: string; url: string }>(
      'POST',
      `/sessions/${sid}/preview`,
      undefined,
      signal,
    ),

  previewURL: (sid: string, hash?: string) =>
    `${BASE}/sessions/${sid}/preview.mp4${hash ? `?h=${hash}` : ''}`,

  // `height` is a NAMED resolution (the canvas's short side — QA-025);
  // `bitrate_kbps` 0 = encode by crf even when the project has a platform
  // target, omitted = use the project's target (QA-027).
  export: (sid: string, opts: { height?: number; fps?: number; crf?: number; container?: 'mp4' | 'mov' | 'm4a' | 'wav'; bitrate_kbps?: number } = {}) =>
    http<{ path: string; filename: string; url: string }>(
      'POST',
      `/sessions/${sid}/export`,
      opts
    ),

  // Async export: returns a job id immediately (202) instead of blocking the
  // request until the render finishes. Poll `getJob` until status is terminal.
  // Exports of long clips take minutes — the sync path can outlive a browser's
  // fetch timeout, which is exactly what made Export appear to "hang forever".
  exportAsync: (sid: string, opts: { height?: number; fps?: number; crf?: number; container?: 'mp4' | 'mov' | 'm4a' | 'wav'; bitrate_kbps?: number } = {}) =>
    http<{ job_id: string; status: JobStatus; status_url: string }>(
      'POST',
      `/sessions/${sid}/export?wait=0`,
      opts
    ),

  getJob: (jobId: string) => http<Job>('GET', `/jobs/${jobId}`),

  cancelJob: (jobId: string) => http<Job>('POST', `/jobs/${jobId}/cancel`),

  // Which tools download model weights on their next run, and how much
  // (QA-065, ai/weights.py). Existence checks only — cheap to ask before a run.
  getDownloads: () => http<{ downloads: DownloadReport }>('GET', '/downloads'),

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
    // Same contract as http() — see apiError for why the raw body rides along.
    if (!res.ok) throw await apiError(res)
    return res.json() as Promise<{ path: string; name: string }>
  },

  // `peaks_l`/`peaks_r`: each side's own peaks for a 2+ channel source (QA-122).
  waveform: (sid: string, src: string, peaksPerSec = 50) =>
    http<{ peaks: number[]; peaks_per_sec: number; duration: number; peaks_l?: number[]; peaks_r?: number[] }>(
      'GET',
      `/sessions/${sid}/waveform?src=${encodeURIComponent(src)}&peaks_per_sec=${peaksPerSec}`
    ),

  // `missing`/`warning` (QA-096): media that could not be bundled because
  // its file is gone — the saved project is not self-contained.
  saveProject: (sid: string) =>
    http<{ path: string; filename: string; url: string; size: number
           missing?: { name: string; src: string }[]; warning?: string }>(
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
    if (!res.ok) throw await apiError(res)
    return res.json() as Promise<{ clip_id: string; src: string; duration: number; summary: string }>
  },

  stickerUpload: async (sid: string, file: File, addAtPlayhead = true, playhead = 0) => {
    const fd = new FormData()
    fd.append('file', file)
    fd.append('add_at_playhead', String(addAtPlayhead))
    fd.append('playhead', String(playhead))
    const res = await fetch(`${BASE}/sessions/${sid}/sticker_upload`, { method: 'POST', body: fd })
    if (!res.ok) throw await apiError(res)
    return res.json() as Promise<{ src: string; filename: string; edl_hash?: string; sticker_id?: string }>
  },

  loadProject: async (file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    const res = await fetch(`${BASE}/load_project`, { method: 'POST', body: fd })
    if (!res.ok) throw await apiError(res)
    return res.json() as Promise<{ id: string }>
  },

  // --- Phone pairing (api/pair_routes.py) ---------------------------------
  // Every one of these is loopback-only on the backend: a paired phone must not
  // be able to pair a SECOND phone, arm LAN mode, or revoke another device. So
  // they exist here, in the desktop's own client, and nowhere else.

  pairInfo: () => http<PairInfo>('GET', '/pair/info'),

  // Arming LAN mode puts this Mac's editor on the local network. `restart_required`
  // comes back true whenever the answer changes what the socket should be bound
  // to — the bind address is chosen once, before uvicorn starts (desktop.py), so
  // the panel has to say "restart" rather than show a code for a socket that is
  // still on 127.0.0.1.
  setPairLan: (enabled: boolean) => http<PairLanResult>('POST', '/pair/lan', { enabled }),

  // Mints a single-use claim code plus the exact QR payload for it. 409 when LAN
  // mode is off, and 409 with `no_lan_address` when this Mac has no LAN address
  // to put in the code.
  newPairCode: () => http<PairCode>('POST', '/pair/new'),

  revokeDevice: (deviceId: string) =>
    http<{ revoked: string; devices: PairDevice[] }>('POST', '/pair/revoke', { device_id: deviceId }),

  // --- Prompt Editor (api/prompt_routes.py) -------------------------------

  // POST …/prompt → SSE. Throws on a non-2xx with the body appended, the same
  // contract as http(), so store.errorMessage() and
  // lib/promptEvents.promptRunningFromError() can read it.
  promptStream: (sid: string, body: PromptBody) => sse(`/sessions/${sid}/prompt`, body),

  // POST …/prompt/answer {token, answers} → SSE (the resumed run).
  promptAnswer: (sid: string, token: string, answers: Record<string, unknown>) =>
    sse(`/sessions/${sid}/prompt/answer`, { token, answers }),

  promptPending: (sid: string) => http<{ pending: PromptPending | null }>('GET', `/sessions/${sid}/prompt/pending`),

  promptRun: (sid: string) => http<PromptRunEnvelope>('GET', `/sessions/${sid}/prompt/run`),

  // Cancelling is the ONLY way a run stops — closing the stream is not
  // (spec §4.2). Without a token it cancels the running plan; with one it
  // drops a pending clarification.
  promptCancel: (sid: string, token?: string) =>
    http<{ cancelled: boolean }>('POST', `/sessions/${sid}/prompt/cancel`, token ? { token } : {}),

  // `refresh` re-probes Apple Intelligence / mlx-lm; otherwise a 60 s memo.
  promptBrains: (refresh = false) =>
    http<unknown>('GET', `/prompt/brains${refresh ? '?refresh=1' : ''}`),

  promptModels: () => http<PromptModels>('GET', '/prompt/models'),

  // Loopback-only on the backend (403 otherwise): a paired phone must not be
  // able to start a 4 GB download on the Mac. 202 + job id; poll getJob.
  downloadModel: (id: string) =>
    http<{ job_id: string }>('POST', '/prompt/models/download', { id }),

  // Same posture: removes a downloaded local model. 202 + job id.
  deleteModel: (id: string) =>
    http<{ job_id: string }>('POST', '/prompt/models/delete', { id }),

  // --- Settings (components/SettingsDialog) ---------------------------------
  // The Anthropic key (QA-063-SETTINGS): kept in the macOS Keychain by the
  // backend (keychain.py), loopback-only routes, and no response carries the
  // key — only a masked suffix and where it comes from. The key is sent once,
  // in a JSON body, and never stored or logged on this side.
  anthropicKeyStatus: () => http<KeyStatus>('GET', '/settings/anthropic-key'),
  saveAnthropicKey: (key: string) => http<KeyStatus>('POST', '/settings/anthropic-key', { key }),
  removeAnthropicKey: () => http<KeyStatus & { removed: boolean }>('DELETE', '/settings/anthropic-key'),
  testAnthropicKey: () =>
    http<{ ok: boolean; message: string }>('POST', '/settings/anthropic-key/test', {}),

  // This project's render caches against their byte budget, and Clear
  // (QA-106; render/cache_budget.py). Clear keeps the preview on screen.
  renderCacheUsage: (sid: string) => http<RenderCacheUsage>('GET', `/sessions/${sid}/render-cache`),
  clearRenderCache: (sid: string) =>
    http<RenderCacheUsage & { freed_bytes: number }>('DELETE', `/sessions/${sid}/render-cache`),
}

export interface RenderCacheUsage { bytes: number; budget_bytes: number; by_area: Record<string, number> }

// One POST that returns the Response for an SSE body. Kept separate from
// http() because the body is a stream, not JSON, but it sends the same
// X-VAE-Client header (api/auth.py demands it on every non-media request once
// LAN mode is armed) and raises the same `${status} ${statusText}: ${body}`
// error shape on failure.
async function sse(path: string, body: unknown): Promise<Response> {
  const res = await send(`${BASE}${path}`, {
    method: 'POST',
    headers: { ...CLIENT_HEADERS, 'content-type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok || !res.body) throw await apiError(res)
  return res
}
