// The project frame — what one arrow-key step or one-frame nudge moves by.
//
// QA-009: this was a module constant `1 / 30` ("the timeline is normalised to
// 30fps"). Ingest now keeps each source's own rate and the project timebase is
// `edl.canvas.fps` (23.976, 25, 29.97, 50, 59.94, …), so on a 25 fps project
// 25 presses of → read 0.83 s instead of 1 s. Steps land ON the frame grid
// (frame n starts at n / fps) rather than adding 1/fps to whatever the
// playhead held, so a playhead parked between frames snaps onto one and a
// long run of steps never accumulates float drift.

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

/** `fps` for display: at most 3 decimals, no trailing zeros — 29.97, 23.976,
 *  59.94, 30. canvas.fps is stored exactly (30000/1001 = 29.97002997…), which
 *  is right for arithmetic and wrong for a label. */
export function formatFps(fps: unknown): string {
  return String(Number(projectFps(fps).toFixed(3)))
}

/** The "1080×1920 · 29.97fps · 12.3s" facts line (top bar and Ratio menu). */
export function canvasFacts(canvas: { w: number; h: number; fps: unknown }, duration: number): string {
  return `${canvas.w}×${canvas.h} · ${formatFps(canvas.fps)}fps · ${duration.toFixed(1)}s`
}
