// Freeze frame at the playhead (CapCut's Freeze), wave D lane S2: the ONE
// rule the timeline toolbar button, the clip context menu and the keymap
// command share.
//
// The freeze is always on the Main video lane: `freeze_frame` splits the v1
// clip under the playhead and inserts a still of that exact frame (the
// server picks the frame from the program map). The playhead is RENDER time
// and the op takes LAYOUT time, decoded through v1's own inverse — the same
// path as ⌘B (lib/splitTargets). A selected v1 clip must be the one under
// the playhead (CapCut greys Freeze out otherwise); a selection on another
// lane is ignored, the clip under the playhead is frozen.
//
// The hold length is NOT decided here: omitted, the server applies its
// default (`speed_presets.FREEZE_DEFAULT_SECONDS`, 3 s), one source.

import { clipEnd, isMediaClip, type EDL } from '../types'
import { splitTimeFor } from './splitTargets'
import { isTrackLocked } from './trackLock'

export type FreezePlan =
  | { kind: 'freeze'; args: { time: number; clip_id?: string } }
  | { kind: 'refuse'; message: string }

const TOL = 1e-6

/** What a Freeze press at `playhead` (render seconds) does. */
export function planFreeze(edl: EDL | null | undefined, selection: string | null, playhead: number,
                           clipId?: string): FreezePlan {
  const v1 = edl?.tracks.find((t) => t.id === 'v1')
  const media = (v1?.clips ?? []).filter(isMediaClip)
  if (!edl || !media.length) return { kind: 'refuse', message: 'Add a video to the Main video track to freeze a frame.' }
  if (isTrackLocked(v1)) return { kind: 'refuse', message: 'The Main video track is locked — unlock it to freeze a frame.' }
  const time = splitTimeFor(edl, 'v1', playhead)
  const under = media.find((c) => c.start - TOL <= time && time < clipEnd(c) - TOL)
  const wanted = clipId ?? (selection && media.some((c) => c.id === selection) ? selection : undefined)
  if (wanted) {
    const c = media.find((m) => m.id === wanted)
    if (!c) return { kind: 'refuse', message: 'Only clips on the Main video track can be frozen.' }
    if (c !== under) return { kind: 'refuse', message: 'Move the playhead over the clip to freeze the frame you want.' }
    return { kind: 'freeze', args: { time, clip_id: c.id } }
  }
  if (!under) return { kind: 'refuse', message: 'Move the playhead over a clip on the Main video track to freeze its frame.' }
  return { kind: 'freeze', args: { time } }
}

/** Whether Freeze can run now (the toolbar button's enabled state). */
export const canFreeze = (edl: EDL | null | undefined, selection: string | null, playhead: number): boolean =>
  planFreeze(edl, selection, playhead).kind === 'freeze'

/** What `freezeAtPlayhead` needs from the store (kept narrow so this module
 *  does not import the store). */
export interface FreezeHost {
  edl: EDL | null
  selection: string | null
  playhead: number
  dispatch: (tool: string, args: Record<string, unknown>) => Promise<unknown>
  setSelection: (id: string | null) => void
}

/** The Freeze gesture: plan it, say why in `notify` when it cannot run,
 *  else one `freeze_frame` (one undo step) and select the new still so the
 *  Inspector shows its hold. */
export async function freezeAtPlayhead(s: FreezeHost, notify: (message: string) => void,
                                       clipId?: string): Promise<unknown> {
  const plan = planFreeze(s.edl, s.selection, s.playhead, clipId)
  if (plan.kind === 'refuse') { notify(plan.message); return null }
  const res = await s.dispatch('freeze_frame', plan.args)
  // store.dispatch answers `{result, edl_hash, op}`; the handler's own
  // answer is `result`.
  const still = (res as { result?: { clip_id?: unknown } } | null)?.result?.clip_id
  if (typeof still === 'string') s.setSelection(still)
  return res
}
