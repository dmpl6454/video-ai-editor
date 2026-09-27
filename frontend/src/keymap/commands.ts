import { useStore } from '../store'
import { usePromptStore } from '../lib/promptStore'
import { layoutPlayhead } from '../lib/timelineLayout'
import { frameDuration, stepFrames, toFrameGrid } from '../lib/frameStep'
import { fitZoom, timelineView } from '../lib/timelineZoom'
import { planLift, planTrimToPlayhead, type EditPlan, type TrimSide } from '../lib/trimToPlayhead'
import { toast } from '../toast'
import { freezeAtPlayhead } from '../lib/freezeFrame'
import { chordLabel, useKeymapStore } from './engine'
import { openSettings } from '../lib/settingsOpen'
import { useLayoutStore } from '../lib/layoutStore'
import { RAIL_ITEMS, railPanelId } from '../components/rail/railModel'
import { cycleRegion } from './regions'
import { pressControl, type UiTarget } from './uiTargets'
import { adjacentClip, clipAtPlayhead, type ClipPick } from '../lib/clipSelect'
import { announce } from '../lib/announce'

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

/** Where a command's chord runs (LEFT_RAIL_SPEC §4.2; the rule is
 *  engine.ts `shouldRun`):
 *  - 'default': not in a text field, not inside `[data-keymap-ignore]`;
 *  - 'global': inside `[data-keymap-ignore]` too (a panel chord from an AI
 *    form or a media row); in a text field only on a ⌘ chord that types
 *    nothing and is not a native text chord (⌘E, ⌥⌘K);
 *  - 'anywhere': in text fields as well (F6 region cycling only).
 *  No command runs while a modal dialog is open. */
export type CommandScope = 'default' | 'global' | 'anywhere'

