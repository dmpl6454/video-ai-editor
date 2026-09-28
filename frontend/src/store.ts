// Zustand store — single source of truth for the editor UI.
// Every UI gesture goes through `dispatch()` so Claude (M2) and the user
// share one mutation path.

import { create } from 'zustand'
import { api, clientFetch } from './api'
import { toast, useToasts } from './toast'
import { type EDL, type Op } from './types'
import { splitTargets } from './lib/splitTargets'
import { deletedLabel } from './lib/deletedLabel'
import { editorValidationMessage, isCancelMessage, stripExceptionPrefix } from './lib/dispatchErrors'
import { editorProse, toolTitle } from './lib/opLabels'
import { firePromptRunning, fireSessionSwitch, promptRunningFromError, PROMPT_RUNNING_MESSAGE } from './lib/promptEvents'
import { nativeSave } from './lib/nativeSave'
import { exportFor, exportLink, withExport, withoutExport, type ExportLinks } from './lib/exportLink'
import { planNudge } from './lib/nudge'
import { isTrackLocked } from './lib/trackLock'
import { frameDuration } from './lib/frameStep'
import { clampZoom } from './lib/timelineZoom'
import { undoDepthOf, undoRefusedMessage } from './lib/undoHorizon'
import { engineState, isAbort, isEngineOffline, onEngineState, type EngineState } from './lib/connection'
import { isStaleEdlError, STALE_VIEW_MESSAGE } from './lib/staleView'
import type { UploadBatch, UploadItem } from './lib/uploadQueue'
import { importFollowUp, type ImportAnswer } from './lib/importFollowUp'
import { placementFor, type LanePlacement, type PlacedAnswer } from './lib/laneDrop'
import { splitTimeFor } from './lib/splitTargets'
import {
  parsePreviewSettings, probePreviewCapabilities, resolvePreviewMode,
  type PreviewCapabilities, type PreviewEngine as PreviewEngineSetting, type PreviewSettings,
} from './lib/previewEngineSetting'
import { PreviewController, type ControllerView } from './lib/preview/previewController'
import { ticksPerFrame } from './lib/preview/timeline/timebase'
import type { EdlLike } from './lib/preview/timeline/framePlan'

// Shape of POST /sessions/:id/dispatch's response as surfaced to UI callers.
// `result` is the tool handler's own return dict (e.g. add_text returns
// {summary, id}; auto_caption returns {summary, cues, language, ...}) — typed
// unknown here because each tool's payload differs; callers narrow it.
export interface DispatchResponse {
  result: unknown
  edl_hash: string
  op: Op | null
}

// Tools that load an ML model and walk every frame. Run on the synchronous
// endpoint they hold a request-thread worker for minutes, which is what makes
// the rest of the app stop responding mid-operation (round-5 VAI-11). These go
// through the job queue instead — the same 202-and-poll path Export has used
// since round 3. Must stay in step with main.py's ASYNC_DISPATCH_TOOLS; the
// backend permits `wait=0` for any tool, so a drift here costs latency, not
// correctness. Exported so the AI panel can tell which of its cards run as a
// job (tests/test_qa_round5.py regexes the `const ASYNC_DISPATCH_TOOLS = new
// Set([` literal — keep it on one line, in this file).
export const ASYNC_DISPATCH_TOOLS = new Set([
  'remove_background', 'object_erase', 'upscale', 'stabilize',
  'smooth_slow_motion', 'vocal_isolate', 'instrumental_isolate',
  'motion_track', 'auto_caption', 'multicam',
])
// `transcribe` (0.7.0) can run for a minute but is deliberately NOT here: the
// backend keeps it out of main.ASYNC_DISPATCH_TOOLS because that set is
// mirrored verbatim in mobile/lib/jobs.ts and the phone is frozen this
// release, and tests/test_qa_round5.py pins this list to the backend's. The
// desktop never dispatches it directly — the prompt executor runs it on its
// own thread (spec §4.2) — so nothing here would pin a request worker.

const JOB_POLL_MS = 700

// QA-034's two terminal export messages.
export const EXPORT_INTERRUPTED = 'The export was interrupted — the editor engine restarted. Export again.'
export const EXPORT_LOST = 'Lost contact with the export — the editor engine stopped responding.'
/** How long an export waits for an unreachable engine before giving up. */
export const EXPORT_LOST_MS = 2 * 60 * 1000

// renderPreview's latest-wins bookkeeping (QA-004): the sequence number of the
// newest request and the controller that can abort it. Module state, not store
// state — nothing renders from them.
let previewSeq = 0
let previewAbort: AbortController | null = null
// openSession's latest-wins counter (a quick A→B→C must land on C).
let openSeq = 0
// Bumped whenever an edit lands (dispatch success). refresh() compares it to
// decide whether the EDL it fetched can still judge the selection (QA-047).
let mutationSeq = 0
// doExport's run token (QA-034): Cancel bumps it so the poll loop that was
// running stops without touching the state of whatever export comes next.
let exportRun = 0
// The import queue (QA-044/094): every file waits for the one before it, so
// a drop lands in drop order and each file's progress is its own.
let importChain: Promise<void> = Promise.resolve()
// Per-item cancel hooks, by upload id.
const importCancels = new Map<string, () => void>()
let importSeq = 0
// One "edits are paused" toast per offline spell, not one per gesture (QA-109).
let offlineNoticeShown = false

// INSTANT PREVIEW (wave D, INSTANT_PREVIEW_SPEC §3.5, §4.1, §7, §9.2): the
// client engine's controller lives here, outside React (like LIVE_FRAMING),
// one per open project while `previewEngine` is 'client'. Server mode never
// creates it, so every server-mode path below is exactly what it was.
let previewCtl: PreviewController | null = null
let previewCaps: PreviewCapabilities | null = null
/** Why the engine fell back to server mode this session (sticky until the
 *  project or the setting changes: §7's engine-level fallback). */
let previewFailure: string | null = null

/** The client preview's controller, or null in server mode. */
export function previewController(): PreviewController | null {
  return previewCtl
}

/** Drop selected ids that are no longer on the timeline (QA-047). Returns the
 *  patch to apply (empty when nothing changed). */
export function pruneSelection(
  s: Pick<State, 'selection' | 'multiSelection' | 'framing'>, edl: EDL | null,
): Partial<Pick<State, 'selection' | 'multiSelection' | 'framing'>> {
  if (!edl) return {}
  const live = new Set<string>()
  for (const t of edl.tracks) for (const c of t.clips) live.add(c.id)
  const keep = [s.selection, ...s.multiSelection].filter((id): id is string => !!id && live.has(id))
  const primaryGone = !!s.selection && !live.has(s.selection)
  const multiGone = s.multiSelection.some((id) => !live.has(id))
  const framingGone = !!s.framing && !live.has(s.framing.clipId)
  if (!primaryGone && !multiGone && !framingGone) return {}
  return {
    ...(primaryGone || multiGone ? { selection: keep[0] ?? null, multiSelection: keep.slice(1) } : {}),
    ...(framingGone ? { framing: null } : {}),
  }
}

/** The server's 409 for a render another request superseded. */
function isPreviewSuperseded(e: unknown): boolean {
  return String((e as Error)?.message ?? e).includes('preview_superseded')
}

/** Submit a tool as a background job and resolve when it finishes, so callers
 *  of `store.dispatch()` see the same promise contract either way.
 *
 *  `onProgress` reports the job id immediately and then its 0..1 progress on
 *  every poll. auto_caption is why: it decodes for minutes, and a caller that
 *  can only render an indefinite spinner is why a working Captions button was
 *  reported as broken. The job id is handed over so the caller can offer
 *  Cancel — the backend has always supported it, nothing ever called it. */
async function runDispatchJob(
  sid: string, tool: string, args: Record<string, unknown>,
  onProgress?: (p: { jobId: string; progress: number }) => void,
  baseHash?: string | null,
): Promise<{ result: { redo_available?: boolean }; edl_hash: string; op: Op | null }> {
  const { job_id } = await api.dispatchAsync(sid, tool, args, baseHash)
  onProgress?.({ jobId: job_id, progress: 0 })
  for (;;) {
    await new Promise((r) => setTimeout(r, JOB_POLL_MS))
    const job = await api.getJob(job_id)
    onProgress?.({ jobId: job_id, progress: job.progress ?? 0 })
    if (job.status === 'completed') {
      return job.result as unknown as
        { result: { redo_available?: boolean }; edl_hash: string; op: Op | null }
    }
    if (job.status === 'failed') throw new Error(job.error || `${tool} failed`)
    // Named in editor language (QA-101): "AI upscale was cancelled", not "upscale …".
    if (job.status === 'cancelled') throw new Error(`${toolTitle(tool)} was cancelled`)
  }
}

