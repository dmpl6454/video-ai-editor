// One output frame onto the engine canvas (INSTANT_PREVIEW_SPEC §3.4, §7):
// black for a gap, the server's composited frame over the whole canvas for a
// landed bake frame, else the clip's source frame through its geometry. And
// the spinner's 80 ms delay (§11.1: a frame that lands sooner shows none).

import type { ProgramFeed } from './engineFeed'
import { fullFrameGeometry } from './render/bakeGeometry'
import type { Compositor } from './render/compositor'
import { frameGeometry, type Size } from './render/geometry'
import { KIND_GAP, type ProgramMap } from './timeline/programMap'

/** Draws output frame k from the current texture (the caller checked it
 *  holds want[k]). False when nothing could be drawn. */
export function drawProgramFrame(comp: Compositor, pm: ProgramMap, feed: ProgramFeed, canvas: Size, k: number): boolean {
  if (pm.kind[k] === KIND_GAP) return comp.draw(null, k)
  const h = feed.handleAt(k)
  if (feed.isBaked(k)) {
    // the server's composited frame: the whole canvas, no clip pass
    const tex = h ? { w: h.index.w, h: h.index.h } : canvas
    return comp.draw({ geometry: fullFrameGeometry(canvas, tex), canvas, texture: tex }, k)
  }
  const fg = frameGeometry(pm, k, canvas, (src) => feed.sourceInfo(src))
  if (!fg) return false
  return comp.draw({ geometry: fg.geometry, canvas, texture: h ? { w: h.index.w, h: h.index.h } : undefined }, k)
}

/** Uploads the element's current frame as output frame k's texture. The
 *  size comes from the program's source (the proxy index), never from the
 *  element: rVFC metadata lagged a frame after an init switch (milestone 1). */
export function uploadProgramFrame(comp: Compositor, video: HTMLVideoElement, feed: ProgramFeed, k: number): boolean {
  const h = feed.handleAt(k)
  return comp.upload(video, feed.want[k], h ? { w: h.index.w, h: h.index.h } : { w: video.videoWidth, h: video.videoHeight })
}

/** A flag raised only if still wanted `delayMs` after it was asked for. */
export class DelayedFlag {
  on = false
  private timer: ReturnType<typeof setTimeout> | null = null
  private readonly delayMs: number
  private readonly changed: () => void

  constructor(delayMs: number, changed: () => void) {
    this.delayMs = delayMs
    this.changed = changed
  }

  /** Raise after the delay if `stillWanted()`; lower at once. */
  set(on: boolean, stillWanted: () => boolean = () => true): void {
    this.cancel()
    if (!on) {
      if (this.on) {
        this.on = false
        this.changed()
      }
      return
    }
    if (this.on) return
    this.timer = setTimeout(() => {
      this.timer = null
      if (!stillWanted()) return
      this.on = true
      this.changed()
    }, this.delayMs)
  }

  cancel(): void {
    if (this.timer) clearTimeout(this.timer)
    this.timer = null
  }
}
