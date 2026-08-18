// Live transform preview drawn from the RAW SOURCE instead of the composited
// frame.
//
// WHY THIS EXISTS. Rotation on v1 is applied IN PLACE by the renderer
// (`rotate=<rad>:c=black`, no output-size expansion — compositor.py explains
// why), so the corners that swing outside the canvas are CUT and replaced with
// black. Measured on a 960x540 preview: at 61° only 72.5% of the picture
// survives, and the two corners that were white at 0° read (0,0,0) at 61°.
//
// The live preview used to apply CSS to that already-rotated frame. The ANGLE
// it produced was right — dragging back to 0° emits rotate(0-61) and the
// picture does come upright — but the 27.5% the bake threw away cannot be
// restored, so the black wedges rotate into view. Reported as "first i rotated
// the video 61 degree and then pulled it back to the 0 degree, the video got
// cropped from the rotation… when i leave it, it worked fine, but this should
// be also working when i hold with my cursor".
//
// No CSS on that frame can fix it: the pixels are gone. A truthful preview has
// to start from pixels that are not yet rotated, i.e. the source file. Drawing
// the source at the TARGET angle reproduces the bake exactly — including its
// corner cutting, which is correct and expected — instead of counter-rotating
// a frame whose corners are already missing.
//
// SCOPE, deliberately narrow. Only for a v1 media clip that is `contain`
// (letterboxed) with a scalar transform, and only while the clip ALREADY has a
// non-zero baked rotation — which is exactly the case that goes wrong. A clip
// at rotation 0 keeps the fully-composited preview, which is both correct today
// and higher fidelity (it carries the colour grade and every other layer).
// `cover` framing is owned by <CropReposition>, which solves the same
// missing-pixels problem the same way and must not be duplicated here.

export interface SourceDrawPlan {
  /** Fitted source size in BOX pixels, before rotation/zoom. */
  drawW: number
  drawH: number
  /** Post-rotation zoom. Mirrors the renderer's `max(1, scale)` clamp. */
  zoom: number
  rotRad: number
  /** Output-space pan in BOX pixels. */
  panX: number
  panY: number
}

/**
 * Reproduce the v1 chain — fit(contain) → rotate → zoom+pan — as canvas draw
 * parameters.
 *
 * The order is the renderer's, not a convenient one: rotation happens on the
 * FITTED canvas image, and the scale/pan crop-zoom happens after it. Applying
 * them the other way round puts the pan in rotated space and the picture slides
 * diagonally when you drag x.
 *
 * `+x` moves the picture RIGHT, matching compositor.py's
 * `crop=…:'(in_w-out_w)/2 - x'` — moving a crop window left by x IS moving the
 * picture right by x. Same sign rule cropLayout.ts documents; keeping the two
 * in agreement is what makes the live drag and the bake land together.
 */
export function planSourceDraw(
  src: { w: number; h: number },
  canvas: { w: number; h: number },
  box: { w: number; h: number },
  tx: { scale?: number; rotation?: number; x?: number; y?: number },
): SourceDrawPlan | null {
  if (!(src.w > 0 && src.h > 0 && canvas.w > 0 && canvas.h > 0 && box.w > 0 && box.h > 0)) {
    return null
  }
  // EDL-canvas pixels -> on-screen box pixels.
  const k = box.w / canvas.w
  // `contain`: scale to fit, preserving aspect, then pad — ffmpeg's
  // force_original_aspect_ratio=decrease + pad.
  const fit = Math.min(canvas.w / src.w, canvas.h / src.h)
  const scale = tx.scale ?? 1
  return {
    drawW: src.w * fit * k,
    drawH: src.h * fit * k,
    // The renderer clamps to >=1 because a sub-1 zoom leaves `crop` less input
    // than output and renders solid black. Mirrored so the preview cannot show
    // a picture the bake will never produce (the same contract SCALE_MIN pins
    // for the framing view).
    zoom: Math.max(1, Number.isFinite(scale) ? scale : 1),
    rotRad: ((tx.rotation ?? 0) * Math.PI) / 180,
    panX: (tx.x ?? 0) * k,
    panY: (tx.y ?? 0) * k,
  }
}

/** Is this clip one the source-based preview may take over? */
export function sourcePreviewApplies(opts: {
  trackId: string | undefined
  fit: string | undefined
  bakedRotation: number
  keyframed: boolean
  hasDims: boolean
}): boolean {
  // A non-zero BAKED rotation is the whole trigger: that is when the frame on
  // screen has already lost its corners, and only then is the composited
  // preview unable to tell the truth.
  return opts.trackId === 'v1'
    && opts.fit !== 'cover'
    && !opts.keyframed
    && opts.hasDims
    && Math.abs(opts.bakedRotation) > 0.001
}