// api.ts's http() throws `Error("422 Unprocessable Entity: {json envelope}")`
// with the raw response body appended. The body is usually the hardening
// error envelope ({error:{message}}) or a FastAPI detail — pull the human-
// readable message out so toasts show e.g. "auto_caption: no clip on v1 to
// caption" instead of a wall of JSON.
// The backend's error envelope (api/hardening.py) hardcodes
//   message = "request failed"
// for EVERY HTTPException whose detail is a dict, and puts the real dict under
// `error.details`. So reading `error.message` first — as this used to — meant a
// carefully written server-side explanation ("a cached overlay image was
// corrupted; your media is fine") could never reach a toast: the user always saw
// "request failed". Prefer details.message and fall back to the sentinel.
export function errorMessage(e: unknown): string {
  const raw = e instanceof Error ? e.message : String(e)
  const jsonStart = raw.indexOf('{')
  if (jsonStart !== -1) {
    try {
      const body = JSON.parse(raw.slice(jsonStart)) as {
        error?: { message?: string; details?: unknown }
        detail?: string | { error?: string; message?: string }
      }
      const d = body.error?.details
      const detailMsg = d && typeof d === 'object' && !Array.isArray(d)
        ? (d as { message?: string }).message
        : undefined
      const msg = detailMsg
        ?? body.error?.message
        ?? (typeof body.detail === 'string'
              ? body.detail
              : body.detail?.message ?? body.detail?.error)
      if (msg) return editorValidationMessage(stripExceptionPrefix(msg))
    } catch {
      // not a JSON tail — fall through to the raw text
    }
  }
  // Job failures arrive as "RuntimeError: …" (api/jobs.py records the class
  // name) — see lib/dispatchErrors.ts for why the prefix is dropped.
  return editorValidationMessage(stripExceptionPrefix(raw))
}

// Reads a persisted panel size (Task 9's Splitter drag state). Guards against
// SSR (no `localStorage`), an unset key (`null` -> NaN -> falls through to
// `fallback`), and a corrupted/non-numeric value the same way. Clamped to the
// same [160, 640] range setPanelSize enforces, so a stale/tampered value from
// an older build can't render a broken layout.
function readStoredPanelSize(key: string, fallback: number): number {
  if (typeof localStorage === 'undefined') return fallback
  const raw = Number(localStorage.getItem(key))
  if (!raw || Number.isNaN(raw)) return fallback
  return Math.max(160, Math.min(640, raw))
}

// Reads a persisted boolean flag (right-panel open/closed). Guards against
// SSR and a missing/corrupted key the same way readStoredPanelSize does.
function readStoredBool(key: string, fallback: boolean): boolean {
  if (typeof localStorage === 'undefined') return fallback
  const raw = localStorage.getItem(key)
  if (raw === null) return fallback
  return raw === 'true'
}

/** One copied clip: the lane it came from and its EDL JSON (QA-056). */
export interface ClipboardItem { track: string; clip: Record<string, unknown> }

/** Per-import options. `addToTimeline` defaults to the Media panel's
 *  "Add imports to the timeline" switch (QA-010). */
export interface ImportOptions {
  addToTimeline?: boolean
  /** A timeline lane drop (QA-093): import without the default placement,
   *  then place the file on this lane from what the server answered. */
  place?: LanePlacement
}

interface State {
  sessionId: string | null
  sessionName: string
  edl: EDL | null
  ops: Op[]
  redoAvailable: boolean
  // QA-046: how many ⌘Z steps the server can still take (GET /sessions
  // undo_depth). The Undo button and History's horizon bind to this.
  undoDepth: number
  // Count of in-flight dispatch() calls. >0 means at least one edit is being
  // applied server-side. Every gesture (drag, click, chat tool call) used to
  // give NO feedback between the click and the debounced refresh landing —
  // there was no lock and no busy indicator, so a user could fire overlapping
  // gestures during that window with no idea anything was in progress
  // (issues 1/2/3/5, "rendering slow and not apparent", "delay after any
  // action which can overlap with performing even more actions").
  pendingOps: number
  selection: string | null   // primary selected clip id
  multiSelection: string[]   // additional selected clip ids (shift+click)
  inMark: number | null      // in/out marks for range selection / export region
  outMark: number | null
  playhead: number           // seconds
  isPlaying: boolean
  playbackRate: number       // J/K/L shuttle
  previewHash: string | null
  // True while the NEWEST preview render is in flight (drives the corner
  // "Rendering…" badge). Owned by renderPreview, so every caller — the Preview
  // effect, chat, the prompt bar — shows the same state (QA-004).
  previewRendering: boolean
  // True while the import queue holds anything (QA-094: busy until EVERY
  // file of a drop is in, not until the first one finishes).
  uploading: boolean
  uploadProgress: string | null
  uploadError: string | null
  // QA-044: one entry per file still importing, in drop order — the Media
  // panel draws each as a placeholder row with stage, %, ETA and Cancel.
  uploads: UploadItem[]
  // How far the current batch has got ("Importing 2 of 3").
  uploadBatch: UploadBatch
  // QA-010 remainder: "Add imports to the timeline" (off = import to the
  // media library only). Remembered per browser.
  importAddToTimeline: boolean
  setImportAddToTimeline(on: boolean): void
  cancelUpload(id: string): void
  // QA-109: is the editor engine answering? Mirrors lib/connection.
  engine: EngineState
  exporting: boolean
  // Each session's last finished export as ONE record — session, url, filename
  // and the EDL hash it was rendered from (lib/exportLink — QA-026). TopBar
  // shows only the one for the session on screen, marked outdated when
  // `edlHash` differs.
  exportLinks: ExportLinks
  // The current timeline's hash (GET /sessions/:id summary.edl_hash), refreshed
  // with the EDL. What "is the export still this timeline?" is asked against.
  edlHash: string | null
  exportStatus: string | null   // 'queued' | 'running' — coarse job phase for the UI
  exportError: string | null
  exportProgress: number        // 0..1 live ffmpeg progress
  exportJobId: string | null    // current export job (for cancel)

  // Client-side live transform: set while a transform slider is being dragged
  // so Preview applies a CSS transform to the <video> for instant feedback,
  // without a server render. Cleared (null) the moment the drag commits.
  // dx/dy (canvas pixels, relative to the clip's own last-committed x/y) drive
  // the same CSS-transform preview for StickerLayer's direct on-canvas video
  // drag — deltas rather than absolute values so Preview doesn't need to
  // re-derive "what was this clip's x/y before the drag started" itself.
  liveTransform: { clipId: string; scale?: number; rotation?: number; opacity?: number
                   dx?: number; dy?: number } | null

  // FRAMING MODE — which clip (if any) currently has the crop/reposition view
  // open over the preview, plus what its framing looked like when the mode was
  // entered so Cancel can put it back exactly.
  //
  // This used to be implicit: the view appeared whenever the selected v1 clip
  // happened to have `fit: 'cover'`, which made the "Fill frame" checkbox do two
  // unrelated jobs — set a render property AND open an editing mode — and gave
  // no way to say "I'm done" without changing the render. Requested as: a button
  // to enter framing and an Apply button to leave it.
  //
  // In the store rather than local state because the two halves live in
  // different components: the buttons are in Properties, the view is in Preview.
  //
  // `before` is what Cancel restores. Snapshotting it also fixes the documented
  // trade-off in the old checkbox, whose untick reset the transform to IDENTITY
  // rather than to whatever it was before cover was entered — so ticking and
  // unticking to compare silently discarded a zoom you had set under `contain`.
  framing: { clipId: string
             before: { fit?: string; x?: number; y?: number; scale?: number } } | null

  // Client-side live color filter — the Color panel's mirror of liveTransform.
  // Set while a brightness/contrast/saturation slider drags so Preview applies
  // a CSS filter() approximation instantly; cleared the same way liveTransform
  // is (the re-rendered <video>'s onLoadedData + safety-net timeout). Values
  // are the EDL grade params (ffmpeg eq semantics) — Preview converts to CSS.
  liveFilter: { clipId: string; brightness?: number; contrast?: number; saturation?: number } | null

  // Which preview runs (wave D): today's server render ('server', the
  // default), or the instant client engine ('client') — resolved from the
  // `preview.engine` setting, this window's capabilities and the project
  // rate (lib/previewEngineSetting.resolvePreviewMode).
  previewEngine: 'server' | 'client'
  previewEngineReason: string | null
  previewSettings: PreviewSettings | null
  /** The client engine's spinner / fidelity view (null in server mode). */
  clientView: ControllerView | null
  /** Read the setting and pick the engine. */
  resolvePreviewEngine(): Promise<void>
  /** Settings' "Instant preview (beta)": write it, then re-pick. */
  setPreviewEngineSetting(engine: PreviewEngineSetting): Promise<boolean>

  // setters
  setLiveTransform(t: State['liveTransform']): void
  setFraming(f: State['framing']): void
  setLiveFilter(f: State['liveFilter']): void
  setSelection(id: string | null): void
  toggleSelection(id: string): void
  clearSelection(): void
  setPlayhead(t: number): void
  setPlaying(p: boolean): void
  /** If the playhead is parked at (or within a frame of) the end, rewind to 0.
      Shared by the transport button and the playPause keyboard command so both
      replay-from-end paths behave identically. Returns true if it rewound. */
  replayFromStart(): boolean
  setPlaybackRate(r: number): void
  setInMark(t: number | null): void
  setOutMark(t: number | null): void
  clearUploadError(): void
  clearExportError(): void
  resetTransient(): void