export interface Command {
  id: string
  label: string
  category: 'Transport' | 'Editing' | 'Marks' | 'Navigation' | 'View' | 'Selection' | 'History' | 'Panels'
  /** Omitted = 'default'. */
  scope?: CommandScope
  /** A region (CSS selector) whose text fields also run this command, e.g.
   *  ⌥9 / ⌥0 from the Chat box inside `#right-panel`. */
  alsoInText?: string
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

/** Press the control a command stands for (keymap/uiTargets). A disabled one
 *  says why in a toast — its tooltip, e.g. "Nothing to export yet" — rather
 *  than the key silently doing nothing. */
function press(target: UiTarget): void {
  const r = pressControl(target)
  if (r.kind === 'disabled' && r.reason) toast.info(r.reason)
  else if (r.kind === 'missing') console.warn(`[keymap] no control on screen for ${target}`)
}

/** Select a clip picked from the keyboard (review RD3), optionally moving
 *  the playhead to its first frame, and say which one it is. */
function pickClip(s: Store, pick: ClipPick | null, movePlayhead: boolean): void {
  if (!pick) { announce('No clip there'); return }
  s.setSelection(pick.id)
  if (movePlayhead) { s.setPlaying(false); s.setPlayhead(pick.start) }
  announce(`Selected ${pick.name}`)
}

const laneOf = (s: Store, id: string | null): string | null =>
  (id && s.edl?.tracks.find((t) => t.clips.some((c) => c.id === id))?.id) || null

/** After ⌥T (review RD3): the new text clip's Inspector field takes focus
 *  with its words selected, so what the user types next IS the text (it was
 *  left on the timeline, where L shuttled and K stopped). Waits for the
 *  insert to land and select the clip; Escape in the field returns to the
 *  timeline (Properties.tsx). */
async function focusNewText(before: string | null): Promise<void> {
  if (typeof requestAnimationFrame !== 'function' || typeof document === 'undefined') return
  const t0 = performance.now()
  while (performance.now() - t0 < 3000) {
    await new Promise((r) => requestAnimationFrame(() => r(null)))
    const s = useStore.getState()
    const id = s.selection
    if (!id || id === before) continue
    if (!s.edl?.tracks.some((t) => t.clips.some((c) => c.id === id && 'text' in c))) return
    if (useLayoutStore.getState().rightTab !== 'inspect' || !useLayoutStore.getState().rightOpen) {
      useLayoutStore.getState().showRight('inspect')
      continue
    }
    const field = document.querySelector<HTMLTextAreaElement>('.props textarea[aria-label="Text"]')
    if (!field) continue
    field.focus()
    field.select()
    return
  }
}

/** Focus a rail panel once the switch has committed (unless focus is
 *  already inside it, or the user typed into a field meanwhile). */
function focusPanel(id: (typeof RAIL_ITEMS)[number]['id']): void {
  if (typeof requestAnimationFrame !== 'function' || typeof document === 'undefined') return
  requestAnimationFrame(() => {
    const panel = document.getElementById(railPanelId(id))
    if (!panel || panel.hidden || panel.contains(document.activeElement)) return
    panel.focus({ preventScroll: true })
  })
}

/** Open the right panel on a tab; Chat also takes focus in its message box
 *  after the commit that shows it (as RightPanel's "Show the Chat" does). */
function showRightPanel(tab: 'inspect' | 'chat'): void {
  // ⌥9 from the Chat box: the box is about to hide, so focus goes to the
  // Inspector tab rather than into a hidden field
  const fromChat = !!document.activeElement?.closest?.('#right-panel-chat')
  useLayoutStore.getState().showRight(tab)
  if (tab !== 'chat') {
    if (fromChat) requestAnimationFrame(() => document.getElementById('right-tab-inspect')?.focus())
    return
  }
  requestAnimationFrame(() => {
    document.querySelector<HTMLElement>('#right-panel-chat textarea, #right-panel-chat input')?.focus()
  })
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
  { id: 'freezeFrame', label: 'Freeze frame at playhead', category: 'Editing', run: (s) => freezeAtPlayhead(s, toast.info) },
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
  // ⌥T: the Text tool's own "add text" button (the default style at the
  // playhead, selected). 'global' like the panel chords.
  { id: 'addText', label: 'Add text at the playhead', category: 'Editing', scope: 'global',
    run: (s) => { const before = s.selection; press('addText'); void focusNewText(before) } },

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
  // A particular clip from the keyboard (review RD3): Final Cut's C,
  // Premiere's D; ↑ / ↓ walk the lane (main track when nothing is selected)
  // and bring the playhead to the clip's first frame.
  { id: 'selectClipAtPlayhead', label: 'Select the clip at the playhead', category: 'Selection',
    run: (s) => pickClip(s, clipAtPlayhead(s.edl, s.playhead, laneOf(s, s.selection)), false) },
  { id: 'selectNextClip', label: 'Select the next clip', category: 'Selection',
    run: (s) => pickClip(s, adjacentClip(s.edl, s.selection, s.playhead, 1), true) },
  { id: 'selectPrevClip', label: 'Select the previous clip', category: 'Selection',
    run: (s) => pickClip(s, adjacentClip(s.edl, s.selection, s.playhead, -1), true) },
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
  // ⌥⌘K (Premiere's Keyboard Shortcuts chord) and ⌘E (CapCut / Final Cut
  // Export): the "Customize keyboard shortcuts" button and the Export button.
  { id: 'openShortcuts', label: 'Customize keyboard shortcuts', category: 'Navigation', scope: 'global',
    run: () => press('shortcutsDialog') },
  { id: 'exportVideo', label: 'Export the video', category: 'Navigation', scope: 'global',
    run: () => press('exportDialog') },

  // ---------- History ----------
  { id: 'undo', label: 'Undo', category: 'History', run: (s) => s.dispatch('undo') },
  { id: 'redo', label: 'Redo', category: 'History', run: (s) => s.dispatch('redo') },

  // ---------- Panels (LEFT_RAIL_SPEC §4.1) ----------
  // One command per rail item, from the rail's own list (railModel), so a
  // panel the rail does not show has no command and Help lists no dead key.
  // The chord shows its panel (opening a collapsed one); the same chord again
  // collapses it, like a click on the active tab. Focus stays where it is
  // unless it was inside the part that hid (focus rescue, §5.3). 'global': a
  // chord still switches from a media row or an AI form (critique H1).
  // Showing a panel puts focus ON it (review RD3: ⌥7 left focus on the
  // timeline, 17 Tab stops from "Generate captions"): Tab goes on into its
  // controls, a screen reader names it, and Space still plays (the panel is
  // not a control). Hiding it leaves focus where the rescue puts it.
  ...RAIL_ITEMS.map((r): Command => ({
    id: r.command, label: `Show or hide the ${r.label} panel`, category: 'Panels', scope: 'global',
    run: () => {
      useLayoutStore.getState().showTab(r.id, { toggle: true })
      const after = useLayoutStore.getState()
      if (after.leftOpen && after.leftTab === r.id) focusPanel(r.id)
    },
  })),
  { id: 'toggleToolPanel', label: 'Show or hide the tool panel', category: 'Panels', scope: 'global',
    run: () => useLayoutStore.getState().toggleLeftOpen() },
  // alsoInText: ⌥0 puts focus in the Chat box, and ⌥9 from there must bring
  // the Inspector back (review RD2).
  { id: 'showInspector', label: 'Show the Inspector', category: 'Panels', scope: 'global', alsoInText: '#right-panel',
    run: () => showRightPanel('inspect') },
  { id: 'showChat', label: 'Show the Chat', category: 'Panels', scope: 'global', alsoInText: '#right-panel',
    run: () => showRightPanel('chat') },
  // F6 / ⇧F6: 'anywhere', so they also leave the Prompt bar or a chat box.
  { id: 'cycleRegion', label: 'Focus the next region', category: 'Panels', scope: 'anywhere',
    run: () => { cycleRegion(1) } },
  { id: 'cycleRegionBack', label: 'Focus the previous region', category: 'Panels', scope: 'anywhere',
    run: () => { cycleRegion(-1) } },
]

export const COMMAND_BY_ID: Record<string, Command> =
  Object.fromEntries(COMMANDS.map((c) => [c.id, c]))

export const CATEGORIES: Command['category'][] =
  ['Transport', 'Editing', 'Marks', 'Navigation', 'Panels', 'View', 'Selection', 'History']
