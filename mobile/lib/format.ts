/**
 * Display formatting. Pure functions only — no React, no I/O, no clock reads
 * except where the signature says so — because these are the pieces the whole
 * app renders numbers through and they have to be testable in isolation.
 *
 * House rule for every function here: an input the caller could not have
 * checked (NaN, a negative duration, an undefined size from the photo picker)
 * produces an em dash, never "NaN" and never a throw. A blank cell is a
 * legible "we do not know"; "NaN:aN" is a bug report.
 */

const EM_DASH = "—";

// ---------------------------------------------------------------------------
// Time
// ---------------------------------------------------------------------------

/**
 * Timeline time as an editor writes it: "12.4" under a minute, "1:02.4" under
 * an hour, "1:02:03.4" beyond. One decimal place, because the timeline ruler
 * and the playhead readout have to agree and frame numbers would need the fps.
 */
export function timecode(seconds: number): string {
  if (!Number.isFinite(seconds)) return EM_DASH;
  const neg = seconds < 0;
  const t = Math.abs(seconds);
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60);
  const s = t % 60;
  const ss = s.toFixed(1).padStart(4, "0");
  const body = h > 0 ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
  return neg ? `-${body}` : body;
}

/**
 * The same instant to frame precision — "00:01:02:11" — for anywhere the user
 * is being asked to trust an exact cut point. Needs the canvas fps, and
 * refuses rather than guessing when it does not have a usable one.
 */
export function timecodeFrames(seconds: number, fps: number): string {
  if (!Number.isFinite(seconds) || !Number.isFinite(fps) || fps <= 0) return EM_DASH;
  const total = Math.max(0, seconds);
  const whole = Math.floor(total);
  // Round the sub-second remainder, then carry: 0.9999 s at 30 fps is frame 30,
  // which is second 1 frame 0 — not "00:00:00:30", a timecode that cannot exist.
  let frames = Math.round((total - whole) * fps);
  let secs = whole;
  if (frames >= fps) {
    frames = 0;
    secs += 1;
  }
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(Math.floor(secs / 3600))}:${p(Math.floor((secs % 3600) / 60))}:${p(secs % 60)}:${p(frames)}`;
}

/** Coarse spoken duration for a summary line: "4 min 12 s", "38 s". */
export function humanDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return EM_DASH;
  const total = Math.round(seconds);
  if (total < 60) return `${total} s`;
  const m = Math.floor(total / 60);
  const s = total % 60;
  if (m < 60) return s === 0 ? `${m} min` : `${m} min ${s} s`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return rm === 0 ? `${h} hr` : `${h} hr ${rm} min`;
}

/**
 * The photo picker reports `asset.duration` in MILLISECONDS while the EDL and
 * every backend field are in seconds. Mixing them once put a 1000× duration on
 * the timeline, so the conversion is a named function rather than a `/ 1000`
 * somewhere in a screen.
 */
export function millisToSeconds(ms: number | null | undefined): number | null {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return null;
  return ms / 1000;
}

/** "3 days ago", "just now". Coarse on purpose. `at` is a UNIX epoch in ms. */
export function relativeTime(at: number | null | undefined, now = Date.now()): string {
  if (at == null || !Number.isFinite(at)) return EM_DASH;
  const ms = now - at;
  // A clock skew between the Mac and the phone must not render "in -3 minutes".
  if (ms < 60_000) return "just now";
  const min = Math.floor(ms / 60_000);
  if (min < 60) return `${min} min ago`;
  const h = Math.floor(min / 60);
  if (h < 24) return `${h} hr ago`;
  const d = Math.floor(h / 24);
  if (d < 30) return `${d} day${d === 1 ? "" : "s"} ago`;
  const mo = Math.floor(d / 30);
  return `${mo} month${mo === 1 ? "" : "s"} ago`;
}

/** `storage.py::list_sessions` sends `created_at` as a UNIX float in SECONDS. */
export function relativeTimeFromEpochSeconds(sec: number | null | undefined, now = Date.now()): string {
  if (sec == null || !Number.isFinite(sec)) return EM_DASH;
  return relativeTime(sec * 1000, now);
}

// ---------------------------------------------------------------------------
// Size and count
// ---------------------------------------------------------------------------

const UNITS = ["B", "KB", "MB", "GB", "TB"] as const;

/**
 * `asset.fileSize` from the photo picker is very often undefined, which is why
 * null is a first-class input here rather than something the caller has to
 * pre-check. An unknown size renders as an em dash and the progress bar that
 * would have used it goes indeterminate.
 */
export function humanBytes(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n) || n < 0) return EM_DASH;
  let v = n;
  let i = 0;
  while (v >= 1024 && i < UNITS.length - 1) {
    v /= 1024;
    i += 1;
  }
  return i === 0 ? `${Math.round(v)} B` : `${v.toFixed(v >= 100 ? 0 : 1)} ${UNITS[i]}`;
}

/** 0..1 as a whole percent, clamped. Anything unusable returns null so the
 *  caller can render an indeterminate bar instead of a confident 0%. */
export function percent(fraction: number | null | undefined): number | null {
  if (fraction == null || !Number.isFinite(fraction)) return null;
  return Math.round(Math.min(1, Math.max(0, fraction)) * 100);
}

export function formatCount(n: number): string {
  if (!Number.isFinite(n)) return EM_DASH;
  return Math.round(n).toLocaleString();
}

/** The first 7 characters of an EDL hash — enough to tell two states apart in
 *  a status line without pretending the whole digest is meaningful to a human. */
export function shortHash(hash: string | null | undefined): string {
  if (!hash) return EM_DASH;
  return hash.slice(0, 7);
}

/** "1920×1080" — the multiplication sign, not the letter x. */
export function dimensions(w: number, h: number): string {
  if (!Number.isFinite(w) || !Number.isFinite(h) || w <= 0 || h <= 0) return EM_DASH;
  return `${Math.round(w)}×${Math.round(h)}`;
}

/**
 * The trailing name of a backend `src` path, for a clip label. Handles both
 * separators because a project imported on Windows can put backslashes in an
 * EDL the Mac then serves.
 */
export function basename(path: string): string {
  const cut = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\"));
  const name = cut === -1 ? path : path.slice(cut + 1);
  return name.length > 0 ? name : path;
}

/**
 * Screen readers verbalise "›" literally. Any label containing a navigation
 * path is passed through this for `accessibilityLabel`.
 */
export function spoken(path: string): string {
  return path.replace(/\s*›\s*/g, ", then ").replace(/\s*→\s*/g, ", then ");
}