  // --- the timeline's resizable height (Task 9), persisted to localStorage
  // so a drag survives reload. Plain px, clamped in setPanelSize. The side
  // panels' widths and the right panel's open state live in
  // lib/layoutStore.ts (LEFT_RAIL_SPEC R1; the old copies here went in R6). ---
  timelineH: number
  setPanelSize(key: 'timelineH', px: number): void

  // --- timeline view + shortcut-driven actions ---
  timelineZoom: number              // px per second
  snapEnabled: boolean
  // QA-056: the copied clips' CONTENT (lane + clip JSON), not their ids — a
  // paste must survive the source being deleted and land at the playhead.
  clipboard: ClipboardItem[]
  flashClipId: string | null        // clip to briefly flash on the timeline
  flashAt: number                   // timestamp the flash started (ms)
  flashClip(id: string): void       // draw attention to a newly-added clip
  setTimelineZoom(z: number): void
  zoomTimeline(factor: number): void   // multiply zoom (in/out)
  toggleSnap(): void
  selectAll(): void
  /** Select exactly `ids` (or add them, `additive`) — box selection. */
  selectClips(ids: string[], additive?: boolean): void
  copySelection(): void
  pasteClipboard(): Promise<void>
  goToStart(): void
  goToEnd(): void
  nudgeSelection(deltaSeconds: number): Promise<void>

  // workflow
  init(): Promise<void>
  refresh(): Promise<void>
  /** QA-105: ask the server whether this window's view is still current and
   *  refresh when another window (or an agent) changed the project. */
  syncWithServer(): Promise<void>
  /** QA-099: rename the open project. Resolves false (and toasts) on failure. */
  renameSession(name: string): Promise<boolean>
  /** Switch the editor to project `id`, atomically: see the action. */
  openSession(id: string): Promise<void>
  refreshSoon(): void
  upload(file: File, opts?: ImportOptions): Promise<void>
  uploadAudio(file: File, opts?: ImportOptions): Promise<void>
  // Resolves with the dispatch response on success (so callers can read the
  // tool's own result, e.g. add_text's new clip id or auto_caption's cue
  // count) or null on failure — the failure is already surfaced via
  // toast.error here, so callers just need to know it didn't land.
  // `opts.onProgress` only fires for tools that run as jobs — the
  // ASYNC_DISPATCH_TOOLS, or any tool with `opts.asJob` (the AI panel sets it
  // for the slow-but-not-ML tools so they don't hold a request worker under
  // the session lock); everything else resolves too fast to be worth
  // reporting. `opts.onError` receives the same message the failure toast
  // shows, so a caller with its own status area can mirror it.
  dispatch(
    tool: string,
    args?: Record<string, unknown>,
    opts?: {
      onProgress?: (p: { jobId: string; progress: number }) => void
      onError?: (message: string) => void
      asJob?: boolean
    },
  ): Promise<DispatchResponse | null>
  /** `priority: 'low'`: the client engine's background render (niced). */
  renderPreview(opts?: { priority?: 'low' }): Promise<string>
  /** Session fields only (ops, undo depth, redo, name) — the client engine
   *  already has the EDL from the dispatch answer (spec §4.1 step 3). */
  refreshSession(): Promise<void>
  // `saveAs` (QA-100): the Export dialog's File name — the name the file is
  // saved under; `fps` (QA-009) only when it differs from the project rate.
  doExport(opts?: { height?: number; fps?: number; crf?: number; container?: 'mp4' | 'mov' | 'm4a' | 'wav'; bitrate_kbps?: number; saveAs?: string }): Promise<void>
  // Save the last finished export to disk. In the packaged app this drives the
  // native Save-As dialog (via the pywebview bridge); in a browser it falls
  // back to an `<a download>` click. Wired to the green download-arrow link so
  // clicking it never navigates the WKWebView to the inline .mp4 (which opened
  // an inescapable native fullscreen player).
  downloadExport(): Promise<void>
  cancelExport(): Promise<void>
  splitAtPlayhead(): Promise<void>
  // Split one track at `time` and move the selection into the resulting right
  // half (see the implementation for why the left half is the wrong thing to
  // keep selected).
  splitTrackAt(track: string, time: number): Promise<void>
  rippleDeleteSelection(): Promise<void>
  duplicateSelection(): Promise<void>
}

