import type { EDL } from '../types'
import { isMediaClip, clipEnd } from '../types'

/**
 * Where v1's PICTURE ends — the last frame there is anything to overlay.
 *
 * NOT `edl.duration`, which is a max over EVERY track and therefore includes the
 * overlays themselves. Clamping a new overlay to that is circular: one overlay
 * that already overshoots the video silently licenses the next one to overshoot
 * just as far.
 *
 * Mirrors the server's own "where does v1's content end" notion (see
 * EDL.recompute_duration's v1 branch): v1 is ASSEMBLED, so the cursor walks the
 * clips with `max(cursor, start) + duration` rather than taking a geometric max —
 * two clips that overlap in the EDL still play one after the other.
 */
export function videoContentEnd(edl: EDL | null): number {
  if (!edl) return 0
  const v1 = edl.tracks.find((t) => t.id === 'v1')
  if (!v1) return 0
  let cursor = 0
  for (const c of v1.clips) {
    if (!isMediaClip(c)) continue
    cursor = Math.max(cursor, c.start) + Math.max(0, clipEnd(c) - c.start)
  }
  return cursor
}

/** Shortest overlay worth creating. Below this it is a few frames and unreadable
 *  — the same floor the sticker tool already used when pulling `start` back. */
export const MIN_OVERLAY_S = 0.5

/**
 * End time for a NEW overlay whose duration is a DEFAULT rather than a request.
 *
 * Reported as: "the total duration was 4 secs but the video ran to 4.4 secs".
 * The real session had v1 ending at 4.000 and a text clip at 1.375–4.375 — a 3s
 * default dropped at the playhead, overshooting the video by 0.375s. Because
 * `edl.duration` is a max over every track, that overlay held the whole timeline
 * open and playback ran on into black past the end of the picture.
 *
 * Only the DEFAULT is clamped. An explicit end is a decision (an outro card over
 * black is a real thing to want), and this is not called for those.
 *
 * Two cases deliberately left alone:
 *   * no video at all (`videoEnd <= 0`) — nothing to clamp against, and an
 *     overlay-only timeline is legitimate;
 *   * a playhead already at or past the end of the picture — the user parked it
 *     there, so honour the full default instead of collapsing it to nothing.
 *
 * The past-the-end test is `start >= videoEnd`, NOT `start >= videoEnd - MIN`.
 * The looser form was wrong and measured wrong: with a 1.0s picture and the
 * playhead at 0.575 — comfortably INSIDE the video — it read as "past the end"
 * and handed back the full 3s, so the overlay still ran to 3.575 and still held
 * the timeline open. Being within half a second of the end is not the same as
 * being past it; the short-overlay floor below already handles the squeeze.
 */
export function defaultOverlayEnd(start: number, want: number, videoEnd: number): number {
  const plain = start + want
  if (videoEnd <= 0) return plain
  if (start >= videoEnd) return plain
  // The floor can overshoot the picture by design, by at most MIN_OVERLAY_S: an
  // overlay of a few frames is invisible in practice (round-5 finding M-02), so
  // a bounded tail beats one the user cannot see or grab.
  return Math.max(start + MIN_OVERLAY_S, Math.min(plain, videoEnd))
}
