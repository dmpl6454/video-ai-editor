import { useStore } from '../store'
import { usePromptStore } from '../lib/promptStore'
import { layoutPlayhead } from '../lib/timelineLayout'
import { frameDuration, stepFrames, toFrameGrid } from '../lib/frameStep'
import { fitZoom, timelineView } from '../lib/timelineZoom'
import { planLift, planTrimToPlayhead, type EditPlan, type TrimSide } from '../lib/trimToPlayhead'
import { toast } from '../toast'
import { chordLabel, useKeymapStore } from './engine'
import { openSettings } from '../lib/settingsOpen'

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

/** Run a planned edit (lib/trimToPlayhead): a refusal says why in a toast
 *  instead of the key silently doing nothing (QA-115). */
async function runPlan(s: Store, plan: EditPlan): Promise<unknown> {
  if (plan.kind === 'refuse') { toast.info(plan.message); return null }
  const res = await s.dispatch(plan.tool, plan.args)
  if (res && plan.playheadAfter !== undefined) s.setPlayhead(plan.playheadAfter)
  return res
}

const trimToPlayhead = (s: Store, side: TrimSide) =>
  runPlan(s, planTrimToPlayhead(s.edl, selectedIds(s), s.playhead, side))

/** The key(s) that ripple-delete in the ACTIVE keymap, for the lift refusal. */
function rippleChord(): string {
  const chords = useKeymapStore.getState().effectiveMap().rippleDelete ?? []
  return chords.slice(0, 1).map(chordLabel).join('')
}

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
  // J/L start at 1× from a stop and double only while ALREADY shuttling that
  // way (QA-058). K resets the rate to 1, so the old `(r || 1) * 2` made the
  // very first L play at 2×.
  { id: 'shuttleReverse', label: 'Shuttle reverse', category: 'Transport',
    run: (s) => { const r = s.isPlaying ? s.playbackRate : 0; s.setPlaybackRate(r < 0 ? Math.max(-8, r * 2) : -1); s.setPlaying(true) } },
  { id: 'shuttleStop', label: 'Shuttle stop', category: 'Transport',
    run: (s) => { s.setPlaying(false); s.setPlaybackRate(1) } },
  { id: 'shuttleForward', label: 'Shuttle forward', category: 'Transport',
    run: (s) => { const r = s.isPlaying ? s.playbackRate : 0; s.setPlaybackRate(r > 0 ? Math.min(8, r * 2) : 1); s.setPlaying(true) } },
  { id: 'frameBack', label: 'Step back 1 frame', category: 'Transport',
    run: (s) => { s.setPlaying(false); s.setPlayhead(stepFrames(s.playhead, -1, fpsOf(s))) } },
  { id: 'frameForward', label: 'Step forward 1 frame', category: 'Transport',
    run: (s) => { s.setPlaying(false); s.setPlayhead(stepFrames(s.playhead, 1, fpsOf(s))) } },
  { id: 'secondBack', label: 'Step back 1 second', category: 'Transport',
    run: (s) => { s.setPlaying(false); s.setPlayhead(toFrameGrid(s.playhead - 1, fpsOf(s))) } },
  { id: 'secondForward', label: 'Step forward 1 second', category: 'Transport',
    run: (s) => { s.setPlaying(false); s.setPlayhead(toFrameGrid(s.playhead + 1, fpsOf(s))) } },
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
  // Delete WITHOUT closing the gap (Premiere's Delete, Final Cut's ⇧⌫).
  // Refused, with the ripple key, when the selection includes a Main video
  // clip — that lane is magnetic (see lib/trimToPlayhead.planLift).
  { id: 'lift', label: 'Delete, leave gap (lift)', category: 'Editing',
    run: async (s) => {
      const res = await runPlan(s, planLift(s.edl, selectedIds(s), rippleChord()))
      if (res) s.clearSelection()
    } },
  // Trim to the playhead (QA-115): CapCut/Premiere Q and W, Final Cut ⌥[ ⌥].
  { id: 'trimStartToPlayhead', label: 'Trim clip start to playhead', category: 'Editing',
    run: (s) => trimToPlayhead(s, 'start') },
  { id: 'trimEndToPlayhead', label: 'Trim clip end to playhead', category: 'Editing',
    run: (s) => trimToPlayhead(s, 'end') },
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
    // Fits the width the timeline really has (QA-054): the mounted timeline
    // registers its lane width; `window.innerWidth − 240` ignored both side
    // panels and the label column, so a 40 s "fit" overflowed by a third.
    run: (s) => {
      const dur = s.edl?.duration ?? 0
      if (!(dur > 0)) return
      const view = timelineView()
      if (view) view.fitTo(fitZoom(dur, view.laneWidth()))
      else s.setTimelineZoom(fitZoom(dur, window.innerWidth * 0.6))
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
  // ⌘, / Ctrl+, — the platform's Settings shortcut (QA-063-SETTINGS).
  { id: 'openSettings', label: 'Open Settings', category: 'Navigation',
    run: () => openSettings() },

  // ---------- History ----------
  { id: 'undo', label: 'Undo', category: 'History', run: (s) => s.dispatch('undo') },
  { id: 'redo', label: 'Redo', category: 'History', run: (s) => s.dispatch('redo') },
]

export const COMMAND_BY_ID: Record<string, Command> =
  Object.fromEntries(COMMANDS.map((c) => [c.id, c]))

export const CATEGORIES: Command['category'][] =
  ['Transport', 'Editing', 'Marks', 'Navigation', 'View', 'Selection', 'History']