export const useStore = create<State>((set, get) => ({
  sessionId: null,
  sessionName: '',
  edl: null,
  ops: [],
  redoAvailable: false,
  undoDepth: 0,
  pendingOps: 0,
  selection: null,
  playhead: 0,
  isPlaying: false,
  previewHash: null,
  previewRendering: false,
  multiSelection: [],
  inMark: null,
  outMark: null,
  playbackRate: 1,
  liveTransform: null,
  framing: null,
  liveFilter: null,
  previewEngine: 'server',
  previewEngineReason: null,
  previewSettings: null,
  clientView: null,
  uploading: false,
  uploadProgress: null,
  uploadError: null,
  uploads: [],
  uploadBatch: { total: 0, done: 0 },
  importAddToTimeline: readStoredBool('vai.importAddToTimeline', true),
  engine: engineState(),
  exporting: false,
  exportLinks: {},
  edlHash: null,
  exportStatus: null,
  exportError: null,
  exportProgress: 0,
  exportJobId: null,

  // The timeline height: read from localStorage (falls back to the historical
  // 280 px when unset, invalid, or running server-side where localStorage
  // doesn't exist).
  timelineH: readStoredPanelSize('vai.timelineH', 280),

  setSelection: (id) => set((s) => ({
    selection: id, multiSelection: id ? [] : [],
    // Leaving a clip ENDS its framing session. The crop view is already gated on
    // the selected clip, so a stale entry never draws — but it would silently
    // reopen the moment that clip was selected again, and its `before` snapshot
    // would by then describe a state from minutes ago, making Cancel restore
    // something the user had long since moved on from.
    framing: s.framing && s.framing.clipId === id ? s.framing : null,
  })),
  toggleSelection: (id) => {
    const s = get()
    if (s.selection === id) {
      // demote primary into multi if we already have a multi-set, otherwise clear
      const next = s.multiSelection.filter((x) => x !== id)
      set({ selection: next[0] ?? null, multiSelection: next.slice(1) })
      return
    }
    if (s.multiSelection.includes(id)) {
      set({ multiSelection: s.multiSelection.filter((x) => x !== id) })
      return
    }
    if (!s.selection) {
      set({ selection: id })
      return
    }
    set({ multiSelection: [...s.multiSelection, id] })
  },
  clearSelection: () => set({ selection: null, multiSelection: [] }),
  setPlayhead: (t) => {
    // Clamp to [0, edl.duration]. Without the upper cap, clicking past the
    // last clip on the ruler sends the <video>'s currentTime past its end →
    // preview goes black.
    const dur = get().edl?.duration
    const clamped = Math.max(0, dur ? Math.min(t, dur) : t)
    // No-op guard: during end-of-timeline replay the rAF clock re-asserts the
    // same clamped value every frame; a redundant set() forces a full re-render
    // that re-runs the playback effects and feeds the play/pause oscillation.
    if (get().playhead === clamped) return
    set({ playhead: clamped })
  },
  setPlaying: (p) => {
    // No-op guard — see setPlayhead. onPlay/onPause + the rAF re-clamp otherwise
    // hammer setPlaying with the same value every frame, re-running effects.
    if (get().isPlaying === p) return
    const ctl = get().previewEngine === 'client' ? previewCtl : null
    if (ctl) {
      // Client engine: Space, the transport click and L all land HERE, inside
      // the key/click handler, so laneA.play() and AudioContext.resume() run
      // synchronously in the user's gesture (spec §3.5). The shuttle's rates
      // are Phase 4: forward plays at 1x, reverse does not start.
      if (p && (get().playbackRate < 0 || reverseAsked)) { reverseAsked = false; return }
      if (p) set({ isPlaying: ctl.play(get().playhead) })
      else {
        ctl.pause()
        set({ isPlaying: false })
      }
      return
    }
    set({ isPlaying: p })
  },
  replayFromStart: () => {
    const s = get()
    const dur = s.edl?.duration ?? 0
    // Within one frame of the end, at the PROJECT rate (QA-009).
    if (!s.isPlaying && dur > 0 && s.playhead >= dur - frameDuration(s.edl?.canvas?.fps)) {
      s.setPlayhead(0)
      return true
    }
    return false
  },
  setPlaybackRate: (r) => {
    // Client engine: the shuttle's rates are Phase 4 — it plays forward at 1×
    // only. The store keeps the rate it ACTUALLY plays (1), so nothing claims
    // 2× while the picture runs at 1× (StickerLayer animates at this rate),
    // and says why once (Final QA: L L and J were silently wrong). A reverse
    // request while playing stops it (inside the same key handler), and the
    // setPlaying(true) J sends right after is refused via `reverseAsked`.
    if (get().previewEngine === 'client' && previewCtl) {
      if (r !== 1) noteShuttleUnsupported()
      if (r < 0) {
        reverseAsked = true
        queueMicrotask(() => { reverseAsked = false })
        if (get().isPlaying) {
          previewCtl.pause()
          set({ playbackRate: 1, isPlaying: false })
          return
        }
      }
      if (get().playbackRate !== 1) set({ playbackRate: 1 })
      return
    }
    set({ playbackRate: r })
  },
  setLiveTransform: (t) => set({ liveTransform: t }),
  setFraming: (f) => set({ framing: f }),
  setLiveFilter: (f) => set({ liveFilter: f }),
  setInMark: (t) => set({ inMark: t }),
  setOutMark: (t) => set({ outMark: t }),
  clearUploadError: () => set({ uploadError: null }),
  setImportAddToTimeline: (on) => {
    try { localStorage.setItem('vai.importAddToTimeline', String(on)) } catch { /* private mode */ }
    set({ importAddToTimeline: on })
  },
  cancelUpload: (id) => {
    // Upload stage → the XHR aborts; processing → the server job is cancelled
    // (its normalise stops and the half-made files are removed); still
    // waiting → it never starts. The queue item goes either way.
    importCancels.get(id)?.()
    set(afterImportLeaves(get(), id))
  },
  clearExportError: () => set({ exportError: null }),

  resolvePreviewEngine: async () => {
    let wire: unknown
    try {
      wire = await api.previewSettings()
    } catch {
      wire = null   // an older backend, or offline: the server preview
    }
    set({ previewSettings: parsePreviewSettings(wire) })
    applyPreviewMode()
  },

  setPreviewEngineSetting: async (engine) => {
    try {
      const wire = await api.setPreviewEngine(engine)
      previewFailure = null
      set({ previewSettings: parsePreviewSettings(wire) })
      applyPreviewMode()
      return true
    } catch (e) {
      toast.error(`Couldn't change the preview: ${errorMessage(e)}`)
      return false
    }
  },

  // Clears per-session view/selection state. Call when switching sessions so a
  // stale playhead/selection/marks from the previous project don't bleed onto
  // the new timeline (which read as "a second frozen playhead").
  //
  // `previewHash` and `edlHash` belong to the project on screen too: a kept
  // previewHash made the new project's scrubber ask for
  // `<new sid>/preview.mp4?h=<old project's hash>` (a 404), and a kept edlHash
  // would judge the other project's export link against the wrong timeline
  // until the refresh landed. `exportLinks` is NOT cleared — each link names its
  // own session, so it is not shown elsewhere and comes back with its project.
  resetTransient: () => set({
    playhead: 0,
    selection: null,
    multiSelection: [],
    inMark: null,
    outMark: null,
    previewHash: null,
    edlHash: null,
    // An export failure is about the project it was rendered from; left set,
    // project B showed A's "⚠ ffmpeg failed …" chip (QA-026's leak, again).
    exportError: null,
  }),

  // Persists a panel size to localStorage as the drag happens (not just on
  // commit) so a mid-drag reload can't lose it, then updates the CSS-var-
  // driving state. Clamped to [160, 640]px — below 160 a panel's own controls
  // start clipping; above 640 one pane can crowd out the rest of the 900px-
  // floor layout.
  setPanelSize: (key, px) => {
    const clamped = Math.max(160, Math.min(640, px))
    if (typeof localStorage !== 'undefined') localStorage.setItem(`vai.${key}`, String(clamped))
    set({ [key]: clamped } as Partial<State>)
  },

  // --- timeline view + shortcut-driven actions ---
  timelineZoom: 80,
  snapEnabled: true,
  clipboard: [],
  flashClipId: null,
  flashAt: 0,
  flashClip: (id) => {
    const at = Date.now()
    set({ flashClipId: id, flashAt: at })
    // Auto-clear after the animation. Guard on `flashAt` (not just id) so a
    // stale timeout from an earlier flash of the SAME clip can't cancel a fresh
    // one — re-flashing within the window must restart, not abort.
    setTimeout(() => {
      const s = get()
      if (s.flashClipId === id && s.flashAt === at) set({ flashClipId: null })
    }, 700)
  },
  // lib/timelineZoom's limits (QA-054): the old 10 px/s floor left a 12-min
  // timeline 7200 px wide at its most zoomed out.
  setTimelineZoom: (z) => set({ timelineZoom: clampZoom(z) }),
  zoomTimeline: (factor) => {
    const z = get().timelineZoom
    set({ timelineZoom: clampZoom(z * factor) })
  },
  toggleSnap: () => set({ snapEnabled: !get().snapEnabled }),
  selectAll: () => {
    const edl = get().edl
    if (!edl) return
    // EVERY clip — text included (QA-116: the old `'src' in c` filter kept
    // media and stickers only, so ⌘A then ⌫ left titles over black) — on
    // every lane that can be edited; a locked lane's clips cannot be deleted
    // or moved, so selecting them would only make the next edit refuse.
    const ids: string[] = []
    for (const t of edl.tracks) {
      if (isTrackLocked(t)) continue
      for (const c of t.clips) ids.push(c.id)
    }
    set({ selection: ids[0] ?? null, multiSelection: ids.slice(1) })
  },
  selectClips: (ids, additive = false) => {
    // The timeline's box selection (QA-116). Additive (Shift/⌘) keeps what
    // was selected and adds the boxed clips; otherwise the box replaces it.
    const s = get()
    const base = additive ? [s.selection, ...s.multiSelection].filter((x): x is string => !!x) : []
    const next = Array.from(new Set([...base, ...ids]))
    set({
      selection: next[0] ?? null, multiSelection: next.slice(1),
      framing: s.framing && s.framing.clipId === next[0] ? s.framing : null,
    })
  },
  copySelection: () => {
    const s = get()
    // QA-056: copy the clips THEMSELVES (lane + JSON), not their ids. The ids
    // made paste a `duplicate_clip` of the source — landing after it instead
    // of at the playhead, and failing "clip not found" once it was deleted.
    const ids = new Set([s.selection, ...s.multiSelection].filter(Boolean) as string[])
    const items: ClipboardItem[] = []
    for (const t of s.edl?.tracks ?? []) {
      for (const c of t.clips) {
        if (ids.has(c.id)) items.push({ track: t.id, clip: JSON.parse(JSON.stringify(c)) as Record<string, unknown> })
      }
    }
    if (items.length) set({ clipboard: items })
  },
  pasteClipboard: async () => {
    const s = get()
    if (!s.clipboard.length) return
    // AT THE PLAYHEAD, in layout time: the playhead is render time, and v1
    // decodes through its own inverse, every other lane through layoutTime —
    // the same rule ⌘B uses (lib/splitTargets). One tool call, one undo step.
    const lane = s.clipboard.some((c) => c.track === 'v1') ? 'v1' : s.clipboard[0].track
    const at = splitTimeFor(s.edl, lane, s.playhead)
    const res = await s.dispatch('paste_clips', { clips: s.clipboard, at })
    const ids = (res?.result as { clip_ids?: string[] } | undefined)?.clip_ids ?? []
    // Select what was just pasted — the next Backspace/trim acts on the copy,
    // as in every NLE.
    if (ids.length) set({ selection: ids[0], multiSelection: ids.slice(1) })
  },
  goToStart: () => set({ playhead: 0 }),
  goToEnd: () => {
    const dur = get().edl?.duration ?? 0
    set({ playhead: dur })
  },
  nudgeSelection: async (deltaSeconds) => {
    const s = get()
    // QA-022: planned locally (lib/nudge) — one frame of the project rate, and
    // a nudge into a neighbour is refused with the reason instead of being
    // sent to a backend whose free-gap snap teleported it past the last clip.
    const plan = planNudge(s.edl, s.selection, deltaSeconds)
    if (plan.kind === 'refuse') { toast.info(plan.message); return }
    if (plan.kind === 'move') {
      await s.dispatch('move_clip', { clip_id: plan.clipId, new_start: plan.newStart })
    }
  },

  init: async () => {
    void get().resolvePreviewEngine()
    // Reconnect to what THIS BROWSER was last showing, not whatever session
    // happens to be most recently touched on the SERVER. `listSessions()` is
    // sorted by each session directory's own mtime (storage.py), which any
    // client sharing this backend can bump — an MCP tool call, a QA/test
    // script hitting the API directly, another browser tab. `list[0]` used to
    // be adopted unconditionally on every launch, so a burst of unrelated
    // activity elsewhere (this was found via repeated exe rebuild/relaunch
    // cycles during development, each creating fresh sessions for
    // verification) could silently swap a user's in-progress project out for
    // a stranger session on their very next launch — no error, no prompt, just
    // a different timeline where theirs used to be.
    //
    // `vai.sessionId` is the fix: written to localStorage whenever the active
    // session changes (see the subscribe() below), so THIS browser profile
    // remembers what it had open regardless of what else touches the shared
    // backend. Falls through to the original "most recent on the server"
    // behavior when there is nothing remembered (first-ever launch) or the
    // remembered session no longer exists (deleted) — unchanged from before
    // for both of those cases.
    const list = await api.listSessions()
    let remembered: string | null = null
    try { remembered = localStorage.getItem('vai.sessionId') } catch { /* private mode */ }
    const existing = (remembered && list.sessions.find((s) => s.id === remembered))
      || list.sessions[0]
    const sid = existing?.id ?? (await api.createSession()).id
    get().resetTransient()
    set({ sessionId: sid, sessionName: existing?.name ?? sid })
    await get().refresh()
  },

  refresh: async () => {
    const sid = get().sessionId
    if (!sid) return
    const seq = mutationSeq
    const [info, edl] = await Promise.all([api.getSession(sid), api.getEDL(sid)])
    // The project changed while this was in flight (a refreshSoon() after an
    // edit, an upload finishing, a project switch): these are the OLD
    // project's EDL and ops. Writing them would put project A's timeline on
    // screen under project B's session id.
    if (get().sessionId !== sid) return
    set({ edl, ops: info.ops, sessionName: info.name, edlHash: info.summary?.edl_hash ?? null,
          redoAvailable: !!info.redo_available, undoDepth: undoDepthOf(info) })
    // QA-047: a selection is only meaningful while its clips exist. Undoing a
    // split removed the right half, yet the (invisible) selection still named
    // it, so ⌘D/Backspace sent it and toasted "clip not found". Skipped when
    // an edit landed while this fetch was in flight: that EDL may predate a
    // clip the edit just selected (split → right half, paste → the copy).
    if (seq === mutationSeq) set(pruneSelection(get(), edl))
  },

  syncWithServer: async () => {
    const s = get()
    const sid = s.sessionId
    // Never while this window's own edit is in flight: its answer updates
    // edlHash, and until then the server legitimately differs.
    if (!sid || s.pendingOps > 0 || s.uploading || engineState() === 'offline') return
    let head: { edl_hash: string }
    try {
      head = await api.sessionHead(sid)
    } catch (e) {
      // Offline is the banner's to report; anything else is a background probe.
      if (!isEngineOffline(e)) console.warn('[store] head check failed:', errorMessage(e))
      return
    }
    const now = get()
    if (now.sessionId !== sid || now.pendingOps > 0 || head.edl_hash === now.edlHash) return
    // Another window, an MCP agent or a prompt run changed the project: take
    // the server's timeline (the store never holds optimistic edits, so there
    // is nothing local to lose).
    await get().refresh()
  },

  renameSession: async (name) => {
    const sid = get().sessionId
    const clean = name.replace(/\s+/g, ' ').trim()
    if (!sid || !clean) return false
    try {
      const r = await api.renameSession(sid, clean)
      if (get().sessionId === sid) set({ sessionName: r.name })
      return true
    } catch (e) {
      toast.error(`Couldn't rename the project: ${errorMessage(e)}`)
      return false
    }
  },

  // Project switch. Loads the new project FIRST, then swaps session id and
  // EDL in one set(). The old sequence (resetTransient → set sessionId →
  // refresh()) left one render with the new sessionId and the previous
  // project's EDL: the Media bin built rows from that EDL and asked
  // `/sessions/<B>/thumb?src=<A's file>` (a 403 on every switch), and the
  // timeline showed A's clips under B's id until the fetch landed. Latest
  // call wins, so a quick A→B→C cannot land on B.
  openSession: async (id) => {
    const seq = ++openSeq
    const [info, edl] = await Promise.all([api.getSession(id), api.getEDL(id)])
    if (seq !== openSeq) return
    get().resetTransient()
    set({ sessionId: id, sessionName: info.name ?? id, edl, ops: info.ops,
          edlHash: info.summary?.edl_hash ?? null, redoAvailable: !!info.redo_available,
          undoDepth: undoDepthOf(info) })
  },

  refreshSession: async () => {
    const sid = get().sessionId
    if (!sid) return
    const info = await api.getSession(sid)
    if (get().sessionId !== sid) return
    set({ ops: info.ops, sessionName: info.name, redoAvailable: !!info.redo_available, undoDepth: undoDepthOf(info) })
  },

  // Coalesce many quick refresh() calls (chat tool storms, drag bursts) into a
  // single fetch ~120ms after the last request. Keeps the EDL fetch from
  // becoming the bottleneck during a flurry of dispatches.
  refreshSoon: (() => {
    let pending: ReturnType<typeof setTimeout> | null = null
    return () => {
      if (pending) clearTimeout(pending)
      pending = setTimeout(() => {
        pending = null
        // A swallowed rejection here leaves the UI showing a STALE timeline
        // that no longer matches the server, with nothing on screen to say so —
        // the user's next edit is then computed against the wrong state. Surface
        // it; the toast is cheap and the alternative is silent divergence.
        useStore.getState().refresh().catch((e) => {
          console.warn('[store] refresh failed:', e)
          // Offline is the banner's to say; it refreshes on reconnect.
          if (!isEngineOffline(e)) toast.error(`Couldn't refresh the timeline: ${errorMessage(e)}`)
        })
      }, 120)
    }
  })(),

  // Both ingresses go through ONE queue (enqueueImport, below): a drop of
  // three files shows three placeholder rows at once, imports them in drop
  // order, and the panel stays busy until the last one is in (QA-044/094).
  // NO renderPreview() after an import: refresh() changes the EDL and the
  // Preview effect renders on exactly that change (QA-004).
  upload: (file, opts) => enqueueImport(file, 'video', opts),

  uploadAudio: (file, opts) => enqueueImport(file, 'audio', opts),

  dispatch: async (tool, args = {}, opts) => {
    const sid = get().sessionId
    if (!sid) return null
    // Resolved BEFORE the request: the toast fires after it, and refreshSoon()
    // may already have replaced the EDL with one that no longer holds the clip
    // — at which point there is nothing left to name.
    const deleteIds = tool === 'bulk_delete'
      ? ((args.clip_ids as string[] | undefined) ?? [])
      : tool === 'ripple_delete' ? [String(args.clip_id ?? '')] : []
    const deleteMsg = deleteIds.length ? deletedLabel(get().edl, deleteIds) : ''
    // QA-109: with the engine gone the gesture cannot land — say so once (the
    // banner carries the rest) instead of a raw "Failed to fetch" per gesture.
    if (engineState() === 'offline') { noticeOffline(opts?.onError); return null }
    // QA-105: the hash this window's view was built from. Only when no other
    // edit of ours is in flight and no import is placing clips: then the
    // server's answer can legitimately be ahead of edlHash.
    const idle = get().pendingOps === 0 && !get().uploading
    const baseHash = idle ? get().edlHash : null
    set({ pendingOps: get().pendingOps + 1 })
    try {
      // We KEEP the previous export's download link after an edit, but the UI
      // marks it "outdated" once the refreshed edlHash differs from the hash the
      // export was rendered from (lib/exportLink, TopBar).
      // Client engine (spec §4.1): the answer carries the post-op EDL and its
      // render hash, applied at once; the refresh then only fetches the
      // session fields. Server mode sends exactly the request it always did.
      const ctl = get().previewEngine === 'client' ? previewCtl : null
      const job = ASYNC_DISPATCH_TOOLS.has(tool) || !!opts?.asJob
      const res: { result: { redo_available?: boolean; ok?: boolean; undo_depth?: number };
                   edl_hash: string; op: Op | null; undo_depth?: number
                   edl?: EDL; render_hash?: string } =
        job
          ? await runDispatchJob(sid, tool, args, opts?.onProgress, baseHash)
          : ctl
            ? await api.dispatchWithEdl<{ redo_available?: boolean }>(sid, tool, args, baseHash)
            : await api.dispatch<{ redo_available?: boolean }>(sid, tool, args, baseHash)
      // The view now IS this answer's timeline — the next gesture's base
      // (QA-105) must not wait for the debounced refresh.
      mutationSeq += 1
      const landed = !!ctl && !!res.edl && typeof res.render_hash === 'string'
        && get().sessionId === sid && previewCtl === ctl
      if (landed) {
        ctl!.applyTimeline(res.edl as unknown as EdlLike, res.render_hash!)
        set({ edl: res.edl!, ...pruneSelection(get(), res.edl!) })
      }
      if (typeof res.edl_hash === 'string' && get().sessionId === sid) set({ edlHash: res.edl_hash })
      // QA-046: the server's undo horizon rides on every dispatch answer, so
      // the Undo button is right the instant an edit (or an undo) lands.
      const depth = res.result?.undo_depth ?? res.undo_depth
      if (typeof depth === 'number') set({ undoDepth: Math.max(0, depth) })
      if (tool === 'undo' && res.result?.ok === false) {
        toast.info(undoRefusedMessage(get().ops.length))
      }
      if (tool === 'undo' || tool === 'redo') {
        // Undo/redo get an IMMEDIATE (non-debounced) refresh, not the
        // 120ms-coalesced refreshSoon(): the whole point of Undo/Redo is that
        // the timeline visibly changes right away, and the debounce (designed
        // for chat tool-storms) was making rapid undo/redo clicks feel laggy
        // and non-deterministic about which state actually landed. Also apply
        // `redo_available` from the response synchronously so the Redo button
        // disables the instant the stack empties, without waiting on refresh.
        if (typeof res.result?.redo_available === 'boolean') {
          set({ redoAvailable: res.result.redo_available })
        }
        await (landed ? get().refreshSession() : get().refresh())
        return res
      }
      // Use the debounced refresh: chained tool calls (chat storms) coalesce
      // into one EDL fetch instead of N.
      if (landed) refreshSessionSoon()
      else get().refreshSoon()
      // Offer a quick Undo on destructive deletes — covers every entry point
      // (keyboard, Properties Delete, timeline context menu) in one spot. The
      // backend's own undo is the restore; 'undo' isn't a delete so it can't loop.
      if (deleteMsg) {
        toast.action(deleteMsg,
          { label: 'Undo', onClick: () => { void get().dispatch('undo') } })
      }
      return res
    } catch (e) {
      // Edits used to fail SILENTLY here — no catch at all, so a rejected
      // dispatch (bad args, a validation error like the new lane-type check,
      // a network hiccup) left the user staring at a UI that looked like
      // nothing happened, with no error anywhere (issue 15-adjacent: "no
      // persistent error surface for a failed edit").
      // 409 `prompt_running`: a Prompt-bar run holds this session's lock
      // (spec §4.2) and the backend refused the edit rather than queueing it
      // behind minutes of captioning. Not an error in the user's terms — the
      // prompt store (which depends on this module, so it listens rather than
      // being imported) shows a toast with Cancel and attaches the bar to the
      // run. If nothing is listening yet, a plain toast still says why.
      if (promptRunningFromError(e)) {
        opts?.onError?.(PROMPT_RUNNING_MESSAGE)
        if (!firePromptRunning(sid)) toast.info(PROMPT_RUNNING_MESSAGE)
        return null
      }
      if (isEngineOffline(e)) { noticeOffline(opts?.onError); return null }
      // QA-105: another window (or an agent) changed the project since this
      // view was fetched, and the server refused to apply the edit to a
      // timeline we are not showing. Show the real one and say why.
      if (isStaleEdlError(e)) {
        opts?.onError?.(STALE_VIEW_MESSAGE)
        toast.info(STALE_VIEW_MESSAGE)
        await get().refresh().catch((err) => console.warn('[store] refresh after stale edit failed:', err))
        return null
      }
      // In editor words (QA-101 sweep): a refusal from the engine names tool
      // ids and clip ids ("set_clip_timing is for overlays; use trim_clip…").
      const msg = editorProse(errorMessage(e))
      opts?.onError?.(msg)
      if (isCancelMessage(msg)) toast.info(msg)   // the user asked for this — not red
      else toast.error(msg)
      return null
    } finally {
      set({ pendingOps: Math.max(0, get().pendingOps - 1) })
    }
  },

  // LATEST-WINS (QA-004). Every call supersedes the one before it: the older
  // request is aborted (the server cancels its render when the newer request
  // arrives), and a response that is no longer the newest can never set
  // previewHash. Before, whichever response arrived LAST won — an edit followed
  // by an undo 0.7 s later left the player on the edited state's render
  // indefinitely, because the undone state's cached render answered first and
  // the slow render of the edit landed on top of it.
  //
  // A superseded call resolves (with the hash currently on screen) rather than
  // rejecting: being replaced by a newer render is not an error for any caller.
  renderPreview: async (opts) => {
    const sid = get().sessionId
    if (!sid) return ''
    const seq = ++previewSeq
    previewAbort?.abort()
    const ac = new AbortController()
    previewAbort = ac
    set({ previewRendering: true })
    const superseded = () => seq !== previewSeq || get().sessionId !== sid
    try {
      const r = opts?.priority === 'low' ? await api.previewLow(sid, ac.signal) : await api.preview(sid, ac.signal)
      if (superseded()) return get().previewHash ?? ''
      set({ previewHash: r.edl_hash })
      return r.edl_hash
    } catch (e) {
      if (superseded() || ac.signal.aborted || isPreviewSuperseded(e)) {
        return get().previewHash ?? ''
      }
      throw e
    } finally {
      if (seq === previewSeq) {
        previewAbort = null
        set({ previewRendering: false })
      }
    }
  },

  doExport: async (opts = {}) => {
    const sid = get().sessionId
    if (!sid) return
    // QA-034: this run's token. Cancel (or a newer export) bumps exportRun, and
    // every await below re-checks it, so a loop that has been abandoned never
    // writes state again — the modal closes the moment Cancel is clicked.
    const run = ++exportRun
    const live = () => run === exportRun
    set({
      exporting: true, exportLinks: withoutExport(get().exportLinks, sid), exportStatus: 'queued',
      exportError: null, exportProgress: 0, exportJobId: null,
    })
    const POLL_MS = 500           // tight enough that the bar feels live
    const MAX_MS = 30 * 60 * 1000 // 30-min ceiling so we never poll forever
    const { saveAs, ...request } = opts
    try {
      const { job_id } = await api.exportAsync(sid, request)
      if (!live()) {
        // Cancelled while the job was being created: stop the render too.
        api.cancelJob(job_id).catch((e) => console.warn('[export] cancel failed:', errorMessage(e)))
        return
      }
      set({ exportJobId: job_id })
      const startedAt = Date.now()
      let lastAnswer = Date.now()
      for (;;) {
        await new Promise((r) => setTimeout(r, POLL_MS))
        if (!live()) return
        let job
        try {
          job = await api.getJob(job_id)
        } catch (e) {
          if (!live()) return
          // QA-034: jobs live in the engine's memory, so a 404 means it
          // restarted and this export is gone — terminal, not "retry for 30
          // minutes behind a modal".
          if (e instanceof Error && /^404\b/.test(e.message)) {
            set({ exportError: EXPORT_INTERRUPTED })
            toast.error(EXPORT_INTERRUPTED)
            return
          }
          // Unreachable: wait for the engine (the modal says so), but not
          // forever.
          if (isEngineOffline(e)) set({ exportStatus: 'reconnecting' })
          if (Date.now() - lastAnswer > EXPORT_LOST_MS) {
            set({ exportError: EXPORT_LOST })
            toast.error(EXPORT_LOST)
            return
          }
          continue
        }
        if (!live()) return
        lastAnswer = Date.now()
        if (job.status === 'completed' && job.result) {
          // Bind the link to the session it was rendered from (`sid`, captured
          // before the poll — the user may have switched projects since) and
          // to the EDL hash it rendered, so the UI can flag it "outdated".
          set({ exportLinks: withExport(get().exportLinks, exportLink(sid, job.result)),
                exportStatus: null, exportProgress: 1 })
          if (saveAs) exportSaveNames.set(job.result.url, saveAs)
          await triggerDownload(job.result.url, job.result.filename, sid, saveAs)
          return
        }
        if (job.status === 'failed') {
          set({ exportError: job.error ?? 'Export failed.', exportStatus: null })
          toast.error('Export failed.')
          return
        }
        if (job.status === 'cancelled') {
          set({ exportStatus: null })
          toast.info('Export cancelled.')
          return
        }
        set({ exportStatus: job.status, exportProgress: job.progress ?? 0 })
        if (Date.now() - startedAt > MAX_MS) {
          set({ exportError: 'Export is taking unusually long; it may have stalled.' })
          return
        }
      }
    } catch (e) {
      // Rendered verbatim by TopBar's ⚠ chip (and its title), so it goes
      // through errorMessage() like every other displayed string — api.ts
      // throws with the raw envelope appended, and a JSON wall in a 1-line
      // toolbar chip is unreadable.
      if (live()) set({ exportError: isEngineOffline(e) ? EXPORT_LOST : errorMessage(e) })
    } finally {
      if (live()) set({ exporting: false, exportStatus: null, exportJobId: null })
    }
  },

  downloadExport: async () => {
    // Only the export of the project on screen — another project's file is
    // never what "↓ MP4" means here — and the bridge gets the session the file
    // was rendered from, which by construction is that same one.
    const link = exportFor(get().exportLinks, get().sessionId)
    if (!link) return
    await triggerDownload(link.url, link.filename, link.sid, exportSaveNames.get(link.url))
  },

  cancelExport: async () => {
    if (!get().exporting) return
    const id = get().exportJobId
    // QA-034: Cancel ALWAYS closes, locally and at once. It used to wait for
    // the poll loop to see 'cancelled' — and when the engine had died the
    // cancel call 404'd silently, the loop kept polling, and the modal sat on
    // "Cancelling…" over the editor. The loop is abandoned via its token.
    exportRun += 1
    set({ exporting: false, exportStatus: null, exportJobId: null, exportProgress: 0 })
    toast.info('Export cancelled.')
    if (!id) return   // not created yet — doExport cancels it when it is
    try {
      await api.cancelJob(id)
    } catch (e) {
      // A 404 (the engine already lost it) or no engine at all: nothing is
      // rendering, so there is nothing more to stop.
      console.warn('[export] cancel request failed:', errorMessage(e))
    }
  },

  splitAtPlayhead: async () => {
    const s = get()
    // Split the SELECTED clip's track when the playhead is inside its range —
    // this used to hardcode v1, cutting the wrong track for a v2/overlay
    // selection even though the backend (and the timeline's right-click
    // "Split here") supports any track. Multi-selection: one split per
    // distinct track that has a selected clip containing the playhead.
    // No containing selected clip → v1, the historical default.
    //
    // The playhead is RENDER time and `split_at` takes LAYOUT time; the
    // containment test and the dispatched time are both decoded per track
    // in lib/splitTargets (v1 through v1's inverse, overlay lanes through
    // `layoutTime`). Testing layout starts against the raw playhead cut v1
    // instead of the caption on screen, 1.5 s before the frame shown.
    const selected = new Set([s.selection, ...s.multiSelection].filter(Boolean) as string[])
    for (const { track, time } of splitTargets(s.edl, selected, s.playhead)) {
      await get().splitTrackAt(track, time)
    }
  },

  splitTrackAt: async (track: string, time: number) => {
    const res = await get().dispatch('split_at', { track, time })
    // Follow the playhead into the RIGHT half. split_at keeps the original id
    // on the LEFT half, which now ends exactly at the cut — so the selection
    // silently pointed at a clip the playhead had just left, and Properties
    // opened with "Not visible at the playhead (28.90s) — this clip runs
    // 0.00–28.90s" the moment you pressed split, reading as a bug in the
    // split itself. Selecting the piece under the playhead is also what every
    // other editor does after a cut. Shared by ⌘B and the timeline's
    // right-click "Split at playhead" so the two can't diverge.
    //
    // This ran only when something was ALREADY selected, which left the
    // commonest path with no selection at all: split with nothing selected and
    // you get two clips and an empty Properties panel — so no keyframe button,
    // no transform fields, nothing to act on until you happen to click a clip.
    // Reported as "the keyframe should be applied even if the clip or split is
    // not selected". A cut is a deliberate act on a specific piece of footage;
    // ending it with nothing selected is never what the user meant.
    const halves = (res?.result as { halves?: Record<string, string> } | null)?.halves
    if (!halves) return
    const sel = get().selection
    if (sel) {
      // Only follow a selection this cut actually divided. A selection that
      // survived untouched is left alone — splitAtPlayhead loops one split per
      // track under a multi-selection, so the later calls see a `sel` that is
      // already the FIRST track's new right half. Claiming those too would walk
      // the selection to whichever track happened to be split last.
      if (halves[sel]) set({ selection: halves[sel] })
      return
    }
    // Nothing was selected. Every entry here is a clip this cut divided, and
    // the right half by construction STARTS at the cut — which is where the
    // playhead is — so it is the piece under the playhead.
    const ids = Object.values(halves)
    if (ids.length === 1) set({ selection: ids[0] })
  },

  rippleDeleteSelection: async () => {
    const sel = get().selection
    if (!sel) return
    // Refused (locked track, QA-023) -> null: the clip is still there.
    if (await get().dispatch('ripple_delete', { clip_id: sel })) set({ selection: null })
  },

  duplicateSelection: async () => {
    const sel = get().selection
    if (!sel) return
    await get().dispatch('duplicate_clip', { clip_id: sel })
  },
}))

