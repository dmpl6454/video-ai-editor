// THE auto-captions run (docs/design/LEFT_RAIL_SPEC.md §2.8, §6.2, R2): lifted
// verbatim out of the top bar's CaptionsButton into ONE module-level run that
// the Captions panel and the top-bar activity chip both read, so there is never
// a second Cancel path. It dispatches `auto_caption` (agent/dispatch.py):
// re-transcribes the v1 source with Whisper, builds broadcast-style cues and
// lays down the caption track; the cues are TextClips TextLayer.tsx previews
// client-side, so no preview re-render is needed for them to appear.
//
// What this run exists to get right (each carried over unchanged):
//
// 1. The wait must LOOK like work, and it varies by two orders of magnitude by
//    machine. Measured decode time for 60 s of Hindi with large-v3: 185.6 s
//    sequential on CPU, 95.2 s batched on CPU, 8.4 s batched on a CUDA GPU. An
//    indefinite spinner for all of it read as "the caption generator button is
//    not working". `etaSeconds` therefore EXTRAPOLATES from this run's own
//    elapsed time and reported progress — it self-calibrates, because a
//    constant tuned for the CPU path would promise "~9 min" for something a GPU
//    finishes in 25 s, and a wrong ETA is worse than none. Batched decoding
//    reports progress COARSELY (about once per 30 s of audio), so the elapsed
//    counter carries the UI until the first update.
//
// 2. The caption language is not the spoken language. `target` picks the
//    OUTPUT language and the backend routes accordingly (Whisper's own
//    translation into English, then a local Argos hop into Hindi, then
//    romanisation for Hinglish). The choice is remembered.
//
// 3. SPEED is independent: large-v3 (default, most accurate) or large-v3-turbo
//    (~4x faster, measured). Turbo cannot serve English's translate task (it
//    returns a handful of ellipses), so auto_caption substitutes large-v3 in
//    that one case and reports which model ran (`result.model`); the success
//    toast says so instead of leaving "I picked Fastest" unexplained.
//
// 4. Cancelling is not instant. The decoder can only be interrupted BETWEEN
//    segments, and faster-whisper emits those per 30 s window — measured 58 s
//    from Cancel to the job reporting `cancelled`. So Cancel switches to a
//    "Stopping…" state and keeps the elapsed counter running.
//
// 5. QA-065: a first run that would download a caption model ASKS first
//    (`consent`); "Fastest" used to start a 1.6 GB download with no word.
//
// Failure modes: no v1 clip → 400; missing whisper model/binary → 422; no
// translation package for an exotic source language → 422 naming English as
// the way out. All carry a readable message store.dispatch already toasts.
import { create, type StoreApi, type UseBoundStore } from 'zustand'
import type { EDL } from '../types'
import { isMediaClip } from '../types'
import { downloadKeyFor, pendingDownload, type DownloadInfo, type DownloadReport } from './modelDownloads'
import { useActivityStore, type CaptionsActivity, type CaptionsOutcome } from './activityStore'
import { useStore } from '../store'
import { api } from '../api'
import { toast } from '../toast'

export type Target = 'as-spoken' | 'en' | 'hi' | 'hinglish' | 'es'
export type Speed = 'quality' | 'fast'

export const TARGETS: readonly { id: Target; label: string; hint: string }[] = [
  { id: 'as-spoken', label: 'As spoken', hint: 'Caption in whatever language the video is in' },
  { id: 'en', label: 'English', hint: 'Translate to English (works from any language)' },
  { id: 'hi', label: 'हिंदी Hindi', hint: 'Hindi in Devanagari script' },
  { id: 'hinglish', label: 'Hinglish', hint: 'Hindi written in Latin letters — "apni last meeting ke baad"' },
  { id: 'es', label: 'Español', hint: 'Translate to Spanish (works from any language)' },
]

export const SPEEDS: readonly { id: Speed; label: string; hint: string }[] = [
  { id: 'quality', label: 'Best quality', hint: 'The most accurate caption model' },
  { id: 'fast', label: 'Fastest', hint: 'About 4× faster. English captions still use the accurate '
      + 'model, which the fast one can’t translate.' },
]

/** The literal faster-whisper model name — must match a name transcribe.py
 *  recognises (verified: WhisperModel("large-v3-turbo", …) loads). */
export const TURBO_MODEL = 'large-v3-turbo'

/** The remembered choices (also read by AiToolCard's Auto captions card). */
export const TARGET_KEY = 'vai.captionTarget'
export const SPEED_KEY = 'vai.captionSpeed'

