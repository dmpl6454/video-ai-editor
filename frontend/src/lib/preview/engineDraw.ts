// One output frame onto the engine canvas (INSTANT_PREVIEW_SPEC §3.4, §7):
// black for a gap, the server's composited frame over the whole canvas for a
// landed bake frame, else the clip's source frame through its geometry. And
// the spinner's 80 ms delay (§11.1: a frame that lands sooner shows none).

import type { ProgramFeed } from './engineFeed'
import { fullFrameGeometry } from './render/bakeGeometry'
import type { Compositor } from './render/compositor'
import { frameGeometry, type Size } from './render/geometry'
import { canvasBgDraw } from './render/canvasBg'
import { canvasBgImage } from './render/canvasBgImages'
import type { EdlClip } from './timeline/framePlan'
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
  // the clip's CapCut Canvas background in its letterbox (wave E, F2)
  const info = feed.sourceInfo(fg.clip.src)
  const background = canvasBgDraw(fg.clip, canvas, info ? { w: info.w, h: info.h } : canvas, comp.canvasBgBaseUrl)
  return comp.draw({ geometry: fg.geometry, canvas, texture: h ? { w: h.index.w, h: h.index.h } : undefined, background }, k)
}

/** Starts fetching the pictures of the program's image canvas backgrounds
 *  (wave E, F2), so the first frame that shows one rarely waits; `ready`
 *  redraws a paused frame when one lands. */
export function preloadCanvasBackgrounds(clips: readonly EdlClip[], canvas: Size, baseUrl: string | undefined,
  ready: () => void): void {
  if (!baseUrl) return
  for (const c of clips) {
    const d = canvasBgDraw(c, canvas, canvas, baseUrl)
    if (d?.mode === 'image') canvasBgImage(d.url, ready)
  }
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
