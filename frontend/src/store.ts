// Zustand store — single source of truth for the editor UI.
// Every UI gesture goes through `dispatch()` so Claude (M2) and the user
// share one mutation path.

import { create } from 'zustand'
import { api } from './api'
import { toast } from './toast'
import { clipEnd, type AnyClip, type EDL, type Op } from './types'

interface State {
  sessionId: string | null
  sessionName: string
  edl: EDL | null
  ops: Op[]
  redoAvailable: boolean
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
  uploading: boolean
  uploadProgress: string | null
  uploadError: string | null
  exporting: boolean
  exportUrl: string | null
  exportStatus: string | null   // 'queued' | 'running' — coarse job phase for the UI
  exportError: string | null
  exportProgress: number        // 0..1 live ffmpeg progress
  exportJobId: string | null    // current export job (for cancel)
  exportGen: number             // ops.length when the current export finished (staleness check)

  // Client-side live transform: set while a transform slider is being dragged
  // so Preview applies a CSS transform to the <video> for instant feedback,
  // without a server render. Cleared (null) the moment the drag commits.
  liveTransform: { clipId: string; scale?: number; rotation?: number; opacity?: number } | null

  // setters
  setLiveTransform(t: State['liveTransform']): void
  setSelection(id: string | null): void
  toggleSelection(id: string): void
  clearSelection(): void
  setPlayhead(t: number): void
  setPlaying(p: boolean): void
  setPlaybackRate(r: number): void
  setInMark(t: number | null): void
  setOutMark(t: number | null): void
  clearUploadError(): void
  clearExportError(): void
  resetTransient(): void

  // --- timeline view + shortcut-driven actions ---
  timelineZoom: number              // px per second
  snapEnabled: boolean
  clipboard: string[]               // copied clip ids (for paste)
  flashClipId: string | null        // clip to briefly flash on the timeline
  flashAt: number                   // timestamp the flash started (ms)
  flashClip(id: string): void       // draw attention to a newly-added clip
  setTimelineZoom(z: number): void
  zoomTimeline(factor: number): void   // multiply zoom (in/out)
  toggleSnap(): void
  selectAll(): void
  copySelection(): void
  pasteClipboard(): Promise<void>
  goToStart(): void
  goToEnd(): void
  nudgeSelection(deltaSeconds: number): Promise<void>

  // workflow
  init(): Promise<void>
  refresh(): Promise<void>
  refreshSoon(): void
  upload(file: File): Promise<void>
  uploadAudio(file: File): Promise<void>
  dispatch(tool: string, args?: Record<string, unknown>): Promise<void>
  renderPreview(): Promise<string>
  doExport(opts?: { height?: number; crf?: number }): Promise<void>
  cancelExport(): Promise<void>
  splitAtPlayhead(): Promise<void>
  rippleDeleteSelection(): Promise<void>
  duplicateSelection(): Promise<void>
}

