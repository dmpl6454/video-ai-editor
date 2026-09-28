// Inspector > Framing's status line for a main-track clip: where the bars
// actually are. It used to be a constant ("Letterboxed — bars top/bottom.")
// keyed on `fit` alone, so a vertical clip in a 16:9 frame — bars left and
// right, which Canvas > Blur fills — and a 16:9 clip in a 16:9 frame with no
// bars at all were both described as letterboxed.
//
// Contain-fit geometry, the renderer's: the clip is scaled to fit inside the
// canvas keeping its aspect, then by the clip's own `scale`. A side is
// "barred" when the picture falls more than a pixel short of it.

export interface Size { w: number; h: number }

const PIXEL = 1

export function framingNote(fitCover: boolean, src: Size | null, canvas: Size, scale = 1): string {
  if (fitCover) return 'Filling the frame (cropped).'
  if (!src || !(src.w > 0 && src.h > 0) || !(canvas.w > 0 && canvas.h > 0)) return 'Fitted inside the frame.'
  const fit = Math.min(canvas.w / src.w, canvas.h / src.h) * (scale > 0 ? scale : 1)
  const sideBars = src.w * fit < canvas.w - PIXEL
  const topBars = src.h * fit < canvas.h - PIXEL
  if (sideBars && topBars) return 'Smaller than the frame — bars all round.'
  if (sideBars) return 'Pillarboxed — bars left/right.'
  if (topBars) return 'Letterboxed — bars top/bottom.'
  return 'Fills the frame — no bars.'
}