// Remember the active session per-browser so `init()` (above) can reconnect to
// it on the next launch instead of blindly adopting whatever session is
// newest on the shared backend. Covers every path that changes `sessionId` —
// `init()` itself, and TopBar's switchSession/newSession — without needing
// each call site to remember to persist it individually.
useStore.subscribe((state, prevState) => {
  if (state.sessionId !== prevState.sessionId && state.sessionId) {
    try { localStorage.setItem('vai.sessionId', state.sessionId) } catch { /* private mode */ }
  }
  // QA-062: the prompt bar's run log belongs to the project it ran in.
  if (state.sessionId !== prevState.sessionId) fireSessionSwitch(state.sessionId)
})

// INSTANT PREVIEW: pick the engine, keep one controller per open project in
// client mode, and feed it every EDL that did not come from a dispatch answer
// (first load, imports, jobs, another window) and every preview render that
// lands (bakes). Nothing here runs in server mode.
function applyPreviewMode(): void {
  const s = useStore.getState()
  if (!s.previewSettings) return
  previewCaps ??= probePreviewCapabilities()
  const rateOk = ticksPerFrame(s.edl?.canvas?.fps ?? 30) !== null
  let r = resolvePreviewMode(s.previewSettings, previewCaps, rateOk)
  if (r.mode === 'client' && previewFailure) r = { mode: 'server', reason: previewFailure }
  if (r.mode !== s.previewEngine || r.reason !== s.previewEngineReason) {
    useStore.setState({ previewEngine: r.mode, previewEngineReason: r.reason })
  }
}

