/**
 * The geometry of the filmstrip: zoom, pixels, and which frames to ask for.
 *
 * Every function here is pure. That is not tidiness for its own sake — the
 * filmstrip is the one part of this app that can hurt the Mac. The rate
 * limiter in `api/hardening.py` is keyed on the path WITHOUT its query string,
 * so every `/thumb?src=…&t=…` a strip fires shares one 60-per-second bucket:
 * a strip that asks for a frame per pixel does not render slowly, it 429s and
 * renders nothing, and then the connection reducer reports the phone as
 * throttled. So the number of requests a given zoom level produces is a fact
 * that has to be provable at a desk, and these are the functions that prove it.
 *
 * THREE DECISIONS WORTH THE WORDS:
 *
 * 1. Thumbnail times are QUANTISED to a step, and the step grows as you zoom
 *    out. `/thumb` caches on disk per (src, t, h), and the phone caches on the
 *    same key — so two clips cut from the same file, and the same clip at two
 *    zoom levels, reuse frames instead of minting new ones. An unquantised
 *    time would make every pixel of pinch-zoom a cache miss.
 *
 * 2. `t` is SOURCE seconds, not timeline seconds. `main.py::get_thumb` hands
 *    it straight to ffmpeg against the clip's own file. A sped-up or trimmed
 *    clip therefore needs the mapping in `visibleThumbTimes`, not a subtraction
 *    at the call site.
 *
 * 3. A `src` that has failed once is dropped from the output entirely rather
 *    than retried. `/thumb` answers 403 for any path outside the session dir,
 *    and `dispatch.py` legitimately puts external paths on the timeline (a LUT
 *    render, a b-roll hit from a folder the user pointed at). That 403 is a
 *    permanent property of the clip, so retrying it costs the rate-limit
 *    budget forever and can never succeed.
 */

import { clipDuration, mediaClipsOf, trackById, VIDEO_TRACK_ID } from "./edl";
import { MAX_THUMBS_IN_FLIGHT } from "./net";
import type { EDL } from "./types";

// ---------------------------------------------------------------------------
// Zoom
// ---------------------------------------------------------------------------

/**
 * Zoom bounds, in pixels per timeline second.
 *
 * The desktop allows 10–600 (`frontend/src/store.ts:443`) across a 1400 px
 * canvas. A phone has roughly a quarter of that width and a fingertip instead
 * of a mouse, so the range is narrower at both ends: below 8 px/s a two-second
 * clip is too small to hit, and above 220 px/s a swipe travels less than two
 * seconds and scrubbing stops feeling like scrubbing.
 */
export const MIN_PIXELS_PER_SECOND = 8;
export const MAX_PIXELS_PER_SECOND = 220;

/** Opening zoom: about seven seconds across a phone screen. */
export const DEFAULT_PIXELS_PER_SECOND = 56;

export function clampPixelsPerSecond(pps: number): number {
  if (!Number.isFinite(pps)) return DEFAULT_PIXELS_PER_SECOND;
  return Math.min(MAX_PIXELS_PER_SECOND, Math.max(MIN_PIXELS_PER_SECOND, pps));
}

// ---------------------------------------------------------------------------
// Pixels ↔ seconds
// ---------------------------------------------------------------------------

/** Timeline seconds at a horizontal offset inside the strip's content. */
export function timeAtX(x: number, pixelsPerSecond: number): number {
  return x / clampPixelsPerSecond(pixelsPerSecond);
}

/** The content offset a timeline second sits at. */
export function xForTime(t: number, pixelsPerSecond: number): number {
  return t * clampPixelsPerSecond(pixelsPerSecond);
}

// ---------------------------------------------------------------------------
// Thumbnail sampling
// ---------------------------------------------------------------------------

/**
 * The quantised step ladder, in SOURCE seconds. 0.5 is the floor — it is what
 * `frontend/src/components/Timeline.tsx:519` has used since M1, and it is
 * already fine enough that consecutive tiles are visibly different frames.
 */
export const THUMB_STEPS: readonly number[] = [0.5, 1, 2, 5, 10, 30, 60];

/**
 * Roughly how wide one thumbnail should be on screen. Everything else follows:
 * the step is chosen so one step covers at least this many pixels, which is
 * what bounds the request count per screenful.
 */
export const IDEAL_TILE_PX = 64;

/**
 * The `h` we ask `/thumb` for. The endpoint clamps to [16, 270]; 96 is twice
 * the strip's 48pt row so it stays sharp on a 2× and 3× screen without asking
 * the Mac to encode a frame nobody will look at closely.
 */
export const THUMB_HEIGHT_PX = 96;