/** Elapsed-counter period (ms). */
const TICK_MS = 1000

interface KV { getItem(k: string): string | null; setItem(k: string, v: string): void }

export function loadTarget(kv: KV | null): Target {
  try {
    const v = kv?.getItem(TARGET_KEY)
    if (v && TARGETS.some((t) => t.id === v)) return v as Target
  } catch { /* private mode / storage disabled — the default is fine */ }
  return 'as-spoken'
}

export function loadSpeed(kv: KV | null): Speed {
  try {
    const v = kv?.getItem(SPEED_KEY)
    if (v && SPEEDS.some((s) => s.id === v)) return v as Speed
  } catch { /* private mode / storage disabled — the default is fine */ }
  return 'quality'
}

/** Seconds remaining, extrapolated from how long the job has taken to reach
 *  `progress`. Null until there is enough signal to be honest about it. */
export function etaSeconds(elapsed: number, progress: number): number | null {
  if (progress <= 0.02 || elapsed < 3) return null
  const total = elapsed / progress
  const left = Math.max(0, total - elapsed)
  return left > 1 ? left : null
}

export function formatEta(sec: number): string {
  if (sec < 60) return `${Math.ceil(sec)}s left`
  const m = Math.floor(sec / 60)
  const s = Math.round(sec % 60)
  return s >= 30 ? `~${m + 1} min left` : `~${m || 1} min left`
}

/** auto_caption's args. 'as-spoken' sends no target (the backend default every
 *  pre-existing caller relies on); 'quality' sends no model, so it rides
 *  WHISPER_CAPTION_MODEL (default large-v3) exactly as before. */
export function captionArgs(target: Target, speed: Speed): Record<string, unknown> {
  const args: Record<string, unknown> = target === 'as-spoken' ? {} : { target }
  if (speed === 'fast') args.model = TURBO_MODEL
  return args
}

/** What a speed choice would download first, or null (cached or unknown). */
export function speedDownload(report: DownloadReport | null, speed: Speed): DownloadInfo | null {
  return pendingDownload(report, downloadKeyFor('auto_caption', speed === 'fast' ? { model: TURBO_MODEL } : {}))
}

/** auto_caption transcribes the first media clip on v1: without one the run
 *  is a guaranteed 400, so the UI disables it and says why. */
export function hasCaptionFootage(edl: EDL | null | undefined): boolean {
  return !!edl?.tracks.find((t) => t.id === 'v1')?.clips.some(isMediaClip)
}

/** The success toast. `r.model` is the model that ACTUALLY ran: Fastest that
 *  did not run turbo means the English target needed the accurate model. */
export function doneMessage(result: unknown, speed: Speed): string {
  const r = result as { cues?: number; language?: string; spoken?: string; model?: string } | null
  const lang = r?.language === 'hi-Latn' ? 'Hinglish' : r?.language === 'es' ? 'Spanish' : r?.language
  const from = r?.spoken && r.spoken !== r.language ? ` from ${r.spoken}` : ''
  const fellBack = speed === 'fast' && r?.model && !r.model.toLowerCase().includes('turbo')
  const modelNote = fellBack ? ` · used ${r!.model} (turbo can't translate)` : ''
  return typeof r?.cues === 'number'
    ? `Captions added — ${r.cues} cues${lang ? ` (${lang}${from})` : ''}${modelNote}`
    : 'Captions added'
}

export interface CaptionRunState {
  target: Target
  speed: Speed
  /** GET /api/downloads, or null until it answers. */
  downloads: DownloadReport | null
  /** The download a run is waiting on the user's yes for. */
  consent: DownloadInfo | null
  busy: boolean
  progress: number
  startedAt: number
  elapsed: number
  cancelling: boolean
  jobId: string | null
  pickTarget(t: Target): void
  pickSpeed(s: Speed): void
  refreshDownloads(): Promise<DownloadReport | null>
  /** Generate captions: asks first when a model would download, else starts. */
  run(): Promise<void>
  /** Start without the download check (the consent dialog's "Download and caption"). */
  start(): Promise<void>
  cancel(): Promise<void>
  dismissConsent(): void
  acceptConsent(): void
}

export interface CaptionRunDeps {
  dispatch(tool: string, args: Record<string, unknown>,
           opts: { onProgress: (p: { jobId: string; progress: number }) => void }): Promise<{ result: unknown } | null>
  getDownloads(): Promise<DownloadReport>
  cancelJob(jobId: string): Promise<unknown>
  hasFootage(): boolean
  toast: { success(m: string): void; error(m: string): void }
  storage: KV | null
  now(): number
  every(fn: () => void, ms: number): () => void
  /** Mirror of the run for the activity chip and the rail dot. */
  publish(c: CaptionsActivity | null, outcome?: CaptionsOutcome): void
}

