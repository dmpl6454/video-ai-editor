// Client-side painting of PIP (v2+) clips.
//
// The preview render deliberately does NOT bake a PIP's picture (see the
// `preview` branch in render/pip.py) so that dragging, resizing and reframing
// move real frames under the pointer. That makes THIS file the preview's PIP
// renderer, and it must agree with pip.py pixel for pixel — export has no
// client, so any divergence shows up as "the preview lied" the moment you
// export. The same contract TextLayer holds against text_overlay.py.
//
// What pip.py does, in order, and therefore what this mirrors:
//   1. box     — element size: square for a circle shape, canvas aspect under
//                fit='cover', else the source's own aspect. Long edge is
//                canvas_long * 0.35 * transform.scale.
//   2. framing — scale to COVER box*zoom, then crop the box out of it at a
//                normalised x/y offset (clamped to the available margin).
//   3. shape   — circle / rounded alpha cut.
//   4. rotate / opacity.
//   5. overlay — centred on transform.x/y in canvas pixels.

export interface PipFraming { x?: number; y?: number; zoom?: number; rotation?: number }

/**
 * Whether THIS PIP's picture is the client's to draw, or still ffmpeg's to bake.
 *
 * Every picture stage pip.py applies has a canvas equivalent — box crop, framing
 * zoom/pan, circle/rounded mask, rotation, opacity — with ONE exception: a
 * chroma key is per-pixel work that a 2D canvas cannot do at 60 Hz. So a
 * chromakey'd PIP keeps being baked, and gives up real-time dragging to stay
 * visually TRUE; everything else gets direct manipulation.
 *
 * Reachable, not theoretical: `chroma_key` is a tool, and `remove_background`
 * sets a key automatically — "cut the background out, then PIP it" is the
 * canonical green-screen workflow. Without the carve-out the preview showed
 * such a clip unkeyed (background and all) while the export keyed it.
 *
 * THE SAME PREDICATE MUST HOLD IN THREE PLACES or the split breaks in one of
 * two visible ways:
 *   * pip.py's `preview` branch — bakes it or doesn't;
 *   * StickerLayer's paint loop — draws it or doesn't. Disagree one way and the
 *     PIP is drawn twice (a baked copy the client cannot erase, plus a live one);
 *     the other way and it is drawn not at all;
 *   * Preview.tsx's videoFingerprint — a baked PIP's placement/size/shape MUST
 *     still trigger a re-render, since nothing else will show the change.
 * pip.py is the authority; this mirrors it.
 */
export function pipIsClientDrawn(clip: { chromakey?: unknown | null }): boolean {
  return clip.chromakey == null
}

/** Live framing offsets during an alt-drag, before the commit round-trips.
 *
 *  Module-level for the same reason as the drag override in lib/overlay: the
 *  painter runs in a rAF loop, so routing a 60 Hz gesture through setState
 *  would re-run the whole draw effect every frame. Held past pointer-up until
 *  the refreshed EDL carries the value, so the picture does not snap back to its
 *  old framing for the commit round-trip.
 */
let LIVE_FRAMING: { id: string; x: number; y: number; rotation?: number } | null = null

export function setLivePipFraming(
  v: { id: string; x: number; y: number; rotation?: number } | null,
): void {
  LIVE_FRAMING = v
}

export function livePipFraming(
  id: string,
): { x: number; y: number; rotation?: number } | null {
  return LIVE_FRAMING && LIVE_FRAMING.id === id
    ? { x: LIVE_FRAMING.x, y: LIVE_FRAMING.y, rotation: LIVE_FRAMING.rotation }
    : null
}

/** A hidden <video> per source, seeked on demand and reused across frames.
 *
 *  Kept module-level rather than in React state for the same reason the drag
 *  override is: this runs inside a rAF loop, and creating or re-rendering per
 *  frame would be far more expensive than the draw itself.
 */
const VIDEOS = new Map<string, HTMLVideoElement>()

/** Offscreen host for the elements above. They must be IN THE DOCUMENT.
 *
 *  A detached `<video>` is enough to *seek* and `drawImage` from — which is all
 *  this file used to do, and why the elements were never attached. It is NOT
 *  reliably enough to PLAY, and playing is what the flicker fix changed them to
 *  do. Blink runs a detached element happily; WebKit is the engine that does
 *  not promise to, and WKWebView is what the packaged macOS app renders in — so
 *  this is a defect that could not appear on the machine it was written on and
 *  would land as "the PIP freezes on one frame while the video plays" on a Mac.
 *
 *  NOT `display:none`, and that distinction is the whole point: a display-less
 *  element is outside layout and an engine is free to stop feeding its decoder,
 *  which puts us back where we started. The host stays laid out and simply has
 *  nothing visible to contribute — 1x1, transparent, off the click path, and
 *  `aria-hidden` so it is not announced.
 *
 *  (FrameScrubber's fallback `<video>` IS `display:none`, correctly: it is only
 *  ever seeked, never played.)
 */
