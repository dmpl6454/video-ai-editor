// Pauses the engine did not issue (INSTANT_PREVIEW_SPEC §3.5, §7; the
// milestone-1 product requirement): WebKit pauses a muted <video> by itself
// when the page is hidden or the window occluded, and it can drop the WebGL
// context mid-play. Either way picture and sound stop TOGETHER at the
// presented k (5 ms ramp), the state shows paused, and on return both resume
// from a fresh anchor — or stay paused — per the user's last intent. Sound
// never runs on while the picture is frozen.

import type { AudioSink, ExternalPauseEvent } from './engine'
import type { EngineOptions } from './engineOptions'

/** After an external pause, the page must stay visible this long before
 *  picture and sound resume: WKWebView flips an occluded window's page
 *  hidden/visible every ~10 ms for a while (measured in the harness), and a
 *  resume per flip would play frames and sound nobody can see. */
export const RESUME_SETTLE_MS = 250
/** How long a resume after an external pause waits for the sound (above). */
export const RESUME_AUDIO_TIMEOUT_MS = 4000
/** The engine's own video.pause() is not an external pause for this long. */
const OWN_PAUSE_MS = 1000

export type ExternalCause = ExternalPauseEvent['cause']

/** What the pause logic needs from the engine. */
export interface ExternalHost {
  readonly opts: EngineOptions
  isPlaying(): boolean
  isDestroyed(): boolean
  presentedK(): number
  intent(): 'play' | 'pause'
  setIntent(i: 'play' | 'pause'): void
  /** Picture and sound stop together at presentedK. */
  stopPlayback(why: string): void
  play(): void
  sink(): AudioSink
  /** The picture can be drawn (the WebGL context is not lost). */
  canDraw(): boolean
  emitPause(e: ExternalPauseEvent): void
  emitStatus(): void
  /** The page went hidden / visible (laneA appends and prefetch, §3.5). */
  onHidden(): void
  onShown(): void
  /** The element played by itself: where it stands is unknown now. */
  elementMoved(): void
}

export class ExternalPauses {
  /** Stopped by an external pause (not by the user) since the last play(). */
  paused = false
  cause: ExternalCause | null = null
  count = 0
  private expectUntil = 0
  private resumeTimer: ReturnType<typeof setTimeout> | null = null
  private resumeToken = 0
  private readonly host: ExternalHost

  constructor(host: ExternalHost) {
    this.host = host
  }

  /** The engine itself is about to pause the element. */
  expectOwnPause(): void {
    this.expectUntil = performance.now() + OWN_PAUSE_MS
  }

  private ownPause(): boolean {
    return performance.now() <= this.expectUntil
  }

  listen(video: HTMLVideoElement): void {
    video.addEventListener('pause', () => {
      if (this.host.isPlaying() && !this.ownPause()) this.pause(this.pageHidden() ? 'hidden' : 'element')
    })
    video.addEventListener('play', () => {
      // WebKit resuming the element on its own: never without the sound.
      // Park it; after an external pause with the intent to play, both
      // resume together once the page has settled visible.
      if (this.host.isPlaying() || this.host.isDestroyed()) return
      this.expectOwnPause()
      video.pause()
      // parked is not still: on an occluded window's visible flips WebKit
      // went on presenting frames 178 → 223 with `paused` true (measured),
      // so the next play or paused show must seek it, not trust it
      this.host.elementMoved()
      if (this.paused && this.host.intent() === 'play' && this.cause !== 'context') this.scheduleResume()
    })
  }

  /** The playing watchdog (every top-up tick): a pause WebKit made without
   *  firing 'pause' at us. */
  watch(video: HTMLVideoElement): void {
    if (video.paused && !this.ownPause()) this.pause('element')
  }

  private pageHidden(): boolean {
    return typeof document !== 'undefined' && document.visibilityState === 'hidden'
  }

  /** A pause someone else made: picture and sound stop together at the
   *  presented k; the user's intent to play is kept only when the engine
   *  will resume by itself (page shown again, context restored). */
  pause(cause: ExternalCause): void {
    const h = this.host
    if (!h.isPlaying()) return
    this.count++
    const k = h.presentedK()
    h.stopPlayback(`external:${cause}`)
    this.paused = true
    this.cause = cause
    const willResume = h.intent() === 'play'
      && (cause === 'context' || (cause === 'hidden' && h.opts.resumeOnVisible !== false))
    if (!willResume) h.setIntent('pause')
    h.emitPause({ k, cause, willResume })
    h.emitStatus()
  }

  /** The user played or paused: whatever stopped us before no longer counts. */
  reset(): void {
    this.paused = false
    this.cause = null
    this.cancelResume()
  }

  readonly onVisibility = (): void => {
    const h = this.host
    if (this.pageHidden()) {
      this.cancelResume()
      if (h.isPlaying() && h.opts.pauseOnHidden !== false) this.pause('hidden')
      h.onHidden()
      return
    }
    h.onShown()
    if (this.paused && h.intent() === 'play') this.scheduleResume()
  }

  /** The WebGL context came back: resume if it is what stopped playback. */
  onContextRestored(): boolean {
    if (!this.paused || this.cause !== 'context' || this.host.intent() !== 'play') return false
    if (this.pageHidden()) {
      // hidden meanwhile: the page's return resumes both
      this.cause = 'hidden'
      return false
    }
    this.paused = false
    this.cause = null
    this.host.play()
    return true
  }

  /** Resume picture and sound from a fresh anchor once the page has been
   *  visible for RESUME_SETTLE_MS (and the user still wants to play). */
  private scheduleResume(): void {
    const h = this.host
    if (h.opts.resumeOnVisible === false || h.isDestroyed()) return
    this.cancelResume()
    const token = ++this.resumeToken
    const still = () => token === this.resumeToken && !h.isDestroyed() && !this.pageHidden()
      && this.paused && h.intent() === 'play' && !h.isPlaying() && h.canDraw()
    this.resumeTimer = setTimeout(() => {
      this.resumeTimer = null
      if (!still()) return
      // the sound first: a context WebKit reports running may not render for
      // ~1.8 s after the page was hidden; the picture waits for it
      const sink = h.sink()
      const ready = sink.whenRunning ? sink.whenRunning(RESUME_AUDIO_TIMEOUT_MS) : Promise.resolve(true)
      void ready.then((ok) => {
        if (!still()) return
        this.paused = false
        this.cause = null
        if (ok) {
          h.play()
        } else {
          // no sound to resume with: stay paused rather than run silent
          h.setIntent('pause')
          h.emitStatus()
        }
      }, () => undefined)
    }, RESUME_SETTLE_MS)
  }

  cancelResume(): void {
    this.resumeToken++
    if (this.resumeTimer) clearTimeout(this.resumeTimer)
    this.resumeTimer = null
  }
}
