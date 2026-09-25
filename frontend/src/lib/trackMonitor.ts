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

/** The two 12 px boxes in a label row of height `h` starting at `top`:
 *  M above the row's middle, S below it, both at x 6–18. */
export const MONITOR_BOX = { x: 6, size: 12 } as const

export function monitorBoxes(top: number, h: number): { mute: number; solo: number } {
  const mid = top + h / 2
  return { mute: mid - 13, solo: mid + 1 }
}

/** Which box (if any) a point on the label canvas hits. */
export function monitorHit(x: number, y: number, top: number, h: number): 'mute' | 'solo' | null {
  if (x < MONITOR_BOX.x || x > MONITOR_BOX.x + MONITOR_BOX.size) return null
  const b = monitorBoxes(top, h)
  if (y >= b.mute && y <= b.mute + MONITOR_BOX.size) return 'mute'
  if (y >= b.solo && y <= b.solo + MONITOR_BOX.size) return 'solo'
  return null
}