export const useStore = create<State>((set, get) => ({
  sessionId: null,
  sessionName: '',
  edl: null,
  ops: [],
  redoAvailable: false,
  pendingOps: 0,
  selection: null,
  playhead: 0,
  isPlaying: false,
  previewHash: null,
  multiSelection: [],
  inMark: null,
  outMark: null,
  playbackRate: 1,
  liveTransform: null,
  uploading: false,
  uploadProgress: null,
  uploadError: null,
  exporting: false,
  exportUrl: null,
  exportStatus: null,
  exportError: null,
  exportProgress: 0,
  exportJobId: null,
  exportGen: 0,

  setSelection: (id) => set({ selection: id, multiSelection: id ? [] : [] }),
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
    set({ playhead: clamped })
  },
  setPlaying: (p) => set({ isPlaying: p }),
  setPlaybackRate: (r) => set({ playbackRate: r }),
  setLiveTransform: (t) => set({ liveTransform: t }),
  setInMark: (t) => set({ inMark: t }),
  setOutMark: (t) => set({ outMark: t }),
  clearUploadError: () => set({ uploadError: null }),
  clearExportError: () => set({ exportError: null }),

  // Clears per-session view/selection state. Call when switching sessions so a
  // stale playhead/selection/marks from the previous project don't bleed onto
  // the new timeline (which read as "a second frozen playhead").
  resetTransient: () => set({
    playhead: 0,
    selection: null,
    multiSelection: [],
    inMark: null,
    outMark: null,
  }),

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
  setTimelineZoom: (z) => set({ timelineZoom: Math.max(10, Math.min(600, z)) }),
  zoomTimeline: (factor) => {
    const z = get().timelineZoom
    set({ timelineZoom: Math.max(10, Math.min(600, z * factor)) })
  },
  toggleSnap: () => set({ snapEnabled: !get().snapEnabled }),
  selectAll: () => {
    const edl = get().edl
    if (!edl) return
    const ids: string[] = []
    for (const t of edl.tracks) for (const c of t.clips) if ('src' in c) ids.push(c.id)
    set({ selection: ids[0] ?? null, multiSelection: ids.slice(1) })
  },
  copySelection: () => {
    const s = get()
    const ids = Array.from(new Set([s.selection, ...s.multiSelection].filter(Boolean) as string[]))
    set({ clipboard: ids })
  },
  pasteClipboard: async () => {
    const s = get()
    // Paste = duplicate each clipboard clip (the dispatch duplicates with an
    // offset). Reuses the existing duplicate path so undo/ops work.
    for (const id of s.clipboard) {
      await s.dispatch('duplicate_clip', { clip_id: id })
    }
  },
  goToStart: () => set({ playhead: 0 }),
  goToEnd: () => {
    const dur = get().edl?.duration ?? 0
    set({ playhead: dur })
  },
  nudgeSelection: async (deltaSeconds) => {
    const s = get()
    if (!s.selection || !s.edl) return
    // Find the clip's current start, move by delta.
    for (const t of s.edl.tracks) {
      for (const c of t.clips) {
        if (c.id === s.selection && 'start' in c) {
          const newStart = Math.max(0, (c.start as number) + deltaSeconds)
          await s.dispatch('move_clip', { clip_id: s.selection, new_start: newStart })
          return
        }
      }
    }
  },

  init: async () => {
    // Try to recover the most recent session, else create a new one.
    const list = await api.listSessions()
    const existing = list.sessions[0]
    const sid = existing?.id ?? (await api.createSession()).id
    get().resetTransient()
    set({ sessionId: sid, sessionName: existing?.name ?? sid })
    await get().refresh()
  },

  refresh: async () => {
    const sid = get().sessionId
    if (!sid) return
    const [info, edl] = await Promise.all([api.getSession(sid), api.getEDL(sid)])
    set({ edl, ops: info.ops, sessionName: info.name, redoAvailable: info.redo_available })
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
        useStore.getState().refresh().catch(() => {})
      }, 120)
    }
  })(),

  upload: async (file) => {
    const sid = get().sessionId
    if (!sid) return
    set({ uploading: true, uploadProgress: file.name, uploadError: null })
    try {
      await api.upload(sid, file, true)
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      set({ uploadError: `${file.name}: ${msg}` })
      set({ uploading: false, uploadProgress: null })
      return
    }
    set({ uploading: false, uploadProgress: null })
    // The upload itself succeeded — media is ingested and on the timeline.
    // A subsequent preview-render failure (e.g. a corrupt cached overlay PNG)
    // is a SEPARATE concern and must not be reported as "upload failed".
    await get().refresh()
    try {
      await get().renderPreview()
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      toast.error(`Preview render failed: ${msg}`)
    }
  },

  uploadAudio: async (file) => {
    const sid = get().sessionId
    if (!sid) return
    set({ uploading: true, uploadProgress: file.name, uploadError: null })
    try {
      await api.audioUpload(sid, file, { addToMusic: true, duck: true })
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      set({ uploadError: `${file.name}: ${msg}` })
      set({ uploading: false, uploadProgress: null })
      return
    }
    set({ uploading: false, uploadProgress: null })
    await get().refresh()
    try {
      await get().renderPreview()
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      toast.error(`Preview render failed: ${msg}`)
    }
  },

  dispatch: async (tool, args = {}) => {
    const sid = get().sessionId
    if (!sid) return
    set({ pendingOps: get().pendingOps + 1 })
    try {
      // We KEEP the previous export's download link after an edit, but the UI
      // marks it "outdated" by comparing ops.length to exportGen (see TopBar).
      const res = await api.dispatch<{ redo_available?: boolean }>(sid, tool, args)
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
        await get().refresh()
        return
      }
      // Use the debounced refresh: chained tool calls (chat storms) coalesce
      // into one EDL fetch instead of N.
      get().refreshSoon()
      // Offer a quick Undo on destructive deletes — covers every entry point
      // (keyboard, Properties Delete, timeline context menu) in one spot. The
      // backend's own undo is the restore; 'undo' isn't a delete so it can't loop.
      if (tool === 'ripple_delete' || tool === 'bulk_delete') {
        const count = tool === 'bulk_delete'
          ? ((args.clip_ids as unknown[] | undefined)?.length ?? 0)
          : 1
        toast.action(
          count > 1 ? `${count} clips deleted` : 'Clip deleted',
          { label: 'Undo', onClick: () => { void get().dispatch('undo') } },
        )
      }
    } catch (e) {
      // Edits used to fail SILENTLY here — no catch at all, so a rejected
      // dispatch (bad args, a validation error like the new lane-type check,
      // a network hiccup) left the user staring at a UI that looked like
      // nothing happened, with no error anywhere (issue 15-adjacent: "no
      // persistent error surface for a failed edit").
      const msg = e instanceof Error ? e.message : String(e)
      toast.error(msg)
    } finally {
      set({ pendingOps: Math.max(0, get().pendingOps - 1) })
    }
  },

  renderPreview: async () => {
    const sid = get().sessionId
    if (!sid) return ''
    const r = await api.preview(sid)
    set({ previewHash: r.edl_hash })
    return r.edl_hash
  },

  doExport: async (opts = {}) => {
    const sid = get().sessionId
    if (!sid) return
    set({
      exporting: true, exportUrl: null, exportStatus: 'queued',
      exportError: null, exportProgress: 0, exportJobId: null,
    })
    const POLL_MS = 500           // tight enough that the bar feels live
    const MAX_MS = 30 * 60 * 1000 // 30-min ceiling so we never poll forever
    try {
      const { job_id } = await api.exportAsync(sid, opts)
      set({ exportJobId: job_id })
      const startedAt = Date.now()
      for (;;) {
        await new Promise((r) => setTimeout(r, POLL_MS))
        let job
        try {
          job = await api.getJob(job_id)
        } catch {
          if (Date.now() - startedAt > MAX_MS) {
            set({ exportError: 'Export timed out while checking status.' })
            return
          }
          continue
        }
        if (job.status === 'completed' && job.result) {
          // Stamp the export with the current history length so the UI can flag
          // it "outdated" once the user edits past this point.
          set({ exportUrl: job.result.url, exportStatus: null, exportProgress: 1,
                exportGen: get().ops.length })
          triggerDownload(job.result.url, job.result.filename)
          toast.success('Export complete — downloading…')
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
      set({ exportError: e instanceof Error ? e.message : String(e) })
    } finally {
      set({ exporting: false, exportStatus: null, exportJobId: null })
    }
  },

  cancelExport: async () => {
    const id = get().exportJobId
    if (!id) return
    try {
      await api.cancelJob(id)
      // The poll loop sees status 'cancelled' and tears down the rest.
    } catch {
      // Best-effort; the export will still finish or time out on its own.
    }
  },

  splitAtPlayhead: async () => {
    const t = get().playhead
    await get().dispatch('split_at', { track: 'v1', time: t })
  },

  rippleDeleteSelection: async () => {
    const sel = get().selection
    if (!sel) return
    await get().dispatch('ripple_delete', { clip_id: sel })
    set({ selection: null })
  },

  duplicateSelection: async () => {
    const sel = get().selection
    if (!sel) return
    await get().dispatch('duplicate_clip', { clip_id: sel })
  },
}))

// Programmatically click a temporary <a download> so a finished export lands
// in the user's downloads without them hunting for a link. Kept module-scoped
// (not in a component) so it can fire from the store's polling loop.
function triggerDownload(url: string, filename: string): void {
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.style.display = 'none'
  document.body.appendChild(a)
  a.click()
  a.remove()
}

// Helper used by the timeline to find the clip under a given timeline time.
export function clipAt(edl: EDL | null, trackId: string, t: number): AnyClip | null {
  if (!edl) return null
  const tk = edl.tracks.find((x) => x.id === trackId)
  if (!tk) return null
  for (const c of tk.clips) {
    if ('src' in c) {
      if (c.start <= t && t < clipEnd(c)) return c
    }
  }
  return null
}