export function createCaptionRun(deps: CaptionRunDeps): UseBoundStore<StoreApi<CaptionRunState>> {
  let stopTicker: (() => void) | null = null
  const store = create<CaptionRunState>()((set, get) => {
    const refreshDownloads = async (): Promise<DownloadReport | null> => {
      try {
        const r = await deps.getDownloads()
        set({ downloads: r })
        return r
      } catch (e) {
        console.warn('[captions] download report unavailable:', e)
        return null
      }
    }

    const start = async () => {
      if (get().busy || !deps.hasFootage()) return
      const startedAt = deps.now()
      set({ busy: true, progress: 0, elapsed: 0, startedAt, cancelling: false, jobId: null })
      stopTicker = deps.every(() => {
        set({ elapsed: Math.floor((deps.now() - get().startedAt) / 1000) })
      }, TICK_MS)
      const { target, speed } = get()
      let outcome: CaptionsOutcome = 'failed'
      try {
        const res = await deps.dispatch('auto_caption', captionArgs(target, speed), {
          onProgress: ({ jobId, progress }) => set({ jobId, progress }),
        })
        if (res) {
          outcome = 'done'
          deps.toast.success(doneMessage(res.result, speed))
        } else if (get().cancelling) {
          outcome = 'cancelled'
        }
        // res === null → the failure (or "was cancelled") toast already fired
        // inside store.dispatch.
      } finally {
        void refreshDownloads()
        stopTicker?.()
        stopTicker = null
        set({ busy: false, progress: 0, cancelling: false, jobId: null })
        deps.publish(null, outcome)
      }
    }

    return {
      target: loadTarget(deps.storage),
      speed: loadSpeed(deps.storage),
      downloads: null,
      consent: null,
      busy: false,
      progress: 0,
      startedAt: 0,
      elapsed: 0,
      cancelling: false,
      jobId: null,
      pickTarget: (t) => {
        set({ target: t })
        try { deps.storage?.setItem(TARGET_KEY, t) } catch { /* not worth failing over */ }
      },
      pickSpeed: (s) => {
        set({ speed: s })
        try { deps.storage?.setItem(SPEED_KEY, s) } catch { /* not worth failing over */ }
      },
      refreshDownloads,
      run: async () => {
        if (get().busy || !deps.hasFootage()) return
        const report = await refreshDownloads()
        const { target, speed } = get()
        const need = pendingDownload(report, downloadKeyFor('auto_caption', captionArgs(target, speed)))
        if (need) { set({ consent: need }); return }
        await start()
      },
      start,
      cancel: async () => {
        const id = get().jobId
        if (!id || !get().busy) return
        set({ cancelling: true })
        try {
          await deps.cancelJob(id)
        } catch {
          set({ cancelling: false })
          deps.toast.error('Could not cancel — it may have already finished')
        }
      },
      dismissConsent: () => set({ consent: null }),
      acceptConsent: () => {
        set({ consent: null })
        void start()
      },
    }
  })

  // The chip and the rail read the run through lib/activityStore; publish on
  // every change of what they show while a run is live.
  store.subscribe((s, prev) => {
    if (!s.busy) return
    if (s.busy === prev.busy && s.progress === prev.progress
        && s.elapsed === prev.elapsed && s.cancelling === prev.cancelling) return
    deps.publish({
      progress: s.progress > 0 ? s.progress : null,
      etaS: s.cancelling ? null : etaSeconds(s.elapsed, s.progress),
      elapsedS: s.elapsed,
      cancelling: s.cancelling,
      cancel: () => { void store.getState().cancel() },
    })
  })
  return store
}

function browserStorage(): KV | null {
  try { return typeof localStorage === 'undefined' ? null : localStorage } catch { return null }
}

/** The app's one captions run. */
export const useCaptionRun = createCaptionRun({
  dispatch: (tool, args, opts) => useStore.getState().dispatch(tool, args, opts),
  getDownloads: async () => (await api.getDownloads()).downloads,
  cancelJob: (id) => api.cancelJob(id),
  hasFootage: () => hasCaptionFootage(useStore.getState().edl),
  toast,
  storage: browserStorage(),
  now: () => Date.now(),
  every: (fn, ms) => { const id = window.setInterval(fn, ms); return () => window.clearInterval(id) },
  publish: (c, outcome) => useActivityStore.getState().setCaptions(c, outcome),
})
