// The export dialog's time-left estimate (QA-097 remainder).
//
// It used to be elapsed/progress × (1 − progress) from the first 2 %: any
// slow start (the preparation before frames flow, a chunk pass) is averaged
// into the whole run, so a job ending at 92 s said "~89s remaining" at 60 s,
// and the number jumped on every poll. Now:
//   • nothing is shown until progress ≥ 10 % AND 3 s have passed — a guess
//     from the first second is noise;
//   • the RATE is an exponential moving average of Δprogress/Δt between
//     samples (α = 0.3), so the estimate follows the current speed rather
//     than the whole history, and one jittery poll cannot swing it;
//   • between samples the estimate counts down with the clock;
//   • above 30 s it is rounded to 5 s, and it is said in words.
//
// Pure: `sampleEta` folds one progress sample into the state; `etaLeft`
// reads the seconds left at a given time; `etaText` words it.

export const ETA_MIN_PROGRESS = 0.1
export const ETA_MIN_ELAPSED_S = 3
export const ETA_ALPHA = 0.3

export interface EtaState {
  /** Time (s since the export began) and progress of the last sample. */
  t: number
  p: number
  /** EMA of progress per second; null until two samples moved forward. */
  rate: number | null
}

export const ETA_START: EtaState = { t: 0, p: 0, rate: null }

/** Fold one (elapsed seconds, progress 0..1) sample in. A sample that does not
 *  move forward in both time and progress changes nothing (a repeated poll). */
export function sampleEta(s: EtaState, t: number, p: number): EtaState {
  if (!Number.isFinite(t) || !Number.isFinite(p)) return s
  const dt = t - s.t
  const dp = p - s.p
  if (dt <= 0 || dp <= 0) return s
  const inst = dp / dt
  const rate = s.rate === null ? inst : ETA_ALPHA * inst + (1 - ETA_ALPHA) * s.rate
  return { t, p, rate }
}

/** Seconds left at elapsed time `now`, or null while it is too early to say. */
export function etaLeft(s: EtaState, now: number): number | null {
  if (s.rate === null || !(s.rate > 0)) return null
  if (s.p < ETA_MIN_PROGRESS || now < ETA_MIN_ELAPSED_S || s.p >= 1) return null
  const atSample = (1 - s.p) / s.rate
  return Math.max(0, atSample - Math.max(0, now - s.t))
}

/** "about 45 s left", "about 1 min 20 s left" — or '' when there is nothing to say. */
export function etaText(left: number | null): string {
  if (left === null || !Number.isFinite(left)) return ''
  if (left < 5) return 'a few seconds left'
  if (left <= 30) return `about ${Math.round(left)} s left`
  const r = Math.round(left / 5) * 5
  if (r < 60) return `about ${r} s left`
  const min = Math.floor(r / 60)
  const sec = r % 60
  return `about ${min} min${sec ? ` ${sec} s` : ''} left`
}
