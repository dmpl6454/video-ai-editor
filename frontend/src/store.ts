// Zustand store — single source of truth for the editor UI.
// Every UI gesture goes through `dispatch()` so Claude (M2) and the user
// share one mutation path.

import { create } from 'zustand'
import { api } from './api'
import { clipEnd, type AnyClip, type EDL, type Op } from './types'

interface State {
  sessionId: string | null
  sessionName: string
  edl: EDL | null
  ops: Op[]
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

  // --- timeline view + shortcut-driven actions ---
  timelineZoom: number              // px per second
  snapEnabled: boolean
  clipboard: string[]               // copied clip ids (for paste)
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
  splitAtPlayhead(): Promise<void>
  rippleDeleteSelection(): Promise<void>
  duplicateSelection(): Promise<void>
}

export const useStore = create<State>((set, get) => ({
  sessionId: null,
  sessionName: '',
  edl: null,
  ops: [],
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

  // --- timeline view + shortcut-driven actions ---
  timelineZoom: 80,
  snapEnabled: true,
  clipboard: [],
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
    set({ sessionId: sid, sessionName: existing?.name ?? sid })
    await get().refresh()
  },

  refresh: async () => {
    const sid = get().sessionId
    if (!sid) return
    const [info, edl] = await Promise.all([api.getSession(sid), api.getEDL(sid)])
    set({ edl, ops: info.ops, sessionName: info.name })
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
      await get().refresh()
      await get().renderPreview()
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      set({ uploadError: `${file.name}: ${msg}` })
    } finally {
      set({ uploading: false, uploadProgress: null })
    }
  },

  uploadAudio: async (file) => {
    const sid = get().sessionId
    if (!sid) return
    set({ uploading: true, uploadProgress: file.name, uploadError: null })
    try {
      await api.audioUpload(sid, file, { addToMusic: true, duck: true })
      await get().refresh()
      await get().renderPreview()
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      set({ uploadError: `${file.name}: ${msg}` })
    } finally {
      set({ uploading: false, uploadProgress: null })
    }
  },

  dispatch: async (tool, args = {}) => {
    const sid = get().sessionId
    if (!sid) return
    await api.dispatch(sid, tool, args)
    // Use the debounced refresh: chained tool calls (chat storms) coalesce
    // into one EDL fetch instead of N.
    get().refreshSoon()
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
    set({ exporting: true, exportUrl: null })
    try {
      const r = await api.export(sid, opts)
      set({ exportUrl: r.url })
    } finally {
      set({ exporting: false })
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
