// Drawing a clip animation on a 2-D canvas (wave E, F1): overlays (pipDraw /
// StickerLayer) in both preview modes — the export bakes the same stages in
// render/pip.py and render/text_overlay.py, in the same order:
//
//   travel (+dx, +dy of the canvas) → turn (+rotation) → zoom (× scale) →
//   opacity (× the fade ramps) → blur (a blurred copy mixed in LINEARLY with
//   weight w, alpha included — ffmpeg's `blend` A + (B − A)·w)
//
// The linear mix is done on an offscreen canvas in premultiplied space: the
// sharp draw at alpha (1 − w), the blurred one ADDED at alpha w ('lighter'),
// then the result composited normally — so what was already under the
// element is covered by exactly the mix's own alpha (a plain second draw
// over the first would darken the picture by w·(1 − w)).

import { blurSigma, type AnimPose } from './clipAnim'

/** Canvas 2-D `filter` (blur()) support: Blink, and WebKit since Safari 18.
 *  Without it the blur is approximated by a down-and-up resample. */
export function canvasFilterSupported(ctx: CanvasRenderingContext2D): boolean {
  return typeof (ctx as unknown as { filter?: unknown }).filter === 'string'
}

let OFF: HTMLCanvasElement | null = null
let SMALL: HTMLCanvasElement | null = null

function offscreen(w: number, h: number, which: 'off' | 'small'): HTMLCanvasElement {
  let c = which === 'off' ? OFF : SMALL
  if (!c) {
    c = document.createElement('canvas')
    if (which === 'off') OFF = c
    else SMALL = c
  }
  if (c.width !== w || c.height !== h) { c.width = w; c.height = h }
  return c
}

/** Apply the pose's travel, turn and zoom to `ctx` (already translated to the
 *  element's centre and turned by its own rotation), and fold its opacity
 *  into globalAlpha. `canvasPx` converts a share of the canvas to the
 *  context's units: (canvas W × display scale, canvas H × display scale). */
export function applyPose(ctx: CanvasRenderingContext2D, pose: AnimPose, ownRotRad: number,
  canvasPx: { w: number; h: number }): void {
  // travel happens in canvas axes (before the element's own turn)
  ctx.rotate(-ownRotRad)
  ctx.translate(pose.dx * canvasPx.w, pose.dy * canvasPx.h)
  ctx.rotate(ownRotRad + (pose.rotation * Math.PI) / 180)
  if (pose.scale !== 1) ctx.scale(pose.scale, pose.scale)
  ctx.globalAlpha *= pose.alpha
}

/**
 * Draw an element with the pose's blur mix. `draw` paints the element on the
 * context it is given, in the CURRENT transform (clip paths included), and
 * must be repeatable. `displayMin` is the smaller side of the display area in
 * CSS px, which sets the sigma exactly as the export's `blur_sigma` does from
 * its output size.
 */
export function drawWithBlur(ctx: CanvasRenderingContext2D, pose: AnimPose, displayMin: number,
  draw: (c: CanvasRenderingContext2D) => void): void {
  const w = pose.blur
  if (!(w > 0.001)) { draw(ctx); return }
  const m = ctx.getTransform()
  const cv = ctx.canvas as HTMLCanvasElement
  const off = offscreen(cv.width, cv.height, 'off')
  const o = off.getContext('2d')
  if (!o) { draw(ctx); return }
  const alpha = ctx.globalAlpha
  o.setTransform(1, 0, 0, 1, 0, 0)
  o.globalCompositeOperation = 'source-over'
  o.globalAlpha = 1
  o.clearRect(0, 0, off.width, off.height)
  o.setTransform(m)
  o.globalAlpha = alpha * (1 - w)
  draw(o)
  // sigma in backing-store px: the export's sigma is 2 % of the output's
  // shorter side, the display's here, times the device-pixel ratio
  const devScale = cv.width / Math.max(1, (cv as HTMLCanvasElement).clientWidth || cv.width)
  const sigma = blurSigma(displayMin, displayMin) * (devScale || 1)
  o.globalCompositeOperation = 'lighter'
  o.globalAlpha = alpha * w
  if (canvasFilterSupported(o)) {
    ;(o as unknown as { filter: string }).filter = `blur(${sigma.toFixed(2)}px)`
    draw(o)
    ;(o as unknown as { filter: string }).filter = 'none'
  } else {
    // down-and-up: a box of ~2 sigma per small pixel, smoothed back up
    const f = Math.max(1, sigma / 2)
    const sw = Math.max(1, Math.round(off.width / f))
    const sh = Math.max(1, Math.round(off.height / f))
    const small = offscreen(sw, sh, 'small')
    const s = small.getContext('2d')
    if (s) {
      s.setTransform(1, 0, 0, 1, 0, 0)
      s.clearRect(0, 0, sw, sh)
      s.imageSmoothingEnabled = true
      s.setTransform(m.a / f, m.b / f, m.c / f, m.d / f, m.e / f, m.f / f)
      s.globalAlpha = 1
      draw(s)
      o.setTransform(1, 0, 0, 1, 0, 0)
      o.imageSmoothingEnabled = true
      ;(o as unknown as { imageSmoothingQuality?: string }).imageSmoothingQuality = 'high'
      o.drawImage(small, 0, 0, off.width, off.height)
    }
  }
  o.globalCompositeOperation = 'source-over'
  ctx.save()
  ctx.setTransform(1, 0, 0, 1, 0, 0)
  ctx.globalAlpha = 1
  ctx.drawImage(off, 0, 0)
  ctx.restore()
}