let VIDEO_HOST: HTMLElement | null = null

// No `typeof document` guard here: `pipVideo` below cannot create an element
// without a document either, so a guard in this one function would promise a
// safety the caller does not have. Both require a document, honestly.
function videoHost(): HTMLElement {
  if (VIDEO_HOST?.isConnected) return VIDEO_HOST
  const el = document.createElement('div')
  el.setAttribute('aria-hidden', 'true')
  el.dataset.pipVideoHost = ''
  el.style.cssText =
    'position:fixed;left:0;top:0;width:1px;height:1px;overflow:hidden;' +
    'opacity:0;pointer-events:none;z-index:-1'
  document.body.appendChild(el)
  VIDEO_HOST = el
  return el
}

/** Elements for the PREVIEW's source-based transform view.
 *
 *  A SEPARATE pool from `VIDEOS`, and that separation is the whole point.
 *  `pipVideo` keys on `src` alone, so the moment the same file sits on v1 and on
 *  a PIP lane, StickerLayer's paint loop and Preview's source-preview loop get
 *  the SAME element — and each calls `syncPipVideo` on it every frame with a
 *  DIFFERENT clip trim. Measured on the reported session (v1 at in=0, a circle
 *  PIP of the same file at in=4.0125 on a 4.01s source): each frame both loops
 *  reassign `currentTime` to a different instant, the seeks never settle,
 *  `readyState` never reaches HAVE_CURRENT_DATA, and the source canvas paints
 *  only its black fill — the main video goes black for the whole rotation drag
 *  while the PIP keeps drawing, because drawPipVideoFrame has a LAST_FRAME
 *  fallback and this path did not. Releasing the pointer fixes it because the
 *  source preview unmounts and the PIP regains sole ownership.
 *
 *  It also matters that these are NOT reachable from `pausePipVideosExcept`:
 *  that helper pauses every element it owns that is not a currently-active PIP,
 *  which would include a v1 source that is not a PIP at all.
 *
 *  Two elements for one file costs a second decoder on exactly one clip during
 *  one gesture — cheap, and the alternative is two owners of one clock.
 */
const SOURCE_VIDEOS = new Map<string, HTMLVideoElement>()

export function sourcePreviewVideo(src: string, url: string): HTMLVideoElement {
  let v = SOURCE_VIDEOS.get(src)
  if (!v) {
    v = document.createElement('video')
    v.preload = 'auto'
    v.muted = true
    v.playsInline = true
    v.src = url
    SOURCE_VIDEOS.set(src, v)
  }
  // ONE element in this pool at a time. Only one clip can be dragged, so a
  // second entry is always a leftover from a previous gesture.
  //
  // This is a WebKit budget, not tidiness. Blink will happily hold dozens of
  // media elements; WebKit enforces a much smaller concurrent limit and starts
  // refusing to load new ones — and the packaged macOS app renders in WKWebView,
  // where the symptom would be the source preview silently never becoming ready
  // (readyState stuck below 2) after enough clips had been dragged in a session.
  // The `srcDrawnRef` guard in Preview.tsx means that degrades to the CSS
  // stand-in rather than to black, but degrading at all is avoidable here.
  // The PIP pool is bounded by how many PIPs the timeline actually has; this one
  // would otherwise grow with every clip the user ever dragged.
  for (const [k, el] of SOURCE_VIDEOS) {
    if (k === src) continue
    // `load()` after clearing src is what actually frees the decoder rather than
    // just dropping our reference — but on a source-less element WebKit fires an
    // error event and some engines log, so the whole teardown is guarded. A
    // failure to release must never propagate into a draw loop.
    try { el.pause(); el.removeAttribute('src'); el.load() } catch { /* already gone */ }
    el.remove()
    SOURCE_VIDEOS.delete(k)
  }
  if (!v.isConnected) videoHost().appendChild(v)
  return v
}

/** Drop the source-preview element entirely (end of a gesture).
 *
 *  Called from Preview.tsx's effect cleanup. Keeping it alive between gestures
 *  would buy a slightly faster second drag at the cost of holding a decoder open
 *  for a view that is only ever on screen while a pointer is down.
 */
