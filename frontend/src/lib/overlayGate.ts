// Half-open overlay windows on frame-exact bounds — the client mirror of
// `edl/timebase.py::enable_window` (and `text_overlay.enable_expr`, which
// every export overlay gate — text, captions, stickers, PiPs — goes through).
//
// QA-016: both sides used CLOSED windows (`between(t,start,end)` in ffmpeg,
// `start <= t && t <= end` here), so on the frame where one caption ends
// exactly as the next begins — every SRT cue change — both were drawn,
// overprinted. The window is `[start, end)` on the frame grid: the frame shown
// at n/fps is inside iff frameOf(start) <= n < frameOf(end). The bounds sit
// half a frame before each boundary frame, so a clock reading 2.9999999 for
// frame 90 cannot flip it, and a non-frame-aligned time lands on the nearest
// frame exactly as the server quantises it. Keep the two in lockstep.

//
// The arithmetic is `preview/timeline/timebase.ts` (the exact port of
// `edl/timebase.py`, spec R2): one copy of frame_of/enable_window in the
// frontend, so a gate boundary can never sit a frame away from the server's.
import {
  enableWindow as tbEnableWindow, frameOf as tbFrameOf,
} from './preview/timeline/timebase'

const usableFps = (fps: number) => (fps > 0 && Number.isFinite(fps) ? fps : 30)

/** Nearest frame index to `t` (non-negative), as `timebase.frame_of`. */
export function frameOf(t: number, fps: number): number {
  return tbFrameOf(t, usableFps(fps))
}

/** `[lo, hi)` gate bounds in seconds for an overlay shown over `[start, end)`. */
export function enableWindow(start: number, end: number, fps: number): [number, number] {
  return tbEnableWindow(start, end, usableFps(fps))
}

/** Is an overlay with window `[start, end)` on screen at instant `t`? */
export function inEnableWindow(start: number, end: number, t: number, fps: number): boolean {
  const [lo, hi] = enableWindow(start, end, fps)
  return lo <= t && t < hi
}
