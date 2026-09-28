// The args a direct-manipulation gesture in the preview (StickerLayer: move,
// corner-resize, rotate) commits through `set_clip_transform`.
//
// Every commit carries the playhead as clip-local `time`, the same value the
// Inspector's Transform fields send (Properties.tsx, `time: localT`). On a
// property with no keyframes the backend just sets the scalar, exactly as
// before; on an animated one it writes (or replaces) a key AT the playhead —
// which is what dragging an animated overlay means in CapCut.
//
// Without it the backend's no-`time` rule applies ("a bare value flattens the
// animation to a constant"), so dragging a keyframed picture-in-picture about
// the frame silently deleted every key it had: x {keyframes:[[4,400],[10,1500]]}
// became the plain 475 in one drag, with the diamonds gone from the timeline.
//
// `time` is measured the way Properties measures it — `clipLocalTime` against
// the clip's RENDER span — so a key dragged in the preview lands on the same
// instant the Inspector's Keyframe button would write.
import { clipLocalTime } from './timelineLayout'
import type { AnyClip, EDL } from '../types'

/** Clip-local keyframe time of `clipId` at the render instant `playhead`,
 *  or undefined when the clip is not in the EDL. */
export function keyTimeAt(edl: EDL | null | undefined, clipId: string, playhead: number): number | undefined {
  if (!edl) return undefined
  for (const tk of edl.tracks) {
    const c = (tk.clips as AnyClip[]).find((k) => k.id === clipId)
    if (c) return clipLocalTime(edl, tk.id, c, playhead)
  }
  return undefined
}

/** `set_clip_transform` args for a gesture commit: the given fields plus the
 *  playhead's clip-local `time`, so an animated property gains a key instead
 *  of losing all of them. */
export function transformCommitArgs(
  edl: EDL | null | undefined, clipId: string,
  fields: Record<string, unknown>, playhead: number,
): Record<string, unknown> {
  const time = keyTimeAt(edl, clipId, playhead)
  return { clip_id: clipId, ...fields, ...(time === undefined ? {} : { time }) }
}