// The J that setPlaybackRate(<0) refused, until the setPlaying(true) the
// shuttle command sends right after it (same handler; cleared next microtask).
let reverseAsked = false

const SHUTTLE_UNSUPPORTED =
  'Instant preview plays forward at 1× for now — turn it off in Settings for reverse and fast shuttle.'

/** Say once (not a stack per key press) why J / L L did not do more. */
function noteShuttleUnsupported(): void {
  if (useToasts.getState().toasts.some((t) => t.message === SHUTTLE_UNSUPPORTED)) return
  toast.info(SHUTTLE_UNSUPPORTED)
}

function syncPreviewController(): void {
  const s = useStore.getState()
  const want = s.previewEngine === 'client' && s.sessionId ? s.sessionId : null
  if (previewCtl && previewCtl.sessionId === want) return
  previewCtl?.dispose()
  previewCtl = null
  if (!want) {
    if (s.clientView) useStore.setState({ clientView: null })
    return
  }
  // A new engine starts paused. Switching Instant preview on while the server
  // preview played left isPlaying true over an engine that never started — a
  // frozen picture under a Pause button, and the next Space only "paused"
  // (Final QA). The Settings click is not a transport gesture: stop.
  if (s.isPlaying) useStore.setState({ isPlaying: false })
  // …and at 1×. The engine plays forward only, and its setPlaybackRate never
  // writes a negative rate, so a J left over from the server preview (rate
  // -1) made setPlaying refuse every Play click and the first Space with no
  // word — a voice-over take would start over a stopped timeline (Final QA r2).
  if (s.playbackRate !== 1) useStore.setState({ playbackRate: 1 })
  const ctl = new PreviewController({
    sessionId: want,
    fetch: (url, init) => clientFetch(url, init),
    onFallback: (reason) => {
      if (previewCtl !== ctl) return
      previewFailure = reason
      console.warn(`[preview] client engine off for this project: ${reason}`)
      if (useStore.getState().isPlaying) useStore.setState({ isPlaying: false })
      applyPreviewMode()
    },
    onPlaying: (playing) => { if (previewCtl === ctl) useStore.setState({ isPlaying: playing }) },
    onView: (view) => { if (previewCtl === ctl) useStore.setState({ clientView: view }) },
    onServerTimeline: (edl, edlHash) => {
      const now = useStore.getState()
      if (previewCtl !== ctl || now.sessionId !== want || now.pendingOps > 0 || now.edlHash === edlHash) return
      useStore.setState({ edl: edl as unknown as EDL, edlHash, ...pruneSelection(now, edl as unknown as EDL) })
    },
  })
  previewCtl = ctl
  if (s.edl) ctl.applyTimeline(s.edl as unknown as EdlLike, null)
  if (s.previewHash) ctl.onPreviewLanded(s.previewHash)
}

