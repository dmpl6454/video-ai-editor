// Zustand store — single source of truth for the editor UI.
// Every UI gesture goes through `dispatch()` so Claude (M2) and the user
// share one mutation path.

import { create } from 'zustand'
import { api } from './api'
import { toast } from './toast'
import { clipEnd, type AnyClip, type EDL, type Op } from './types'

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

  // Client-side live color filter — the Color panel's mirror of liveTransform.
  // Set while a brightness/contrast/saturation slider drags so Preview applies
  // a CSS filter() approximation instantly; cleared the same way liveTransform
  // is (the re-rendered <video>'s onLoadedData + safety-net timeout). Values
  // are the EDL grade params (ffmpeg eq semantics) — Preview converts to CSS.
  liveFilter: { clipId: string; brightness?: number; contrast?: number; saturation?: number } | null

  // setters
  setLiveTransform(t: State['liveTransform']): void
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

  // --- resizable panel sizes (Task 9), persisted to localStorage so a drag
  // survives reload. Plain px, clamped in setPanelSize. ---
  leftW: number
  rightW: number
  timelineH: number
  setPanelSize(key: 'leftW' | 'rightW' | 'timelineH', px: number): void

  // --- right-panel (Properties/History) collapse toggle, persisted like the
  // panel sizes above. When false, the sidebar shrinks to a thin rail (see
  // RIGHT_RAIL_W in App.tsx) instead of removing the grid column outright,
  // so there's no layout reflow jump on toggle. ---
  rightPanelOpen: boolean
  setRightPanelOpen(open: boolean): void

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
  doExport(opts?: { height?: number; crf?: number; container?: 'mp4' | 'mov' }): Promise<void>
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
  liveFilter: null,
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

  // Panel sizes: read from localStorage (falls back to the historical fixed
  // CSS defaults — 220/280/280 — when unset, invalid, or running server-side
  // where localStorage doesn't exist).
  leftW: readStoredPanelSize('vai.leftW', 220),
  rightW: readStoredPanelSize('vai.rightW', 280),
  timelineH: readStoredPanelSize('vai.timelineH', 280),
  rightPanelOpen: readStoredBool('vai.rightPanelOpen', true),

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
    set({ isPlaying: p })
  },
  replayFromStart: () => {
    const s = get()
    const dur = s.edl?.duration ?? 0
    // 1/30 = one frame at the timeline's normalised 30fps.
    if (!s.isPlaying && dur > 0 && s.playhead >= dur - 1 / 30) {
      s.setPlayhead(0)
      return true
    }
    return false
  },
  setPlaybackRate: (r) => set({ playbackRate: r }),
  setLiveTransform: (t) => set({ liveTransform: t }),
  setLiveFilter: (f) => set({ liveFilter: f }),
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

  // Toggles the right panel (Properties/History) collapsed/open, persisted
  // the same way panel sizes are. Does not touch `rightW` — the splitter's
  // dragged width is preserved underneath the collapse so re-expanding
  // returns to the same size rather than a fixed default.
  setRightPanelOpen: (open) => {
    if (typeof localStorage !== 'undefined') localStorage.setItem('vai.rightPanelOpen', String(open))
    set({ rightPanelOpen: open })
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
          await triggerDownload(job.result.url, job.result.filename, sid)
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
    const s = get()
    const t = s.playhead
    // Split the SELECTED clip's track when the playhead is inside its range —
    // this used to hardcode v1, cutting the wrong track for a v2/overlay
    // selection even though the backend (and the timeline's right-click
    // "Split here") supports any track. Multi-selection: one split per
    // distinct track that has a selected clip containing the playhead.
    // No containing selected clip → v1, the historical default.
    const selected = new Set([s.selection, ...s.multiSelection].filter(Boolean) as string[])
    const trackIds: string[] = []
    if (s.edl && selected.size) {
      for (const tk of s.edl.tracks) {
        for (const c of tk.clips) {
          if (!selected.has(c.id)) continue
          if (c.start <= t && t < clipEnd(c) && !trackIds.includes(tk.id)) trackIds.push(tk.id)
        }
      }
    }
    if (!trackIds.length) trackIds.push('v1')
    for (const track of trackIds) {
      await s.dispatch('split_at', { track, time: t })
    }
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

// Narrow shape of the bridge desktop.py's `_Api` exposes over pywebview's
// js_api — only the one method this file calls, not the whole class.
interface PywebviewBridge {
  pywebview?: { api?: { save_export?: (sid: string, filename: string) => Promise<string | null> } }
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
// Kept module-scoped (not in a component) so it can fire from the store's
// polling loop.
async function triggerDownload(url: string, filename: string, sessionId: string | null): Promise<void> {
  const py = (window as unknown as PywebviewBridge).pywebview
  if (py?.api?.save_export && sessionId) {
    try {
      const saved = await py.api.save_export(sessionId, filename)
      if (saved) {
        toast.success(`Saved to ${saved}`)
        return
      }
      // User cancelled the native dialog — nothing was saved, and falling
      // through to the anchor click below wouldn't help (same WKWebView/
      // WebView2 limitation), so just stop here without a false success toast.
      return
    } catch {
      // Bridge call itself failed (e.g. older packaged build without the
      // bridge) — fall through to the anchor path as a best effort.
    }
  }
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.style.display = 'none'
  document.body.appendChild(a)
  a.click()
  a.remove()
  toast.success('Export complete — downloading…')
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