export function releaseSourcePreviewVideos(): void {
  for (const [k, el] of SOURCE_VIDEOS) {
    try { el.pause() } catch { /* not started */ }
    el.removeAttribute('src')
    el.load()
    el.remove()
    SOURCE_VIDEOS.delete(k)
  }
}

export function pipVideo(src: string, url: string): HTMLVideoElement {
  let v = VIDEOS.get(src)
  if (!v) {
    v = document.createElement('video')
    v.preload = 'auto'
    v.muted = true          // audio comes from the render's mix, never from here
    v.playsInline = true
    v.src = url
    VIDEOS.set(src, v)
  }
  // Re-parent on every call rather than only at creation: cheap when it is
  // already there (appendChild of a current child is a no-op move), and it
  // self-heals if the host is ever torn out from under us.
  if (!v.isConnected) videoHost().appendChild(v)
  return v
}

/** Drift we tolerate while PLAYING before forcing a corrective seek. */
const PIP_RESYNC_TOL = 0.25

/** Put the hidden element on the source time a PIP shows at timeline time `t`.
 *
 *  `in_` is the clip's trim start, so source time is `in_ + (t - start)`.
 *
 *  PAUSED (scrubbing) → seek, and only when the gap is worth one: a seek per
 *  frame would thrash the decoder and never settle, and the eye cannot see a
 *  sub-frame difference anyway.
 *
 *  PLAYING → PLAY the element and let it run, correcting only real drift.
 *  Seeking during playback is what made the PIP "keep flickering when the video
 *  is played and sometimes get vanished": the playhead advances ~1/fps per
 *  frame while the tolerance is max(1/fps, 0.03), so EVERY frame re-assigned
 *  `currentTime`. Each assignment starts an asynchronous seek, `readyState`
 *  dips below HAVE_CURRENT_DATA while it runs, and the draw loop then painted
 *  its no-frame placeholder instead — 30 seeks a second that never settle, so
 *  the placeholder is on screen as often as the picture. A decoder asked to
 *  play is exactly the case it is built for; a decoder asked to seek 30 times a
 *  second is not.
 */
export function syncPipVideo(
  v: HTMLVideoElement, t: number, start: number, inPoint: number, fps: number,
  opts?: { playing?: boolean; rate?: number },
): void {
  if (!Number.isFinite(v.duration) || v.duration <= 0) return
  const want = Math.max(0, Math.min(v.duration - 1e-3, inPoint + (t - start)))
  const playing = !!opts?.playing
  const rate = opts?.rate ?? 1

  if (playing && rate > 0) {
    if (v.playbackRate !== rate) {
      try { v.playbackRate = rate } catch { /* out of supported range */ }
    }
    if (v.paused) {
      // Land on the right frame BEFORE starting, or the PIP runs offset by
      // however far the timeline had already travelled.
      if (Math.abs(v.currentTime - want) > PIP_RESYNC_TOL) {
        try { v.currentTime = want } catch { /* not seekable yet */ }
      }
      v.play().catch(() => { /* autoplay/decode hiccup — next frame retries */ })
    } else if (Math.abs(v.currentTime - want) > PIP_RESYNC_TOL) {
      // Genuine drift (a stall, a scrub mid-play). Correct it rarely, never
      // per frame — that is the thrash this function exists to avoid.
      try { v.currentTime = want } catch { /* not seekable yet */ }
    }
    return
  }

  if (!v.paused) v.pause()
  const tol = Math.max(1 / Math.max(1, fps), 0.03)
  if (Math.abs(v.currentTime - want) > tol) {
    try { v.currentTime = want } catch { /* not seekable yet */ }
  }
}

/** Pause every hidden PIP element except the ones drawn this frame.
 *
 *  A PIP whose clip has ended must not keep running: it would carry on
 *  decoding, and when the playhead loops back the element is somewhere else
 *  entirely and has to seek — the same stall this module avoids elsewhere.
 */
export function pausePipVideosExcept(active: Set<string>): void {
  for (const [src, v] of VIDEOS) {
    if (!active.has(src) && !v.paused) v.pause()
  }
}

// Last frame successfully drawn per source, at DISPLAY size. A decoder dip is
// normal — every paused seek causes one — and the placeholder is far more
// alarming than one repeated frame, so a dip now re-shows the previous picture
// instead of blanking the element. Cached at display size, not source size, so
// the copy is a few hundred pixels rather than a 1080p blit every frame.
const LAST_FRAME = new Map<string, HTMLCanvasElement>()

