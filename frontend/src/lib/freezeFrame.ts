// Freeze frame at the playhead (CapCut's Freeze), wave D lane S2: the ONE
// rule the timeline toolbar button, the clip context menu and the keymap
// command share.
//
// The freeze is always on the Main video lane: `freeze_frame` splits the v1
// clip under the playhead and inserts a still of that exact frame (the
// server picks the frame from the program map). The playhead is RENDER time
// and the op takes LAYOUT time, decoded through v1's own inverse — the same
// path as ⌘B (lib/splitTargets). A selected v1 clip must be the one under
// the playhead (CapCut greys Freeze out otherwise); a selection on a
// non-video lane is ignored, the clip under the playhead is frozen.
//
// An OVERLAY (picture-in-picture, v2+) clip freezes too (wave D3, E2): when
// the selected (or right-clicked) clip is on an overlay lane, THAT clip is
// frozen at the playhead in its lane's layout time, and only that lane opens
// up (`freeze_frame` with `clip_id` + `track`; render/pip.py holds the frame
// exactly as v1 does).
//
// The hold length is NOT decided here: omitted, the server applies its
// default (`speed_presets.FREEZE_DEFAULT_SECONDS`, 3 s), one source.

import { clipEnd, isMediaClip, type EDL } from '../types'
import { splitTimeFor } from './splitTargets'
import { isTrackLocked } from './trackLock'

export type FreezePlan =
  | { kind: 'freeze'; args: { time: number; clip_id?: string; track?: string } }
  | { kind: 'refuse'; message: string }

const TOL = 1e-6

/** What a Freeze press at `playhead` (render seconds) does. */
export function planFreeze(edl: EDL | null | undefined, selection: string | null, playhead: number,
                           clipId?: string): FreezePlan {
  const overlay = planOverlayFreeze(edl, clipId ?? selection, playhead)
  if (overlay) return overlay
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

/** The plan when `id` names a media clip on an OVERLAY video lane, else null
 *  (the Main video rule applies). */
function planOverlayFreeze(edl: EDL | null | undefined, id: string | null | undefined,
                           playhead: number): FreezePlan | null {
  if (!edl || !id) return null
  const lane = edl.tracks.find((t) => t.type === 'video' && t.id !== 'v1'
    && t.clips.some((c) => c.id === id && isMediaClip(c)))
  if (!lane) return null
  const c = lane.clips.find((x) => x.id === id)!
  if (isTrackLocked(lane)) return { kind: 'refuse', message: `${lane.label || lane.id} is locked — unlock it to freeze a frame.` }
  const time = splitTimeFor(edl, lane.id, playhead)
  if (!(c.start - TOL <= time && time < clipEnd(c) - TOL)) {
    return { kind: 'refuse', message: 'Move the playhead over the clip to freeze the frame you want.' }
  }
  return { kind: 'freeze', args: { time, clip_id: c.id, track: lane.id } }
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
