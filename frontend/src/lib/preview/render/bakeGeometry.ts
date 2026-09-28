// Geometry of a BAKE frame (instant preview spec §5.3, §7): the bake of a
// render hash is the server's composited picture of output frame k (every
// clip's geometry, colour and effects already applied; text, stickers and
// PiP excluded), so the compositor draws it as the whole canvas — texture uv
// (0..1) stretched over canvas px (0..W, 0..H) — with no clip pass on top.

import { IDENTITY, type ClipGeometry, type Size } from './geometry'

export function fullFrameGeometry(canvas: Size, texture: Size): ClipGeometry {
  const W = Math.max(1, canvas.w)
  const H = Math.max(1, canvas.h)
  const frame = { x0: 0, y0: 0, x1: W, y1: H }
  return {
    toF2: IDENTITY, f2Bounds: frame,
    toF1: IDENTITY, f1Bounds: frame, f1Clamp: false,
    toUv: { a: 1 / W, b: 0, c: 0, d: 1 / H, e: 0, f: 0 }, uvBounds: { x0: 0, y0: 0, x1: 1, y1: 1 },
    gain: 1, alpha: 1, fade: 1,
    minification: Math.max(texture.w / W, texture.h / H),
  }
}
