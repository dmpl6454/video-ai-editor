// The playing loop (INSTANT_PREVIEW_SPEC §3.5, §4.3, §11.2): one rVFC per
// presented frame (display time moved onto the performance.now() timeline:
// WebKit's MSE <video> reports it on another clock), its handler time kept
// for the §11.2 budget, and a top-up tick for the laneA window and the
// external-pause watchdog.

import { DisplayTimeBase } from './clock/presentedClock'

/** What the presented-frame handler reads from rVFC (display time already
 *  on the performance.now() timeline). */
export interface PresentedMeta { mediaTime: number; expectedDisplayTime: number; width: number; height: number }

/** Handler times kept (ms). */
const HANDLER_SAMPLES = 2048

export class FrameLoop {
  private gen = 0
  private handle = 0
  private tick: ReturnType<typeof setInterval> | null = null
  private video: HTMLVideoElement | null = null
  private readonly base = new DisplayTimeBase()
  private readonly handlerMs: number[]
  private readonly onFrame: (meta: PresentedMeta) => void
  private readonly onTick: () => void
  private readonly tickMs: number

  /** `handlerMs`: the engine's stats array, filled here. */
  constructor(handlerMs: number[], onFrame: (meta: PresentedMeta) => void, onTick: () => void, tickMs: number) {
    this.handlerMs = handlerMs
    this.onFrame = onFrame
    this.onTick = onTick
    this.tickMs = tickMs
  }

  start(video: HTMLVideoElement): void {
    this.stop()
    this.video = video
    this.base.reset()
    const gen = ++this.gen
    const cb: VideoFrameRequestCallback = (cbNow, meta) => {
      if (gen !== this.gen) return
      const t0 = performance.now()
      this.onFrame({ mediaTime: meta.mediaTime, width: meta.width, height: meta.height,
        expectedDisplayTime: this.base.fix(cbNow, meta.expectedDisplayTime) })
      const h = this.handlerMs
      h.push(performance.now() - t0)
      if (h.length > HANDLER_SAMPLES) h.shift()
      if (gen === this.gen) this.handle = video.requestVideoFrameCallback(cb)
    }
    this.handle = video.requestVideoFrameCallback(cb)
    this.tick = setInterval(() => { if (gen === this.gen) this.onTick() }, this.tickMs)
  }

  stop(): void {
    this.gen++
    if (this.video && this.handle) this.video.cancelVideoFrameCallback?.(this.handle)
    this.handle = 0
    if (this.tick) clearInterval(this.tick)
    this.tick = null
  }
}