/** Never finer than the floor of the ladder, and never off the ladder. */
export function thumbStepSeconds(pixelsPerSecond: number): number {
  const pps = clampPixelsPerSecond(pixelsPerSecond);
  const floor = THUMB_STEPS[0] as number;
  for (const step of THUMB_STEPS) {
    if (step * pps >= IDEAL_TILE_PX) return step;
  }
  // Zoomed all the way out: the coarsest rung is the best we can do, and the
  // total cap below is what actually protects the rate limiter there.
  return THUMB_STEPS[THUMB_STEPS.length - 1] ?? floor;
}

/** A media clip flattened to the numbers the strip draws with. */
export interface StripClip {
  id: string;
  src: string;
  /** SOURCE seconds. */
  in: number;
  out: number;
  /** TIMELINE seconds. */
  start: number;
  /** TIMELINE seconds occupied — already divided by `speed`. */
  duration: number;
  /** Playback rate; source seconds advance this fast per timeline second. */
  speed: number;
}

/** The media clips of one track, in timeline order, ready to draw. */
export function stripClipsOf(edl: EDL | null, trackId: string = VIDEO_TRACK_ID): StripClip[] {
  return mediaClipsOf(trackById(edl, trackId)).map((c) => {
    const duration = clipDuration(c);
    // Derive `speed` from the two lengths rather than re-reading `c.speed`:
    // that keeps this in step with `clipDuration`'s handling of speed CURVES,
    // which are dicts and would otherwise land here as NaN.
    const sourceSpan = c.out - c.in;
    const speed = duration > 0 && sourceSpan > 0 ? sourceSpan / duration : 1;
    return { id: c.id, src: c.src, in: c.in, out: c.out, start: c.start, duration, speed };
  });
}

/**
 * The clip under a playhead, on the HALF-OPEN interval `[start, start+dur)`.
 *
 * A playhead sitting exactly on a cut belongs to the clip that is about to
 * play, not the one that just ended — which is also what `split_at` does, so
 * "split here" and "this is the clip" cannot disagree. Iterating in timeline
 * order and keeping the LAST match gives the same answer when two clips
 * overlap during a transition.
 */
export function clipAt(clips: readonly StripClip[], time: number): StripClip | null {
  let found: StripClip | null = null;
  for (const c of clips) {
    if (time >= c.start && time < c.start + c.duration) found = c;
  }
  return found;
}

/** One frame request, and where its tile sits in the strip's content. */
export interface ThumbTile {
  /** Cache key — `src|t`, so two clips cut from one file share the image. */
  key: string;
  clipId: string;
  src: string;
  /** SOURCE seconds. An exact multiple of the step. */
  t: number;
  /** Content offset of the tile's left edge, in pixels. */
  x: number;
  /** Tile width in pixels. */
  width: number;
}

export interface FilmstripRequest {
  clips: readonly StripClip[];
  /** The visible timeline window, in seconds. */
  windowStart: number;
  windowEnd: number;
  pixelsPerSecond: number;
  /** Sources whose `/thumb` has already failed. See note 3 in the header. */
  failedSrcs?: ReadonlySet<string>;
  /** Hard ceiling on requests. Defaults to the rate-limiter-safe 24. */
  maxTiles?: number;
}

/** Float slack when snapping a source time onto the step grid. */
const GRID_EPSILON = 1e-6;

/**
 * ffmpeg cannot seek to a clip's exact out-point and return a frame, so the
 * last grid point is pulled just inside it. 50 ms is under two frames at 30fps.
 */
const TAIL_MARGIN_S = 0.05;

/**
 * Which frames to fetch for the visible part of the strip.
 *
 * The output is capped ABSOLUTELY at `maxTiles`, not per clip and not per
 * screenful, because the cap is the thing that keeps a pathological input — a
 * three-hour timeline whose whole length is on screen — from firing tens of
 * thousands of requests at a 60-per-second bucket. Over the cap, the grid is
 * strided rather than truncated, so the strip stays evenly sampled across its
 * whole width instead of showing frames for the first two seconds and grey
 * after that.
 */