useStore.subscribe((state, prev) => {
  if (state.sessionId !== prev.sessionId) previewFailure = null
  const fpsChanged = state.edl?.canvas?.fps !== prev.edl?.canvas?.fps
  if (fpsChanged || state.sessionId !== prev.sessionId) applyPreviewMode()
  const now = useStore.getState()
  if (now.previewEngine !== prev.previewEngine || now.sessionId !== prev.sessionId || (now.previewEngine === 'client' && !previewCtl)) {
    syncPreviewController()
  }
  const ctl = previewCtl
  if (!ctl) return
  if (now.edl && now.edl !== prev.edl && (now.edl as unknown) !== ctl.appliedEdl) {
    ctl.applyTimeline(now.edl as unknown as EdlLike, null)
  }
  if (now.previewHash !== prev.previewHash) ctl.onPreviewLanded(now.previewHash)
})

// The session-only counterpart of refreshSoon() for client-mode dispatches
// (spec §4.1 step 3): same 120 ms coalescing, no EDL fetch.
const refreshSessionSoon = (() => {
  let pending: ReturnType<typeof setTimeout> | null = null
  return () => {
    if (pending) clearTimeout(pending)
    pending = setTimeout(() => {
      pending = null
      useStore.getState().refreshSession().catch((e) => {
        console.warn('[store] session refresh failed:', e)
        if (!isEngineOffline(e)) toast.error(`Couldn't refresh the project: ${errorMessage(e)}`)
      })
    }, 120)
  }
})()

// QA-109: mirror the connection state into the store, and on the way back
// online say so once and take the server's timeline (edits made elsewhere, or
// by the recovered engine, while this window could not ask).
onEngineState((next) => {
  useStore.setState({ engine: next })
  if (next === 'online') {
    offlineNoticeShown = false
    toast.success('Reconnected to the editor engine.')
    // No project yet = the engine was down at launch and init() never got one.
    const s = useStore.getState()
    const again = s.sessionId ? s.refresh() : s.init()
    again.catch((e) => console.warn('[store] reload after reconnect failed:', e))
  }
})

/** Say once per offline spell that edits are paused (the banner stays up). */
function noticeOffline(onError?: (message: string) => void): void {
  const msg = 'Not applied — the editor engine is not responding. Edits resume when it reconnects.'
  onError?.(msg)
  if (offlineNoticeShown) return
  offlineNoticeShown = true
  toast.info(msg)
}

