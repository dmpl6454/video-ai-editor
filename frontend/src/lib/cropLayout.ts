// Layout math for the crop/reposition view, kept OUT of the component so the
// sign conventions and the clamping are unit-testable.
//
// Every value here has to agree with render/compositor.py's cover-with-pan
// chain, which is the thing that actually bakes the picture:
//
//   scale=<canvas>:force_original_aspect_ratio=increase   // cover
//   scale=iw*max(1, scale)                                // optional zoom
//   crop=<canvas>:'(in_w-out_w)/2 - x':'(in_h-out_h)/2 - y'
//
// Moving the crop window LEFT by x is moving the picture RIGHT by x, so
// **positive x moves the picture right** — and this view must do the same, or
// the drag preview and the export disagree.

export interface CropLayoutInput {
  canvasW: number
  canvasH: number
  srcW: number
  srcH: number
  paneW: number
  paneH: number
  scale: number
  x: number
  y: number
  /** Fraction of the pane the crop window may occupy. <1 keeps the croppable
   *  footage around the window on screen. */
  windowFill?: number
}

export interface CropLayoutBoxes {
  /** Screen px per canvas px. Independent of `scale` on purpose. */
  winScale: number
  /** The crop window (the output frame). Fixed: never moves, never resizes. */
  win: { left: number; top: number; w: number; h: number }
  /** The source picture. This is what `scale` grows and what x/y moves. */
  pic: { left: number; top: number; w: number; h: number }
  /** x/y after clamping to the pan the renderer will actually honour. Commit
   *  THESE, not the raw pointer values. */
  clamped: { x: number; y: number }
  /** Max |x| / |y| in canvas px. 0 means that axis cannot pan at all. */
  limit: { x: number; y: number }
}

/** The zoom the RENDERER will apply, which is not always the stored `scale`.
 *
 * compositor.py's cover-with-pan branch uses `extra_zoom = max(1.0, scale)`:
 * a sub-1 zoom would shrink the frame below the canvas and `crop` would then
 * render solid black. The no-pan path has no such clamp (it pads instead), so
 * mirroring the renderer means mirroring that split — otherwise a clip stored
 * with scale<1 (reachable from the Properties slider, which allows 0.1, and
 * from Claude/MCP) draws here at half size, showing black inside the yellow
 * frame that the export will never contain.
 */
function effectiveScale(scale: number, x: number, y: number): number {
  const panning = x !== 0 || y !== 0
  return panning ? Math.max(1, scale) : scale
}

export function cropLayout(i: CropLayoutInput): CropLayoutBoxes {
  const fill = i.windowFill ?? 1
  const safe = (n: number) => (Number.isFinite(n) && n > 0 ? n : 0)
  const srcW = safe(i.srcW)
  const srcH = safe(i.srcH)
  const paneW = safe(i.paneW)
  const paneH = safe(i.paneH)
  const canvasW = safe(i.canvasW)
  const canvasH = safe(i.canvasH)
  if (!srcW || !srcH || !paneW || !paneH || !canvasW || !canvasH) {
    const zero = { left: 0, top: 0, w: 0, h: 0 }
    return { winScale: 0, win: zero, pic: zero, clamped: { x: 0, y: 0 }, limit: { x: 0, y: 0 } }
  }

  // Source size in CANVAS px, exactly as the renderer computes it.
  const cover = Math.max(canvasW / srcW, canvasH / srcH)
  const baseW = srcW * cover
  const baseH = srcH * cover
  const zoom = effectiveScale(i.scale, i.x, i.y)

  // Derived from the scale-1 cover size, NOT the current scale: the output
  // frame must not resize when you zoom the footage inside it.
  const winScale = Math.min(
    paneW / baseW, paneH / baseH,
    (paneW / canvasW) * fill, (paneH / canvasH) * fill,
  )
  const cx = paneW / 2
  const cy = paneH / 2

  const winW = canvasW * winScale
  const winH = canvasH * winScale
  const picW = baseW * zoom * winScale
  const picH = baseH * zoom * winScale

  // How far the picture can actually slide before the window leaves it.
  //
  // ffmpeg's `crop` clamps its x/y into [0, in_w-out_w] (compositor.py says so
  // explicitly, and it is what makes panning past the edge hold on the last
  // real pixel instead of exposing black). So beyond this margin the bake
  // simply ignores the extra: the view used to keep sliding the picture and
  // painting black inside the yellow frame while the export never moved.
  // Worst case is a source whose aspect EQUALS the canvas — the most common
  // upload, a 9:16 phone clip on a 9:16 canvas — where BOTH margins are 0 at
  // scale 1 and every drag was a total no-op in the render. Measured: on a
  // 1920x1080 source over a 1080x1920 canvas, y=400 and y=-978 both rendered
  // byte-identically to y=0.
  const limitX = Math.max(0, (picW - winW) / 2 / winScale)
  const limitY = Math.max(0, (picH - winH) / 2 / winScale)
  const clampedX = Math.max(-limitX, Math.min(limitX, i.x))
  const clampedY = Math.max(-limitY, Math.min(limitY, i.y))

  return {
    winScale,
    win: { left: cx - winW / 2, top: cy - winH / 2, w: winW, h: winH },
    pic: {
      left: cx - picW / 2 + clampedX * winScale,
      top: cy - picH / 2 + clampedY * winScale,
      w: picW, h: picH,
    },
    clamped: { x: clampedX, y: clampedY },
    limit: { x: limitX, y: limitY },
  }
}

/** Screen-px pointer delta -> canvas-px transform offset. Positive both ways:
 *  the picture follows the pointer. Clamping is cropLayout's job — feed the
 *  result back through it and commit `clamped`. */
export function dragToOffset(
  start: { x: number; y: number },
  deltaPx: { dx: number; dy: number },
  winScale: number,
): { x: number; y: number } {
  if (!winScale) return { x: start.x, y: start.y }
  return {
    x: start.x + deltaPx.dx / winScale,
    y: start.y + deltaPx.dy / winScale,
  }
}
