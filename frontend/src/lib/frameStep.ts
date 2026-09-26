// The project frame — what one arrow-key step or one-frame nudge moves by.
//
// QA-009: this was a module constant `1 / 30` ("the timeline is normalised to
// 30fps"). Ingest now keeps each source's own rate and the project timebase is
// `edl.canvas.fps` (23.976, 25, 29.97, 50, 59.94, …), so on a 25 fps project
// 25 presses of → read 0.83 s instead of 1 s. Steps land ON the frame grid
// (frame n starts at n / fps) rather than adding 1/fps to whatever the
// playhead held, so a playhead parked between frames snaps onto one and a
// long run of steps never accumulates float drift.

import { formatTimecode } from './timecode'

export const DEFAULT_FPS = 30

/** A usable project frame rate: `fps` when it is a finite 1..240, else 30 —
 *  the historical project default, same fallback as the backend timebase. */
export function projectFps(fps: unknown): number {
  const f = typeof fps === 'number' ? fps : Number(fps)
  return Number.isFinite(f) && f >= 1 && f <= 240 ? f : DEFAULT_FPS
}

/** Seconds per frame at `fps`. */
export function frameDuration(fps: unknown): number {
  return 1 / projectFps(fps)
}

/** The time `frames` frames away from `t` on the `fps` grid (never < 0). */
export function stepFrames(t: number, frames: number, fps: unknown): number {
  const f = projectFps(fps)
  const n = Math.round(Math.max(0, Number.isFinite(t) ? t : 0) * f) + frames
  return Math.max(0, n) / f
}

/** `t` moved onto the nearest frame boundary of the `fps` grid (never < 0) —
 *  what every client gesture (ruler click, scrub, drag delta) commits (QA-049). */
export function toFrameGrid(t: number, fps: unknown): number {
  return stepFrames(t, 0, fps)
}

/** Where a PAUSED <video> of a render at `fps` must seek to SHOW the frame at
 *  `t` (frame `round(t·fps)`, the frame the timecode names): the MIDDLE of
 *  that frame, never its exact pts. Chromium truncates `currentTime` to µs —
 *  2/30 s becomes 0.066666, just before frame 2 — so an exact-pts seek showed
 *  the PREVIOUS frame on every frame n ≡ 2 (mod 3) at 30 fps (QA-077). */
export function displaySeekTime(t: number, fps: unknown): number {
  const f = projectFps(fps)
  const n = Math.max(0, Math.round((Number.isFinite(t) ? t : 0) * f))
  return (n + 0.5) / f
}

/** The same guard for a SOURCE element (a PIP) whose own frame rate is not
 *  known: a bias far below any frame length that still clears the µs
 *  truncation. */
export const PAUSED_SEEK_BIAS_S = 0.001

/** `fps` for display: at most 3 decimals, no trailing zeros — 29.97, 23.976,
 *  59.94, 30. canvas.fps is stored exactly (30000/1001 = 29.97002997…), which
 *  is right for arithmetic and wrong for a label. */
export function formatFps(fps: unknown): string {
  return String(Number(projectFps(fps).toFixed(3)))
}

/** The "1080×1920 · 29.97fps · 00:01:31:00" facts line (top bar and Ratio
 *  menu). The length is SMPTE timecode, the format of every clock, ruler and
 *  History row beside it (it said "91.0s"). */
export function canvasFacts(canvas: { w: number; h: number; fps: unknown }, duration: number): string {
  return `${canvas.w}×${canvas.h} · ${formatFps(canvas.fps)}fps · ${formatTimecode(duration, canvas.fps)}`
}