export function visibleThumbTimes(req: FilmstripRequest): ThumbTile[] {
  const pps = clampPixelsPerSecond(req.pixelsPerSecond);
  const step = thumbStepSeconds(pps);
  const maxTiles = Math.max(1, req.maxTiles ?? MAX_THUMBS_IN_FLIGHT);
  const failed = req.failedSrcs ?? new Set<string>();

  const spans = req.clips
    .filter((c) => !failed.has(c.src) && c.duration > 0)
    .map((c) => gridSpan(c, req.windowStart, req.windowEnd, step))
    .filter((s): s is GridSpan => s !== null);

  const total = spans.reduce((n, s) => n + s.count, 0);
  if (total === 0) return [];

  // Stride, don't truncate: every stride-th grid point across the whole run.
  const stride = Math.max(1, Math.ceil(total / maxTiles));
  const tileSeconds = stride * step;

  const tiles: ThumbTile[] = [];
  let index = 0;
  for (const span of spans) {
    for (let k = 0; k < span.count; k += 1, index += 1) {
      if (index % stride !== 0) continue;
      const t = (span.firstIndex + k) * step;
      const { clip } = span;
      const tStart = clip.start + (t - clip.in) / clip.speed;
      // A strided tile covers the ground its skipped neighbours would have.
      const widthSeconds = Math.min(tileSeconds / clip.speed, clipEndOf(clip) - tStart);
      tiles.push({
        key: thumbKey(clip.src, t),
        clipId: clip.id,
        src: clip.src,
        t,
        x: xForTime(tStart, pps),
        width: Math.max(1, widthSeconds * pps),
      });
      if (tiles.length >= maxTiles) return tiles;
    }
  }
  return tiles;
}

interface GridSpan {
  clip: StripClip;
  /** Index of the first grid point (multiply by the step for the time). */
  firstIndex: number;
  count: number;
}

/** The grid points of one clip that fall inside the visible window. */
function gridSpan(clip: StripClip, windowStart: number, windowEnd: number, step: number): GridSpan | null {
  const visibleStart = Math.max(clip.start, windowStart);
  const visibleEnd = Math.min(clipEndOf(clip), windowEnd);
  if (visibleEnd <= visibleStart) return null;

  // Timeline → source. `speed` source seconds pass per timeline second.
  const sourceLo = clip.in + (visibleStart - clip.start) * clip.speed;
  const sourceHi = Math.min(clip.in + (visibleEnd - clip.start) * clip.speed, clip.out - TAIL_MARGIN_S);
  if (sourceHi < sourceLo) return null;

  const firstIndex = Math.ceil(sourceLo / step - GRID_EPSILON);
  const lastIndex = Math.floor(sourceHi / step + GRID_EPSILON);
  // A clip shorter than one step contains no grid point. It draws as a plain
  // coloured block — at these zooms it is a few pixels wide, and inventing an
  // off-grid time for it would cost a cache miss on every single zoom level.
  if (lastIndex < firstIndex) return null;

  return { clip, firstIndex, count: lastIndex - firstIndex + 1 };
}

function clipEndOf(c: StripClip): number {
  return c.start + c.duration;
}

/** The shared cache key for one frame. */
export function thumbKey(src: string, t: number): string {
  return `${src}|${t.toFixed(2)}`;
}

/** The API path for one frame. `h` is clamped server-side to [16, 270]. */
export function thumbPath(sid: string, src: string, t: number, height = THUMB_HEIGHT_PX): string {
  return `/api/sessions/${sid}/thumb?src=${encodeURIComponent(src)}&t=${t}&h=${height}`;
}

// ---------------------------------------------------------------------------
// Ruler
// ---------------------------------------------------------------------------

const TICK_STEPS: readonly number[] = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600];

/** Seconds between ruler ticks, chosen so labels never collide. */
export function tickStepSeconds(pixelsPerSecond: number, minLabelPx = 68): number {
  const pps = clampPixelsPerSecond(pixelsPerSecond);
  for (const step of TICK_STEPS) {
    if (step * pps >= minLabelPx) return step;
  }
  return TICK_STEPS[TICK_STEPS.length - 1] ?? 600;
}

/** Tick times across a window, capped so a huge span cannot emit a huge array. */
export function tickTimes(windowStart: number, windowEnd: number, step: number, maxTicks = 60): number[] {
  if (!(step > 0) || windowEnd <= windowStart) return [];
  const first = Math.ceil(windowStart / step - GRID_EPSILON);
  const last = Math.floor(windowEnd / step + GRID_EPSILON);
  const out: number[] = [];
  for (let k = first; k <= last && out.length < maxTicks; k += 1) out.push(k * step);
  return out;
}

/** Advance one frame. Falls back to 30fps rather than refusing to step, which
 *  is what a canvas with a nonsense fps would otherwise do. */
export function frameStep(fps: number | undefined): number {
  return Number.isFinite(fps) && (fps as number) > 0 ? 1 / (fps as number) : 1 / 30;
}

/** Keep a playhead inside the timeline. */
export function clampTime(t: number, extent: number): number {
  if (!Number.isFinite(t)) return 0;
  return Math.min(Math.max(0, t), Math.max(0, extent));
}
