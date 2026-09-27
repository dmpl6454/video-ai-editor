// Where an edit made WHILE PLAYING lands (INSTANT_PREVIEW_SPEC §3.2, §3.6,
// §11.1): one lead for the picture AND the sound, in TIME.
//
// laneA re-appends changed frames from presentedK + ceil(0.15 s · R) (nearer
// frames race the decoder), so the picture changes there; the sound was
// rescheduled from presentedK + 6 frames. Six frames is 200 ms at 30 fps but
// 250 ms at 24 fps — the whole §11.1 "audible (playing) ≤ 250 ms" budget
// before the dispatch round trip (measured in WKWebView, tone masters:
// p95 259 ms at 24 fps) — and 100 ms at 60 fps, where the sound then changed
// 50 ms BEFORE the picture. Both now land on the same frame.

import type { Rational } from '../timeline/timebase'

/** Seconds past the presented frame at which an edit while playing lands. */
export const EDIT_LEAD_S = 0.15

/** The lead in output frames at rate R (≥ 1). */
export function editLeadFrames(R: Rational): number {
  return Math.max(1, Math.ceil((EDIT_LEAD_S * R.num) / R.den - 1e-9))
}
