// What is running right now, for the top bar's activity chip and the rail's
// live-state dots (docs/design/LEFT_RAIL_SPEC.md §2.8, §6.2). A module-level
// Zustand store (the aiRuns.ts / layoutStore.ts pattern) so the chip, the rail
// and the panels read ONE truth:
//   - VoRecorder publishes `recording` (with its own stop, so the chip's Stop
//     is the same stop as the panel's — never a second path);
//   - lib/captionRun publishes `captions` (progress, ETA, the "Stopping…"
//     state and its cancel).
//
// `liveMessage` is what the chip's polite live region says. It is THROTTLED
// by construction: it changes only on a state change (started, stopped, done,
// cancelled) and when captions cross 25 / 50 / 75 %. Timer ticks never reach
// it — they go only into the chip buttons' aria-labels — because a hidden or
// chattering live region is exactly what this chip exists to fix (§2.8).
import { create } from 'zustand'

export interface RecordingActivity {
  /** Date.now() when capture started (the take's first frame). */
  startedAt: number
  /** Stops the take — VoRecorder's own stop. */
  stop(): void
}

export interface CaptionsActivity {
  /** 0..1, or null before the job has reported anything. */
  progress: number | null
  /** Seconds left, or null while there is not enough signal to be honest. */
  etaS: number | null
  /** Seconds since the run started (the chip shows it until an ETA exists). */
  elapsedS: number
  /** Cancel was pressed; the decoder stops at its next segment boundary. */
  cancelling: boolean
  cancel(): void
}

export type CaptionsOutcome = 'done' | 'cancelled' | 'failed'

export interface ActivityState {
  recording: RecordingActivity | null
  captions: CaptionsActivity | null
  liveMessage: string
  setRecording(r: RecordingActivity | null): void
  setCaptions(c: CaptionsActivity | null, outcome?: CaptionsOutcome): void
}

/** The progress thresholds (%) the live region announces. */
export const CAPTION_MILESTONES = [25, 50, 75] as const

/** The highest milestone `progress` has reached, or 0. */
export function milestoneOf(progress: number | null | undefined): number {
  const pct = Math.floor((progress ?? 0) * 100)
  let m = 0
  for (const t of CAPTION_MILESTONES) if (pct >= t) m = t
  return m
}

/** What the live region should say for a recording change, or null. */
export function recordingMessage(prev: RecordingActivity | null, next: RecordingActivity | null): string | null {
  if (!prev && next) return 'Recording a voiceover'
  if (prev && !next) return 'Recording stopped'
  return null
}

/** What the live region should say for a captions change, or null (no
 *  announcement: a tick, an ETA update, a progress step inside a band). */
export function captionsMessage(
  prev: CaptionsActivity | null, next: CaptionsActivity | null, outcome?: CaptionsOutcome,
): string | null {
  if (!prev && next) return 'Captions started'
  if (prev && !next) {
    if (outcome === 'cancelled') return 'Captions cancelled'
    if (outcome === 'failed') return 'Captions failed'
    return 'Captions done'
  }
  if (prev && next) {
    if (next.cancelling && !prev.cancelling) return 'Stopping captions'
    const m = milestoneOf(next.progress)
    if (m > milestoneOf(prev.progress)) return `Captions ${m}%`
  }
  return null
}

export const useActivityStore = create<ActivityState>()((set, get) => ({
  recording: null,
  captions: null,
  liveMessage: '',
  setRecording: (r) => {
    const msg = recordingMessage(get().recording, r)
    set(msg ? { recording: r, liveMessage: msg } : { recording: r })
  },
  setCaptions: (c, outcome) => {
    const msg = captionsMessage(get().captions, c, outcome)
    set(msg ? { captions: c, liveMessage: msg } : { captions: c })
  },
}))

/** "0:12" / "1:05:09" — the chip's clock, from whole seconds. */
export function clockLabel(totalS: number): string {
  const s = Math.max(0, Math.floor(totalS))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const ss = String(s % 60).padStart(2, '0')
  return h ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`
}

/** "31 seconds" / "2 minutes" — a spoken duration for an aria-label. */
export function spokenDuration(totalS: number): string {
  const s = Math.max(0, Math.round(totalS))
  if (s < 60) return `${s} second${s === 1 ? '' : 's'}`
  const m = Math.round(s / 60)
  return `${m} minute${m === 1 ? '' : 's'}`
}