/** Watch the open project for changes made elsewhere (QA-105) and the engine
 *  for liveness (QA-109): ask on focus/visibility, and every `HEAD_POLL_MS`
 *  while the window is visible. Returns the cleanup. Installed by App. */
export const HEAD_POLL_MS = 4000
export function startSessionWatch(win: Window = window): () => void {
  const visible = () => typeof document === 'undefined' || document.visibilityState !== 'hidden'
  const check = () => { if (visible()) void useStore.getState().syncWithServer() }
  const onVisibility = () => { if (visible()) check() }
  win.addEventListener('focus', check)
  win.document?.addEventListener('visibilitychange', onVisibility)
  const id = win.setInterval(check, HEAD_POLL_MS)
  return () => {
    win.removeEventListener('focus', check)
    win.document?.removeEventListener('visibilitychange', onVisibility)
    win.clearInterval(id)
  }
}

/** The queue once item `id` is gone (finished or cancelled): the batch count
 *  moves on, and the panel goes idle the moment nothing is left. */
function afterImportLeaves(
  s: Pick<State, 'uploads' | 'uploadBatch'>, id: string,
): Pick<State, 'uploads' | 'uploading' | 'uploadBatch'> & { uploadProgress?: null } {
  if (!s.uploads.some((u) => u.id === id)) {
    return { uploads: s.uploads, uploading: s.uploads.length > 0, uploadBatch: s.uploadBatch }
  }
  const uploads = s.uploads.filter((u) => u.id !== id)
  return uploads.length
    ? { uploads, uploading: true, uploadBatch: { ...s.uploadBatch, done: s.uploadBatch.done + 1 } }
    : { uploads, uploading: false, uploadProgress: null, uploadBatch: { total: 0, done: 0 } }
}

/**
 * Put one file on the import queue (QA-044/094). The item shows at once as a
 * placeholder row; the file starts when every earlier one has finished, so a
 * multi-file drop lands in drop order. Resolves when THIS file is done
 * (imported, failed or cancelled) — never rejects: failures go to the Media
 * panel's error box like before.
 */
function enqueueImport(file: File, kind: 'video' | 'audio', opts?: ImportOptions): Promise<void> {
  const st = useStore.getState()
  const sid = st.sessionId
  if (!sid) return Promise.resolve()
  const place = opts?.place ?? null
  // A lane drop places the file itself, after the answer (never the server's
  // default "append to Main video / Music lane end").
  const addToTimeline = place ? false : (opts?.addToTimeline ?? st.importAddToTimeline)
  const id = `up_${++importSeq}`
  const item: UploadItem = { id, name: file.name, kind, stage: 'queued', progress: 0,
                             stageStartedAt: Date.now(), addToTimeline: addToTimeline || !!place,
                             // Where the timeline's ghost clip sits (QA-044).
                             lane: place?.track ?? null, laneStart: place ? place.cursor.next : null }
  const batch = st.uploads.length ? st.uploadBatch : { total: 0, done: 0 }
  useStore.setState({ uploads: [...st.uploads, item], uploadBatch: { ...batch, total: batch.total + 1 },
                      uploading: true, uploadError: null })
  const ac = new AbortController()
  const ctl = { cancelled: false, jobId: null as string | null }
  importCancels.set(id, () => {
    ctl.cancelled = true
    ac.abort()
    if (ctl.jobId) api.cancelJob(ctl.jobId).catch((e) => console.warn('[import] cancel failed:', errorMessage(e)))
  })
  const run = importChain.then(() => runImport(sid, file, id, kind, addToTimeline, ac.signal, ctl, place))
  importChain = run.catch(() => undefined)
  return run
}

async function runImport(
  sid: string, file: File, id: string, kind: 'video' | 'audio', addToTimeline: boolean,
  signal: AbortSignal, ctl: { cancelled: boolean; jobId: string | null },
  place: LanePlacement | null = null,
): Promise<void> {
  const patch = (p: Partial<UploadItem>) => useStore.setState({
    uploads: useStore.getState().uploads.map((u) => (u.id === id ? { ...u, ...p } : u)),
  })
  const stage = (next: UploadItem['stage']) => {
    const cur = useStore.getState().uploads.find((u) => u.id === id)
    if (cur && cur.stage !== next) patch({ stage: next, progress: 0, stageStartedAt: Date.now() })
  }
  let ok = false
  let answer: ImportAnswer | null = null
  try {
    if (ctl.cancelled) return
    stage('uploading')
    const onBytes = ({ loaded, total }: { loaded: number; total: number }) => {
      // Every byte sent: the rest is the server's (normalise, place).
      if (total > 0 && loaded >= total) stage('processing')
      else if (total > 0) patch({ progress: loaded / total })
    }
    if (kind === 'video') {
      answer = await api.upload(sid, file, addToTimeline, {
        signal, onBytes,
        onJob: (jobId) => { ctl.jobId = jobId; stage('processing') },
        onProgress: (p) => { stage('processing'); patch({ progress: p }) },
      })
    } else {
      // No `duck` (QA-083): the lane keeps the ducking choice the user made.
      answer = await api.audioUpload(sid, file, { addToMusic: addToTimeline, signal, onBytes })
    }
    if (place && !ctl.cancelled) {
      // Still inside the queue item, so the panel stays busy and dispatch()
      // keeps skipping base_hash until the clip is actually placed.
      stage('processing')
      const p = placementFor(answer as PlacedAnswer | null, kind, place)
      if (p.notice) toast.info(p.notice)
      if (p.args) await useStore.getState().dispatch('add_clip', p.args)
    }
    ok = true
  } catch (e) {
    if (ctl.cancelled || isAbort(e)) {
      toast.info(`Import of ${file.name} cancelled.`)
      return
    }
    // errorMessage(), not `e.message`: the api.ts contract appends the raw
    // envelope, and MediaBin renders this string verbatim.
    const why = isEngineOffline(e) ? 'the editor engine stopped responding.' : errorMessage(e)
    useStore.setState({ uploadError: `${file.name}: ${why}` })
  } finally {
    importCancels.delete(id)
    useStore.setState(afterImportLeaves(useStore.getState(), id))
  }
  if (ok && useStore.getState().sessionId === sid) {
    await useStore.getState().refresh().catch((e) => console.warn('[import] refresh failed:', errorMessage(e)))
    // QA-083 / QA-092: where an audio-only file went; "Trim to video" when an
    // audio file runs past the picture (it is no longer cut silently).
    const follow = importFollowUp(answer, file.name)
    if (follow?.action) {
      const { tool, args } = follow.action
      toast.action(follow.message, { label: follow.action.label,
                                     onClick: () => { void useStore.getState().dispatch(tool, args) } },
                   { ttlMs: 12000 })
    } else if (follow) {
      toast.info(follow.message)
    }
  }
}

// A finished export needs to reach the user's disk. In a real browser an
// `<a download>` click does that natively. But the packaged app runs inside
// pywebview's WKWebView/WebView2 (no Chrome/Safari chrome around it), which
// has no reliable way to surface an OS "Save As" dialog for that anchor click
// — the export renders fine but nothing visibly happens (issue: "export
// can't be downloaded"). When running inside pywebview we instead call the
// native `save_export` bridge (desktop.py's `_Api`), which drives a real save
// dialog and copies the file server-side. Browser-dev mode has no
// `window.pywebview`, so it falls through to the anchor path unchanged.
// Bridge detection and the call itself live in lib/nativeSave so the saved
// .vae project link (TopBar) uses the exact same path instead of a second
// copy of the `window.pywebview` probe.
// Kept module-scoped (not in a component) so it can fire from the store's
// polling loop.
// The name each export was asked to be saved under (QA-100), by its URL, so
// "↓ MP4" re-saves it under the same name.
const exportSaveNames = new Map<string, string>()

async function triggerDownload(url: string, filename: string, sessionId: string | null, saveAs?: string): Promise<void> {
  const pending = nativeSave(sessionId, filename, globalThis, saveAs)
  if (pending) {
    const outcome = await pending
    if (outcome.kind === 'saved') {
      toast.success(`Saved to ${outcome.path}`)
      return
    }
    // User cancelled the native dialog — nothing was saved, and falling
    // through to the anchor click below wouldn't help (same WKWebView/
    // WebView2 limitation), so just stop here without a false success toast.
    if (outcome.kind === 'cancelled') return
    // Bridge call itself failed (e.g. older packaged build without the
    // bridge) — fall through to the anchor path as a best effort.
  }
  const a = document.createElement('a')
  // The server's Content-Disposition names a download (an <a download>
  // attribute cannot override it), so the chosen name rides on ?name= (QA-100).
  a.href = saveAs ? `${url}${url.includes('?') ? '&' : '?'}name=${encodeURIComponent(saveAs)}` : url
  a.download = saveAs || filename
  a.style.display = 'none'
  document.body.appendChild(a)
  a.click()
  a.remove()
  toast.success('Export complete — downloading…')
}

// `clipAt(edl, trackId, t)` used to live here: a LAYOUT-time containment test
// with no callers. Deleted rather than left as bait — "which clip is under the
// playhead" is a render-time question now (`lib/timelineLayout.v1ClipAt`,
// which StickerLayer's framing gate uses), and a stray layout-time helper is
// exactly what a future caller would reach for first.