/**
 * Draw a PIP's picture, falling back to the last frame that worked.
 *
 * Returns false only when there is genuinely nothing to show yet (first load),
 * which is the one case where the caller's placeholder is the honest answer.
 */
export function drawPipVideoFrame(
  ctx: CanvasRenderingContext2D, v: HTMLVideoElement, src: string,
  g: { sx: number; sy: number; sw: number; sh: number; dw: number; dh: number },
): boolean {
  const w = Math.max(1, Math.round(g.dw))
  const h = Math.max(1, Math.round(g.dh))
  if (v.readyState >= 2) {
    try {
      let c = LAST_FRAME.get(src)
      if (!c) { c = document.createElement('canvas'); LAST_FRAME.set(src, c) }
      if (c.width !== w || c.height !== h) { c.width = w; c.height = h }
      const cg = c.getContext('2d')
      if (cg) {
        cg.clearRect(0, 0, w, h)
        cg.drawImage(v, g.sx, g.sy, g.sw, g.sh, 0, 0, w, h)
        ctx.drawImage(c, -g.dw / 2, -g.dh / 2, g.dw, g.dh)
        return true
      }
      ctx.drawImage(v, g.sx, g.sy, g.sw, g.sh, -g.dw / 2, -g.dh / 2, g.dw, g.dh)
      return true
    } catch { /* decoder had nothing this tick — fall through to the cache */ }
  }
  const cached = LAST_FRAME.get(src)
  if (cached && cached.width > 0 && cached.height > 0) {
    try {
      ctx.drawImage(cached, -g.dw / 2, -g.dh / 2, g.dw, g.dh)
      return true
    } catch { /* nothing usable */ }
  }
  return false
}

export interface PipDrawGeom {
  /** Source rectangle to sample (in the video's own pixels). */
  sx: number; sy: number; sw: number; sh: number
  /** Destination size in DISPLAY px (centred on the box's own origin). */
  dw: number; dh: number
}

/**
 * Source/destination rects implementing pip.py's box + framing.
 *
 * The box is filled by COVERING it and cropping the overflow — never padding —
 * so the source rect is the largest sub-rect of the source with the box's
 * aspect, shrunk by `zoom`, slid by the normalised offset and clamped to the
 * real margin. Clamping (rather than allowing overflow) is what makes a pan with
 * no headroom a no-op instead of dragging black in, matching ffmpeg's `crop`,
 * which pins its x/y into [0, in-out] itself.
 */
export function pipDrawGeom(
  srcW: number, srcH: number,
  boxW: number, boxH: number,
  framing: PipFraming | null | undefined,
): PipDrawGeom {
  const zoom = Math.max(1, framing?.zoom ?? 1)
  const fx = Math.max(-1, Math.min(1, framing?.x ?? 0))
  const fy = Math.max(-1, Math.min(1, framing?.y ?? 0))
  const boxAspect = boxW / Math.max(1e-6, boxH)
  // Largest source rect with the box's aspect ("cover"), then zoomed in.
  let sw = srcW
  let sh = srcW / boxAspect
  if (sh > srcH) { sh = srcH; sw = srcH * boxAspect }
  sw /= zoom
  sh /= zoom
  const marginX = (srcW - sw) / 2
  const marginY = (srcH - sh) / 2
  const sx = marginX + fx * marginX
  const sy = marginY + fy * marginY
  return { sx, sy, sw, sh, dw: boxW, dh: boxH }
}

export interface PipMask { type?: string | null; invert?: boolean }

/**
 * Whether this mask actually CUTS anything, mirroring pip.py's
 * `_shape_alpha_expr` returning None.
 *
 * `Mask.type` also allows rectangle/linear/mirror/heart/star. pip.py implements
 * none of those for a PIP — `rectangle` deliberately (it is the frame's own
 * shape, and no mask is cheaper than a full-white one) and the rest because
 * effects.render_mask_png is v1-only — so it emits no alpha at all and the PIP
 * stays a full rectangle. The client has to agree, INCLUDING about `invert`:
 * pip.py returns None before it ever looks at invert, so an inverted rectangle
 * bakes fully VISIBLE. Applying the even-odd hole here for a shape pip.py
 * doesn't cut would blank the PIP completely in preview while it exported intact.
 */
export function maskCuts(mask: PipMask | null | undefined): boolean {
  return mask?.type === 'circle' || mask?.type === 'rounded'
}

