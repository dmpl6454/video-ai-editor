// Keyboard nudge (Alt+←/→) of the selected clip — QA-022.
//
// `nudgeSelection` used to send `move_clip {new_start: start ± 1/30}` blind.
// On the magnetic main lane the clips butt against each other, so a one-frame
// move RIGHT overlapped the next clip and the backend's free-gap snap pushed it
// past the LAST clip: blue jumped from 10.005 to 40.02 s, opening a 10 s black
// hole and growing the timeline, with no toast. A LEFT nudge snapped straight
// back — a silent no-op. The backend now refuses an overlapping same-lane v1
// move with the reason; this planner answers BEFORE the round-trip, for every
// media lane (a v2/music nudge into a neighbour would take the same forward
// snap), and steps by one frame of the PROJECT rate rather than a fixed 1/30.
import { clipDuration, isMediaClip, type AnyClip, type EDL, type Track } from '../types'
import { frameDuration } from './frameStep'

export type NudgePlan =
  | { kind: 'move'; clipId: string; newStart: number }
  | { kind: 'refuse'; message: string }
  | { kind: 'none' }

function isLocked(t: Track): boolean {
  return !!(t as unknown as { locked?: boolean }).locked
}

/** One frame of the project timebase, in seconds (lib/frameStep's rule). */
export function projectFrame(edl: EDL): number {
  return frameDuration(edl.canvas?.fps)
}

export function planNudge(edl: EDL | null | undefined, clipId: string | null | undefined,
                          deltaSeconds: number): NudgePlan {
  if (!edl || !clipId || !deltaSeconds) return { kind: 'none' }
  let track: Track | undefined
  let clip: AnyClip | undefined
  for (const t of edl.tracks) {
    const c = t.clips.find((x) => x.id === clipId)
    if (c) { track = t; clip = c; break }
  }
  if (!track || !clip || !('start' in clip)) return { kind: 'none' }
  if (isLocked(track)) {
    return { kind: 'refuse', message: `Track "${track.label ?? track.id}" is locked — unlock it to move its clips.` }
  }
  const frame = projectFrame(edl)
  // A caller asking for more than ~one frame gets that distance; the keyboard's
  // one-frame nudge gets exactly one frame of THIS project's rate.
  const step = Math.abs(deltaSeconds) > frame * 1.5 ? Math.abs(deltaSeconds) : frame
  const start = clip.start as number
  const newStart = Math.max(0, start + Math.sign(deltaSeconds) * step)
  if (Math.abs(newStart - start) < 1e-9) return { kind: 'none' }
  if (isMediaClip(clip)) {
    const end = newStart + clipDuration(clip)
    const blocker = track.clips.find((o) => o.id !== clip!.id && isMediaClip(o)
      && newStart < o.start + clipDuration(o) - 1e-9 && end > o.start + 1e-9)
    if (blocker) {
      const main = track.id === 'v1'
      return {
        kind: 'refuse',
        message: main
          ? 'No room to nudge — main-track clips sit end to end. Drag to reorder, or trim a neighbour first.'
          : `No room to nudge — it would overlap the next clip on "${track.label ?? track.id}".`,
      }
    }
  }
  return { kind: 'move', clipId, newStart }
}
