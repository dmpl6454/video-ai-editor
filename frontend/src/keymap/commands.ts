import { useStore } from '../store'
import { usePromptStore } from '../lib/promptStore'
import { layoutPlayhead } from '../lib/timelineLayout'
import { frameDuration, stepFrames } from '../lib/frameStep'

/**
 * Editor command registry — the actions keyboard shortcuts can trigger,
 * decoupled from any particular key. Presets (CapCut / Premiere / Final Cut /
 * custom) map key chords to these command ids.
 *
 * Each command's `run` gets the live store state. Commands are intentionally
 * small and reuse the same store actions the UI buttons call, so undo / ops
 * log stay consistent no matter how the action was triggered.
 */
export type Store = ReturnType<typeof useStore.getState>

export interface Command {
  id: string
  label: string
  category: 'Transport' | 'Editing' | 'Marks' | 'Navigation' | 'View' | 'Selection' | 'History'
  // Promise<unknown>: store.dispatch now returns the response payload, and
  // commands hand its promise straight back — the engine ignores the value.
  run: (s: Store) => void | Promise<unknown>
}

// One frame of THIS project (QA-009): edl.canvas.fps, not a hardcoded 30.
const fpsOf = (s: Store): unknown => s.edl?.canvas?.fps

const selectedIds = (s: Store): string[] =>
  Array.from(new Set([s.selection, ...s.multiSelection].filter(Boolean) as string[]))