/** Add the shape's outline to the CURRENT path (no beginPath, no clip). */
function addShapePath(
  ctx: CanvasRenderingContext2D, shape: string, hw: number, hh: number,
): void {
  if (shape === 'circle') {
    // pip.py inscribes an ellipse in the element and forces the element square
    // for this shape — so an ellipse here is a circle there, and stays correct
    // if that box is ever allowed to be non-square again.
    ctx.ellipse(0, 0, hw, hh, 0, 0, Math.PI * 2)
    return
  }
  const r = 0.12 * Math.min(hw * 2, hh * 2)   // same 12% of the shorter side
  ctx.moveTo(-hw + r, -hh)
  ctx.lineTo(hw - r, -hh)
  ctx.quadraticCurveTo(hw, -hh, hw, -hh + r)
  ctx.lineTo(hw, hh - r)
  ctx.quadraticCurveTo(hw, hh, hw - r, hh)
  ctx.lineTo(-hw + r, hh)
  ctx.quadraticCurveTo(-hw, hh, -hw, hh - r)
  ctx.lineTo(-hw, -hh + r)
  ctx.quadraticCurveTo(-hw, -hh, -hw + r, -hh)
}

/**
 * Apply the shape as a canvas clip path, in the box's local (centred) frame.
 *
 * `invert` is honoured — the schema offers it and pip.py bakes it, so a
 * hole-punch PIP would otherwise preview as a solid circle and export as a hole.
 * Done as ONE even-odd path (the box rect plus the shape as an inner subpath),
 * which is precisely "everything except the shape"; there is no need to guess at
 * a second draw pass or a composite mode.
 */
export function clipToShape(
  ctx: CanvasRenderingContext2D, mask: PipMask | null | undefined,
  hw: number, hh: number,
): void {
  if (!maskCuts(mask)) return
  ctx.beginPath()
  if (mask?.invert) ctx.rect(-hw, -hh, hw * 2, hh * 2)
  addShapePath(ctx, mask!.type as string, hw, hh)
  ctx.clip(mask?.invert ? 'evenodd' : 'nonzero')
}


/**
 * Client-side geometry for ROTATING THE PICTURE INSIDE a PIP's shape.
 *
 * Mirrors render/pip.py's inner-rotation branch, which is `cover -> rotate ->
 * crop`, and the order is not interchangeable:
 *
 *  * the cover is GROWN first. A box_w x box_h window still wholly inside a
 *    rectangle turned by t needs that rectangle to be at least
 *    `w|cos t| + h|sin t|` by `w|sin t| + h|cos t|`; without the growth the
 *    rotate's transparent corners are dragged into the shape. Rotating costs
 *    magnification — that is inherent to rotate-and-fill, not a choice.
 *  * the PAN is applied AFTER the rotation, in the box's own axes, because the
 *    renderer pans at the crop. Folding it into the source rect instead (which
 *    is what the unrotated path does) would make "pan X" slide the picture
 *    diagonally once it was turned.
 */
export interface PipInnerPlan {
  /** How much the cover had to grow to keep corners out of the shape. */
  coverScale: number
  /** Destination size in DISPLAY px, centred on the box origin. */
  destW: number
  destH: number
  rotRad: number
  /** Post-rotation pan in DISPLAY px. */
  offX: number
  offY: number
}

export function pipInnerPlan(
  boxW: number, boxH: number,
  opts: { zoom?: number; rotation?: number; x?: number; y?: number },
): PipInnerPlan {
  const rotDeg = opts.rotation ?? 0
  const rotRad = (rotDeg * Math.PI) / 180
  const zoom = Math.max(1, opts.zoom ?? 1)
  const ca = Math.abs(Math.cos(rotRad))
  const sa = Math.abs(Math.sin(rotRad))
  const coverScale = boxW > 0 && boxH > 0
    ? Math.max((boxW * ca + boxH * sa) / boxW, (boxW * sa + boxH * ca) / boxH)
    : 1
  const destW = boxW * zoom * coverScale
  const destH = boxH * zoom * coverScale
  // The renderer's crop offset is `(in-out)/2 + f*(in-out)/2`; a crop window
  // moving right shows the picture moving LEFT, hence the negation.
  const fx = Math.max(-1, Math.min(1, opts.x ?? 0))
  const fy = Math.max(-1, Math.min(1, opts.y ?? 0))
  // `+ 0` collapses -0 to 0. Harmless for a canvas translate, but a signed
  // zero leaking out of a geometry helper is the kind of thing that makes an
  // equality check downstream fail for no visible reason.
  return {
    coverScale, destW, destH, rotRad,
    offX: -fx * (destW - boxW) / 2 + 0,
    offY: -fy * (destH - boxH) / 2 + 0,
  }
}
