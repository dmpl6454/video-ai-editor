// Reading the ENGINE CANVAS in tests (inside the compositor's onDrawn hook,
// the only task in which the drawing buffer is readable): the frame-identity
// bar of the drawn frame (testkit/barcode: source id + source frame), and
// the mean green level of a canvas rectangle (graded vs raw, flash vs dark).
// Geometry maps the bar's source cells to canvas pixels, so any fit/scale
// the program applies is read correctly.

import { KIND_GAP, type ProgramMap } from '../timeline/programMap'
import type { SourceInfo } from '../timeline/frameMap'
import { canvasPointOf, frameGeometry, type Size } from '../render/geometry'
import { BAR_BITS, BAR_CELL, NO_PICTURE, barCode, barSrc } from './barcode'

/** The bar the program says frame k shows (NO_PICTURE for a gap). */
export function expectedBar(pm: ProgramMap, k: number, srcIds: ReadonlyMap<string, number>): number {
  if (k < 0 || k >= pm.total || pm.kind[k] === KIND_GAP) return NO_PICTURE
  return barCode(srcIds.get(pm.sources[pm.srcKey[k]]) ?? 0, pm.srcFrame[k])
}

/** The bar on the drawing buffer for frame k of `pm`, drawn at EDL canvas
 *  size `canvasEdl`. */
export function readBar(gl: WebGL2RenderingContext, pm: ProgramMap, k: number, canvasEdl: Size,
  infoOf: (src: string) => SourceInfo | null): number {
  const canvas = gl.canvas as HTMLCanvasElement
  if (k < 0 || k >= pm.total || pm.kind[k] === KIND_GAP) return NO_PICTURE
  const fg = frameGeometry(pm, k, canvasEdl, infoOf)
  if (!fg) return -3
  const info = infoOf(fg.clip.src)
  const w = info?.w ?? canvasEdl.w
  const h = info?.h ?? canvasEdl.h
  const sx = canvas.width / canvasEdl.w
  const sy = canvas.height / canvasEdl.h
  const px = new Uint8Array(4)
  let code = 0
  for (let bit = 0; bit < BAR_BITS; bit++) {
    const at = canvasPointOf(fg.geometry, (bit * BAR_CELL + BAR_CELL / 2) / w, 20 / h)
    if (!at) return -4
    gl.readPixels(Math.floor(at[0] * sx), canvas.height - 1 - Math.floor(at[1] * sy), 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, px)
    if (px[1] > 128) code |= 1 << bit
  }
  return barSrc(code) === 0 ? NO_PICTURE : code
}

/** Mean green (0-255) of a rectangle given in EDL canvas px. */
export function meanGreen(gl: WebGL2RenderingContext, canvasEdl: Size, r: { x: number; y: number; w: number; h: number }): number {
  const canvas = gl.canvas as HTMLCanvasElement
  const sx = canvas.width / canvasEdl.w
  const sy = canvas.height / canvasEdl.h
  const x = Math.floor(r.x * sx)
  const w = Math.max(1, Math.floor(r.w * sx))
  const h = Math.max(1, Math.floor(r.h * sy))
  const y = canvas.height - Math.floor(r.y * sy) - h
  const buf = new Uint8Array(w * h * 4)
  gl.readPixels(x, y, w, h, gl.RGBA, gl.UNSIGNED_BYTE, buf)
  let sum = 0
  for (let i = 1; i < buf.length; i += 4) sum += buf[i]
  return sum / (w * h)
}

export function percentile(xs: number[], p: number): number {
  if (!xs.length) return 0
  const s = [...xs].sort((a, b) => a - b)
  return s[Math.min(s.length - 1, Math.floor(p * (s.length - 1) + 0.5))]
}
