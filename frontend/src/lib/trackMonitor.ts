// Track monitoring — the M (mute) and S (solo) boxes on a sound lane's label
// and the one rule for whether a lane is HEARD (QA-086).
//
// Mirrors render/audio_mix.apply_solo: while any track is soloed, only soloed
// tracks are heard; a muted track is never heard. The timeline greys the
// waveform of a lane it will not play, so the rule must match the renderer.

import type { Track } from '../types'

export function anySolo(tracks: Track[]): boolean {
  return tracks.some((t) => !!t.solo)
}

export function isHeard(t: Track, tracks: Track[]): boolean {
  if (t.muted) return false
  return anySolo(tracks) ? !!t.solo : true
}

/** Where a sound lane's Mute and Solo buttons sit in its label row
 *  (wave C review: they were 12 px canvas boxes holding 9 px "M"/"S", mouse
 *  only). Real <button>s now — keyboard-reachable, aria-pressed, lucide icons
 *  — side by side under the lane name, each a 22×18 target. */
export const MONITOR_BUTTON = { w: 22, h: 18, gap: 2, left: 6, bottom: 2 } as const

export interface MonitorRect { x: number; y: number; w: number; h: number }

export function monitorButtons(top: number, h: number): { mute: MonitorRect; solo: MonitorRect } {
  const { w, h: bh, gap, left, bottom } = MONITOR_BUTTON
  const y = top + h - bottom - bh
  return { mute: { x: left, y, w, h: bh }, solo: { x: left + w + gap, y, w, h: bh } }
}

/** Baseline of a sound lane's name: the line above its buttons. */
export function laneNameBaseline(top: number): number {
  return top + 13
}

/** The buttons' accessible names — the action, then the lane. */
export function monitorLabels(lane: string): { mute: string; solo: string } {
  return { mute: `Mute ${lane}`, solo: `Solo ${lane}` }
}