export const COMMANDS: Command[] = [
  // ---------- Transport ----------
  { id: 'playPause', label: 'Play / Pause', category: 'Transport',
    run: (s) => {
      // Pressing play when the playhead is parked at (or within a frame of) the
      // end rewinds to the start (CapCut/every NLE does this) — shared with the
      // transport button via replayFromStart(). Only rewinds when STARTING
      // playback forward from the end; pausing / resuming a rate<0 reverse are
      // unaffected. Unlike the button, this layer has no <video> ref of its
      // own to rewind synchronously — it relies on the rAF clock's TRUST_TOL
      // proximity check (Preview.tsx) to free-run correctly from the fresh
      // playhead=0 without being fooled by a stale currentTime, plus the
      // playhead-sync effect's async seek eventually landing.
      s.replayFromStart()
      s.setPlaying(!s.isPlaying)
      s.setPlaybackRate(1)
    } },
  { id: 'shuttleReverse', label: 'Shuttle reverse (J)', category: 'Transport',
    run: (s) => { const r = s.playbackRate; s.setPlaybackRate(r > 0 ? -1 : Math.max(-8, r * 2)); s.setPlaying(true) } },
  { id: 'shuttleStop', label: 'Shuttle stop (K)', category: 'Transport',
    run: (s) => { s.setPlaying(false); s.setPlaybackRate(1) } },
  { id: 'shuttleForward', label: 'Shuttle forward (L)', category: 'Transport',
    run: (s) => { const r = s.playbackRate; s.setPlaybackRate(r < 0 ? 1 : Math.min(8, (r || 1) * 2 > 1 ? (r || 1) * 2 : 1)); s.setPlaying(true) } },
  { id: 'frameBack', label: 'Step back 1 frame', category: 'Transport',
    run: (s) => { s.setPlaying(false); s.setPlayhead(stepFrames(s.playhead, -1, fpsOf(s))) } },
  { id: 'frameForward', label: 'Step forward 1 frame', category: 'Transport',
    run: (s) => { s.setPlaying(false); s.setPlayhead(stepFrames(s.playhead, 1, fpsOf(s))) } },
  { id: 'secondBack', label: 'Step back 1 second', category: 'Transport',
    run: (s) => { s.setPlaying(false); s.setPlayhead(s.playhead - 1) } },
  { id: 'secondForward', label: 'Step forward 1 second', category: 'Transport',
    run: (s) => { s.setPlaying(false); s.setPlayhead(s.playhead + 1) } },
  { id: 'goToStart', label: 'Go to start', category: 'Transport', run: (s) => s.goToStart() },
  { id: 'goToEnd', label: 'Go to end', category: 'Transport', run: (s) => s.goToEnd() },

  // ---------- Editing ----------
  { id: 'split', label: 'Split / Blade at playhead', category: 'Editing',
    run: (s) => s.splitAtPlayhead() },
  { id: 'rippleDelete', label: 'Ripple delete selection', category: 'Editing',
    run: async (s) => {
      const ids = selectedIds(s)
      const res = ids.length === 1 ? await s.dispatch('ripple_delete', { clip_id: ids[0] })
        : ids.length > 1 ? await s.dispatch('bulk_delete', { clip_ids: ids }) : null
      // A refused delete (a locked track, QA-023) resolves null and its toast
      // says why; the clip is still there, so it stays selected.
      if (res) s.clearSelection()
    } },
  { id: 'duplicate', label: 'Duplicate selection', category: 'Editing',
    run: async (s) => {
      const ids = selectedIds(s)
      if (ids.length > 1) await s.dispatch('bulk_duplicate', { clip_ids: ids })
      else await s.duplicateSelection()
    } },
  { id: 'copy', label: 'Copy', category: 'Editing', run: (s) => s.copySelection() },
  { id: 'paste', label: 'Paste', category: 'Editing', run: (s) => s.pasteClipboard() },
  { id: 'nudgeLeft', label: 'Nudge clip left 1 frame', category: 'Editing',
    run: (s) => s.nudgeSelection(-frameDuration(fpsOf(s))) },
  { id: 'nudgeRight', label: 'Nudge clip right 1 frame', category: 'Editing',
    run: (s) => s.nudgeSelection(frameDuration(fpsOf(s))) },

  // ---------- Marks ----------
  { id: 'markIn', label: 'Mark in', category: 'Marks', run: (s) => s.setInMark(s.playhead) },
  { id: 'markOut', label: 'Mark out', category: 'Marks', run: (s) => s.setOutMark(s.playhead) },
  { id: 'clearMarks', label: 'Clear in/out marks', category: 'Marks',
    run: (s) => { s.setInMark(null); s.setOutMark(null) } },
  // A marker's `time` is LAYOUT time like every other EDL field (the
  // Timeline draws it at `renderTime`). It used to store the raw playhead —
  // render time — which was consistent with the canvas only by accident and
  // would have put an `at`-taking recipe ("cut at the marker") Σoverlap
  // seconds before the frame the marker was set on.
  { id: 'addMarker', label: 'Add marker', category: 'Marks',
    run: (s) => s.dispatch('add_marker', { time: layoutPlayhead(s.edl, s.playhead) }) },

  // ---------- View ----------
  { id: 'zoomIn', label: 'Zoom in timeline', category: 'View', run: (s) => s.zoomTimeline(1.25) },
  { id: 'zoomOut', label: 'Zoom out timeline', category: 'View', run: (s) => s.zoomTimeline(1 / 1.25) },
  { id: 'zoomFit', label: 'Zoom to fit', category: 'View',
    run: (s) => {
      const dur = s.edl?.duration ?? 0
      if (dur > 0) s.setTimelineZoom(Math.max(10, Math.min(600, (window.innerWidth - 240) / dur)))
    } },
  { id: 'toggleSnap', label: 'Toggle snapping', category: 'View', run: (s) => s.toggleSnap() },

  // ---------- Selection ----------
  { id: 'selectAll', label: 'Select all clips', category: 'Selection', run: (s) => s.selectAll() },
  { id: 'deselect', label: 'Deselect / clear', category: 'Selection',
    run: (s) => { s.clearSelection(); s.setInMark(null); s.setOutMark(null) } },

  // ---------- Navigation ----------
  // Focus the Prompt bar from anywhere. The engine never sees keys typed in a
  // text field, so `/` inside chat or a rename box still types a slash; from
  // the timeline or a button it jumps to the bar with the text selected.
  { id: 'focusPrompt', label: 'Focus the Prompt bar', category: 'Navigation',
    run: () => usePromptStore.getState().focus() },

  // ---------- History ----------
  { id: 'undo', label: 'Undo', category: 'History', run: (s) => s.dispatch('undo') },
  { id: 'redo', label: 'Redo', category: 'History', run: (s) => s.dispatch('redo') },
]

export const COMMAND_BY_ID: Record<string, Command> =
  Object.fromEntries(COMMANDS.map((c) => [c.id, c]))

export const CATEGORIES: Command['category'][] =
  ['Transport', 'Editing', 'Marks', 'Navigation', 'View', 'Selection', 'History']
