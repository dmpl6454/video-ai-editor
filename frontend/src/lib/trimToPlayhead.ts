// Keyboard trim-to-playhead and lift (QA-115).
//
// There was no way to trim a clip to the playhead from the keyboard — every
// NLE has one (CapCut/Premiere Q and W, Final Cut ⌥[ and ⌥]) — and no delete
// that leaves the gap: Premiere's Delete rippled. These planners decide what
// one keypress does and say why when it does nothing; the keymap commands
// dispatch what they return. Pure, so the rules are table tests.
//
// Times: the playhead is RENDER time, a clip's `start`/`end` are LAYOUT time.
// `splitTimeFor` is the decode ⌘B already uses per lane (v1 through its own
// clip-slot inverse, every other lane through `layoutTime`), so a trim lands
// on the frame the split would have cut.
import { clipDuration, clipEnd, clipSpeedFactor, isMediaClip, type AnyClip, type EDL, type Track } from '../types'
import { splitTimeFor } from './splitTargets'
import { renderSpanOf } from './timelineLayout'
import { isTrackLocked, lockedNotice } from './trackLock'
import { frameDuration } from './frameStep'
import { laneName } from './timelineLanes'

export type TrimSide = 'start' | 'end'

export type EditPlan =
  | { kind: 'dispatch'; tool: string; args: Record<string, unknown>; playheadAfter?: number }
  | { kind: 'refuse'; message: string }

const MAIN = 'v1'

interface Located { track: Track; clip: AnyClip; t: number }

/** The clip a trim-to-playhead acts on: the selected clip under the playhead
 *  (the primary selection first), or — with nothing selected — the Main video
 *  clip under it, which is what CapCut's Q/W do. */
function target(edl: EDL, selected: readonly string[], playhead: number): Located | { refuse: string } {
  const under = (c: AnyClip, t: number) => c.start < t && t < clipEnd(c)
  if (selected.length) {
    for (const id of selected) {
      for (const tk of edl.tracks) {
        const c = tk.clips.find((x) => x.id === id)
        if (!c) continue
        const t = splitTimeFor(edl, tk.id, playhead)
        if (under(c, t)) return { track: tk, clip: c, t }
      }
    }
    return { refuse: "The playhead isn't inside the selected clip — move it to where the clip should start or end." }
  }
  const main = edl.tracks.find((tk) => tk.id === MAIN)
  if (main) {
    const t = splitTimeFor(edl, MAIN, playhead)
    const c = main.clips.find((x) => isMediaClip(x) && under(x, t))
    if (c) return { track: main, clip: c, t }
  }
  return { refuse: 'No clip under the playhead — select a clip and put the playhead inside it.' }
}

/**
 * Trim the start (Q, ⌥[) or end (W, ⌥]) of a clip to the playhead.
 *
 * Media: `trim_clip` with the source in/out the playhead maps to (timeline
 * offset × speed). On the magnetic Main video lane a head trim closes up —
 * the kept frame now plays where the clip began, so the playhead follows it
 * there. On any other lane `move_start` keeps the kept frames where they play.
 * Text and stickers: `set_clip_timing`.
 */
export function planTrimToPlayhead(
  edl: EDL | null | undefined, selected: readonly string[], playhead: number, side: TrimSide,
): EditPlan {
  if (!edl) return { kind: 'refuse', message: 'Nothing to trim yet.' }
  const hit = target(edl, selected, playhead)
  if ('refuse' in hit) return { kind: 'refuse', message: hit.refuse }
  const { track, clip, t } = hit
  if (isTrackLocked(track)) return { kind: 'refuse', message: lockedNotice(track) }
  const frame = frameDuration(edl.canvas?.fps)
  const offset = t - clip.start
  if (offset < frame - 1e-9 || clipDuration(clip) - offset < frame - 1e-9) {
    return { kind: 'refuse', message: 'The playhead is on the clip\'s edge — there is nothing to trim.' }
  }
  if (isMediaClip(clip)) {
    const at = clip.in + offset * clipSpeedFactor(clip)
    if (side === 'end') return { kind: 'dispatch', tool: 'trim_clip', args: { clip_id: clip.id, out: at } }
    const args: Record<string, unknown> = { clip_id: clip.id, in: at }
    if (track.id !== MAIN) args.move_start = true
    return track.id === MAIN
      ? { kind: 'dispatch', tool: 'trim_clip', args, playheadAfter: renderSpanOf(edl, MAIN, clip).start }
      : { kind: 'dispatch', tool: 'trim_clip', args }
  }
  return side === 'start'
    ? { kind: 'dispatch', tool: 'set_clip_timing', args: { clip_id: clip.id, start: t } }
    : { kind: 'dispatch', tool: 'set_clip_timing', args: { clip_id: clip.id, end: t } }
}

/**
 * Lift: delete the selection and LEAVE THE GAP — nothing else moves.
 *
 * Every lane but Main video already keeps its clips' times when one is
 * deleted (QA-013; `ripple_delete`/`bulk_delete` only close gaps on v1), so a
 * lift there is those ops. Main video is the one MAGNETIC lane — every edit
 * re-packs it, so a hole there would close on the next trim and the overlays
 * would drift off the picture. A lift that includes a Main video clip is
 * therefore refused with the chord that deletes-and-closes instead.
 */
export function planLift(edl: EDL | null | undefined, selected: readonly string[], rippleChord: string): EditPlan {
  if (!edl || !selected.length) return { kind: 'refuse', message: 'Select a clip to delete.' }
  const ids = Array.from(new Set(selected))
  for (const id of ids) {
    const tk = edl.tracks.find((x) => x.clips.some((c) => c.id === id))
    if (!tk) continue
    if (isTrackLocked(tk)) return { kind: 'refuse', message: lockedNotice(tk) }
    if (tk.id === MAIN) {
      return {
        kind: 'refuse',
        message: `${laneName(tk)} is magnetic — its clips always close up, so it can't keep a gap. `
          + `Press ${rippleChord || 'Ripple delete'} to delete and close the gap there.`,
      }
    }
  }
  const live = ids.filter((id) => edl.tracks.some((tk) => tk.clips.some((c) => c.id === id)))
  if (!live.length) return { kind: 'refuse', message: 'Select a clip to delete.' }
  return live.length === 1
    ? { kind: 'dispatch', tool: 'ripple_delete', args: { clip_id: live[0] } }
    : { kind: 'dispatch', tool: 'bulk_delete', args: { clip_ids: live } }
}
