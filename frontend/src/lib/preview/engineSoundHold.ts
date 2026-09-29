// The engine's hold for sound not in memory yet (INSTANT_PREVIEW_SPEC §11.1:
// for spans not yet encoded, "buffering ≤ 700 ms, together with video").
// Final sweep 3, run 3: play pressed while a fresh import's sound chunks were
// still loading played the first 0.4 s (1 s with a slow server) as silence
// while picture and playhead ran, and the range read EXACT. play() now asks
// the sink first; while it loads the chunks under the start, the transport
// shows buffering and neither picture nor sound runs. After SOUND_HOLD_MS
// both run anyway — the sink labels what it still cannot play
// (AudioSink.soundLoadingFrames → APPROX 'audio:pending').

import type { AudioSink } from './engine'

/** Longest hold before picture and sound run anyway (§11.1). */
export const SOUND_HOLD_MS = 700

/** What the hold needs from the engine. */
export interface SoundHoldHost {
  sink(): AudioSink
  isPlaying(): boolean
  isDestroyed(): boolean
  /** Enter / leave the buffering state (the spinner, a 'buffering' event). */
  setBuffering(on: boolean): void
  /** Start picture and sound from where the transport stands now. */
  run(): void
}

export class SoundHold {
  /** A hold is in progress (play() was called, nothing runs yet). */
  active = false
  readonly stats = { holds: 0, timeouts: 0 }
  private token = 0
  private readonly host: SoundHoldHost

  constructor(host: SoundHoldHost) {
    this.host = host
  }

  /** From play(), inside the user's gesture: true when the sound under
   *  output sample `fromSample` is not in memory — the host must not run
   *  now; run() comes when it is (or after SOUND_HOLD_MS). */
  begin(fromSample: number): boolean {
    const wait = this.host.sink().soundHold?.(fromSample, SOUND_HOLD_MS) ?? null
    if (!wait) return false
    const token = ++this.token
    this.active = true
    this.stats.holds++
    this.host.setBuffering(true)
    void wait.then((ok) => ok, (e: unknown) => {
      console.warn('[preview engine] waiting for the sound failed', e)
      return false
    }).then((ok) => {
      if (token !== this.token) return
      this.active = false
      if (!ok) this.stats.timeouts++
      if (!this.host.isPlaying() || this.host.isDestroyed()) return
      this.host.setBuffering(false)
      this.host.run()
    })
    return true
  }

  /** A pause, a seek or a new play(): the hold in progress never runs. */
  cancel(): void {
    this.token++
    this.active = false
  }
}
