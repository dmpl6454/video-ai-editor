/**
 * Timeline arithmetic over an EDL.
 *
 * `lib/types.ts` owns the SHAPES; this file owns the SUMS. Nothing here
 * re-declares a type — a second `Clip` interface that drifted from the wire
 * format by one optional field would be invisible until a render came out
 * wrong.
 *
 * THE ONE RULE THAT IS EASY TO GET WRONG. A clip's timeline length is not
 * `out - in`. It is `(out - in) / speed`, because the backend's
 * `Clip.effective_duration` says so and every ripple the Mac performs is
 * computed from that. Drawing a 2× clip at its source length puts it on top of
 * the neighbours the Mac has already pulled left — the timeline then disagrees
 * with the render, and the user is the one who finds out.
 *
 * `speed` is also not always a number: a speed-ramped clip carries a curve
 * dict. `clipSpeedFactor` is the single place that decides what a non-number
 * means (1×, the same answer `frontend/src/types.ts::clipSpeedFactor` gives),
 * so no call site has to remember.
 */

import { isMediaClip, type AnyClip, type Clip, type EDL, type Track } from "./types";

/** The video track the Mac appends uploads to, and the only one `video_extent`
 *  measures (`edl/schema.py::video_extent`). */
export const VIDEO_TRACK_ID = "v1";

/**
 * Scalar playback rate. Mirrors the backend's `Clip.speed_factor`: anything
 * that is not a positive finite number — absent, null, or a speed-curve dict —
 * is 1×.
 */
export function clipSpeedFactor(c: AnyClip): number {
  const raw = (c as { speed?: unknown }).speed;
  return typeof raw === "number" && Number.isFinite(raw) && raw > 0 ? raw : 1;
}

/** TIMELINE seconds a clip occupies. See the file header. */
export function clipDuration(c: AnyClip): number {
  if (isMediaClip(c)) return (c.out - c.in) / clipSpeedFactor(c);
  return c.end - c.start;
}

/** The timeline second at which a clip stops. */
export function clipEnd(c: AnyClip): number {
  if (isMediaClip(c)) return c.start + clipDuration(c);
  return c.end;
}

export function trackById(edl: EDL | null, trackId: string): Track | null {
  if (!edl) return null;
  return edl.tracks.find((t) => t.id === trackId) ?? null;
}

/** Only the media clips on a track, in timeline order. Text and sticker
 *  overlays live on the same `clips` array and have no `src` to draw. */
export function mediaClipsOf(track: Track | null): Clip[] {
  if (!track) return [];
  return track.clips.filter(isMediaClip).slice().sort((a, b) => a.start - b.start);
}

/**
 * Timeline seconds occupied by the V1 video track — the port of
 * `edl/schema.py::video_extent`.
 *
 * DISTINCT FROM `edl.duration`, which is a max over EVERY track: a six-minute
 * music bed makes `duration` 373 s while the video is 29 s. A player scrubbed
 * against `duration` on such a project spends most of its travel on black, so
 * the transport and the filmstrip both measure themselves against this.
 */
export function videoExtent(edl: EDL | null): number {
  const clips = mediaClipsOf(trackById(edl, VIDEO_TRACK_ID));
  return clips.reduce((max, c) => Math.max(max, clipEnd(c)), 0);
}

/** Where the transport's travel ends: the video, or — on a project that is
 *  audio-only so far — whatever the EDL says its whole duration is. */
export function timelineExtent(edl: EDL | null): number {
  const video = videoExtent(edl);
  if (video > 0) return video;
  return edl && Number.isFinite(edl.duration) ? Math.max(0, edl.duration) : 0;
}

export interface ClipLocation {
  clip: AnyClip;
  track: Track;
}

/** Find a clip and the track it sits on. Ids are unique across the EDL, but a
 *  caller almost always needs the track too (to name the lane, or to split). */
export function findClip(edl: EDL | null, clipId: string): ClipLocation | null {
  if (!edl) return null;
  for (const track of edl.tracks) {
    const clip = track.clips.find((c) => c.id === clipId);
    if (clip) return { clip, track };
  }
  return null;
}

/** Every media clip on the timeline, whatever lane it is on. */
export function allMediaClips(edl: EDL | null): Clip[] {
  if (!edl) return [];
  return edl.tracks.flatMap((t) => t.clips.filter(isMediaClip));
}

export function isEmptyTimeline(edl: EDL | null): boolean {
  if (!edl) return true;
  return edl.tracks.every((t) => t.clips.length === 0);
}

export interface PosterSpec {
  /** Absolute path inside the session dir, as `/thumb` requires. */
  src: string;
  /** SOURCE seconds — `/thumb`'s `t` samples the FILE, not the timeline. */
  t: number;
}

/**
 * One frame that stands for a whole project, for the project list.
 *
 * `storage.py::list_sessions` sends no thumbnail — it reads `meta.json` and the
 * directory's mtime and nothing else — so a poster costs one EDL fetch per row
 * and the list is designed to load them lazily rather than to block on them.
 *
 * A little way in rather than at the first frame: a cut very often opens on
 * black or on a slate, and a poster wall of black rectangles tells the user
 * nothing about which project is which.
 */
/**
 * A poster spec whose `src` is guaranteed to sit inside the session directory,
 * or null when the project has none.
 *
 * `GET /thumb` answers 403 for any absolute path outside the session workdir
 * (main.py:1186), and `add_clip`/`find_broll` legitimately put external paths
 * like `~/Movies/interview.mov` on the timeline — a permanent per-clip fact
 * that `TimelineStrip` handles per tile. A PROBE cannot use such a clip: it
 * would report "media is refused" for the whole session while `preview.mp4`,
 * which lives inside the session dir, played perfectly above the warning.
 *
 * The session directory is `<workdir>/<sid>/`, and every path the Mac writes
 * for an upload is under `<sid>/uploads/`. Matching on that segment is the only
 * check available from here — the phone does not know the Mac's workdir — and
 * it is a conservative one: a false negative just means no probe runs, which is
 * strictly better than a false alarm.
 */
export function inSessionPosterSpec(edl: EDL | null, sessionId: string): PosterSpec | null {
  const marker = `/${sessionId}/`;
  const clip = allMediaClips(edl).find((c) => c.src.includes(marker));
  if (!clip) return null;
  const sourceSpan = Math.max(0, clip.out - clip.in);
  return { src: clip.src, t: clip.in + Math.min(0.6, sourceSpan / 2) };
}

export function posterSpec(edl: EDL | null): PosterSpec | null {
  const first = mediaClipsOf(trackById(edl, VIDEO_TRACK_ID))[0] ?? allMediaClips(edl)[0];
  if (!first) return null;
  const sourceSpan = Math.max(0, first.out - first.in);
  return { src: first.src, t: first.in + Math.min(0.6, sourceSpan / 2) };
}
