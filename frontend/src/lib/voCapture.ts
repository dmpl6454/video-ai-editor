// The pure half of the voiceover recorder (QA-085).
//
// Recording used to PAUSE playback (so narrating to picture was impossible),
// showed no level and no count-in, placed the clip at the raw playhead (not on
// a frame), and a mic-permission prompt that never answered left the button on
// "Requesting mic access…" forever with no way out. The component now counts
// in 3-2-1, plays the timeline from the frame-snapped record point while it
// captures, draws a live level meter, and lets the user cancel a pending
// permission request; these helpers are the parts that can be unit-tested.

import { toFrameGrid } from './frameStep'

/** Seconds of count-in before capture and playback start. */
export const COUNTDOWN_S = 3

/** After this long waiting on the mic prompt, say what to look for. */
export const PERMISSION_HINT_MS = 8000

/** Where the clip lands: the playhead on the project's frame grid. */
export function recordStart(playhead: number, fps: unknown): number {
  return toFrameGrid(Math.max(0, playhead), fps)
}

/** The meter floor: anything quieter reads as silence. */
export const METER_FLOOR_DB = -60

/** RMS level of one analyser frame (time-domain samples in -1..1) as dBFS and
 *  a 0..1 meter fraction over METER_FLOOR_DB..0 dBFS. */
export function levelOf(samples: ArrayLike<number>): { dbfs: number; fraction: number } {
  let sum = 0
  for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i]
  const rms = samples.length ? Math.sqrt(sum / samples.length) : 0
  const dbfs = rms > 0 ? 20 * Math.log10(rms) : -Infinity
  const fraction = Number.isFinite(dbfs) ? Math.min(1, Math.max(0, (dbfs - METER_FLOOR_DB) / -METER_FLOOR_DB)) : 0
  return { dbfs, fraction }
}

export const CANCELLED: unique symbol = Symbol('cancelled')

/**
 * A promise the user can walk away from. getUserMedia cannot be aborted, so a
 * cancelled request may still resolve later — `onLate` gets that value (the
 * caller releases the stream, so the OS mic indicator does not stay lit).
 */
export function cancellable<T>(p: Promise<T>, onLate: (v: T) => void): { promise: Promise<T | typeof CANCELLED>; cancel(): void } {
  let cancelled = false
  let settle: ((v: typeof CANCELLED) => void) | null = null
  const gate = new Promise<typeof CANCELLED>((r) => { settle = r })
  const wrapped = p.then((v) => {
    if (cancelled) { onLate(v); return CANCELLED }
    return v
  })
  return {
    promise: Promise.race([wrapped, gate]),
    cancel() { cancelled = true; settle?.(CANCELLED) },
  }
}
