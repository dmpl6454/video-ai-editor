// Engine-level FAULTS and their fallbacks (INSTANT_PREVIEW_SPEC §7 fallback
// ladder, §3.4 context loss, §13 P1-R1):
//
// * webglcontextlost: the 2D snapshot stands in for the canvas, but only
//   while it holds the frame on screen; playing, picture and sound stop
//   together at presentedK (resumed on restore). Not restored in 2 s: the
//   server preview.
// * decode errors: a MediaError on laneA's element (WebKit ends the
//   MediaSource with a decode error, and every later append then throws, so
//   the lane is dead) is counted; below 3 in 60 s the lane is REBUILT (a new
//   MediaSource, the window re-appended) and the picture goes on from where
//   it was; the 3rd within 60 s is the server preview. Before this a single
//   decode error left the engine on a frozen frame for good: the dead lane
//   never reached laneA's own 3-error count (its pump stops once the
//   MediaSource is not 'open').

export const CONTEXT_RESTORE_MS = 2000
export const DECODE_ERROR_WINDOW_MS = 60_000
export const FATAL_DECODE_ERRORS = 3

export interface RecoveryHost {
  isPlaying(): boolean
  presentedK(): number
  /** The engine is attached, in client mode, not destroyed. */
  live(): boolean
  compositorLost(): boolean
  snapshotK(): number
  clearSnapshot(): void
  /** Show the spinner (the canvas is not the frame on screen). */
  spinner(on: boolean): void
  /** A pause the engine did not issue ('context'): picture and sound stop. */
  pauseExternal(cause: 'context'): void
  /** After a restore: resume if the context loss stopped playback (true when it did). */
  resumeAfterRestore(): boolean
  /** The texture is gone: forget which frame the element holds. */
  forgetElementFrame(): void
  showSnapshot(on: boolean): void
  showPaused(): void
  /** A fresh laneA (new MediaSource) on the same element. */
  rebuildLane(): void
  stopPlayback(why: string): void
  play(): void
  fallback(reason: string): void
  emitStatus(): void
  now?(): number
}

export class EngineRecovery {
  readonly stats = { decodeErrors: 0, laneRebuilds: 0 }
  private readonly host: RecoveryHost
  private contextTimer: ReturnType<typeof setTimeout> | null = null
  private errorTimes: number[] = []

  constructor(host: RecoveryHost) {
    this.host = host
  }

  private now(): number {
    return this.host.now?.() ?? performance.now()
  }

  // ------------------------------------------------------ context loss

  readonly onContextLost = (): void => {
    const h = this.host
    this.clearContextTimer()
    this.contextTimer = setTimeout(() => {
      this.contextTimer = null
      if (h.compositorLost()) h.fallback('webgl-lost')
    }, CONTEXT_RESTORE_MS)
    if (h.isPlaying()) h.pauseExternal('context')
    if (h.snapshotK() !== h.presentedK()) {
      h.clearSnapshot()
      h.spinner(true)
    }
    h.emitStatus()
  }

  readonly onContextRestored = (): void => {
    const h = this.host
    this.clearContextTimer()
    h.showSnapshot(false)
    // the texture is gone: re-seek (paused) or wait for the next rVFC
    h.forgetElementFrame()
    if (!h.isPlaying() && !h.resumeAfterRestore()) h.showPaused()
    h.emitStatus()
  }

  // ------------------------------------------------------ decode errors

  /** laneA's element reported a MediaError (`code` 3 is MEDIA_ERR_DECODE;
   *  any error on a MediaSource-backed element leaves it unusable). */
  onMediaError(code: number): void {
    const h = this.host
    if (!h.live()) return
    this.stats.decodeErrors++
    const t = this.now()
    this.errorTimes = this.errorTimes.filter((x) => t - x < DECODE_ERROR_WINDOW_MS)
    this.errorTimes.push(t)
    console.warn(`[preview engine] media error ${code} (${this.errorTimes.length} in 60 s)`)
    if (this.errorTimes.length >= FATAL_DECODE_ERRORS) {
      h.fallback('decode-errors')
      return
    }
    const wasPlaying = h.isPlaying()
    if (wasPlaying) h.stopPlayback('decode-error')
    this.stats.laneRebuilds++
    h.rebuildLane()
    if (wasPlaying) h.play()
    else h.showPaused()
    h.emitStatus()
  }

  /** Decode errors counted in the last 60 s (tests, telemetry). */
  get recentErrors(): number {
    const t = this.now()
    return this.errorTimes.filter((x) => t - x < DECODE_ERROR_WINDOW_MS).length
  }

  private clearContextTimer(): void {
    if (this.contextTimer) clearTimeout(this.contextTimer)
    this.contextTimer = null
  }

  destroy(): void {
    this.clearContextTimer()
  }
}
