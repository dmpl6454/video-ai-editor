// Shared geometry + keyframe sampling for canvas overlays. Used by both the
// display layer (StickerLayer draws the glyph) and the interaction layer (the
// same code hit-tests + sizes the selection handles), so the box always lines
// up exactly with what's painted.
//
// It is also the meeting point between TextLayer and StickerLayer. Text and
// stickers are drawn by two different components, but they must be SELECTABLE,
// DRAGGABLE and RESIZABLE identically — the round-4/round-5 gap was that only
// stickers had on-canvas handles (M-01). Rather than duplicate the handle
// geometry (which would drift the moment either side is tuned), the box shape,
// the chrome drawing and the hit tests live here once, and a small registry
// lets the layer that OWNS a clip's draw math publish its measured box to the
// layer that owns interaction.

export interface KFSpec { keyframes: [number, number][]; interp?: string }
export type KFNum = number | KFSpec

export interface StickerClip {
  id: string
  src: string
  start: number
  end: number
  label?: string | null
  transform?: { x?: KFNum; y?: KFNum; scale?: KFNum; rotation?: KFNum; opacity?: KFNum }
}

export function isSticker(c: unknown): c is StickerClip {
  if (!c || typeof c !== 'object') return false
  const o = c as Record<string, unknown>
  return typeof o.id === 'string' && typeof o.src === 'string' && typeof o.end === 'number'
}

export function sampleKF(v: KFNum | undefined, t: number, fallback: number): number {
  if (typeof v === 'number') return v
  if (v && typeof v === 'object' && Array.isArray(v.keyframes) && v.keyframes.length) {
    const pts = [...v.keyframes].sort((a, b) => a[0] - b[0])
    if (t <= pts[0][0]) return pts[0][1]
    if (t >= pts[pts.length - 1][0]) return pts[pts.length - 1][1]
    for (let i = 0; i < pts.length - 1; i++) {
      const [t0, v0] = pts[i]
      const [t1, v1] = pts[i + 1]
      if (t0 <= t && t <= t1) {
        const f = (t - t0) / Math.max(1e-9, t1 - t0)
        const interp = v.interp ?? 'linear'
        let g = f
        if (interp === 'ease-in') g = f * f
        else if (interp === 'ease-out') g = 1 - (1 - f) ** 2
        else if (interp === 'ease-in-out') g = 3 * f * f - 2 * f * f * f
        else if (interp === 'back-out') g = 1 - (1 - f) ** 3
        else if (interp === 'step') g = 0
        return v0 + (v1 - v0) * g
      }
    }
    return pts[pts.length - 1][1]
  }
  return fallback
}

/** Every distinct keyframe time on a clip's transform, ascending, CLIP-LOCAL.
 *
 *  The union across properties is the honest answer to "where are this clip's
 *  keyframes": the UI keys all five together, but an EDL from Claude/MCP (or an
 *  older project) can perfectly well animate only `scale`, and that key is just
 *  as real. Times within half a frame at 30fps collapse to one — the panel and
 *  the timeline must agree on the count, and floats that arrived by different
 *  routes are never bit-identical.
 */
/** How close two keyframe times count as "the same key": half a frame.
 *
 *  Mirrors `_kf_tol` in agent/dispatch.py, and MUST stay equal to it. The panel
 *  used a hardcoded 0.017 while the backend matched at 1e-3, so between 1ms and
 *  17ms of a stored key the ◆ button lit up, promised "Remove the keyframe at the
 *  playhead", and the handler removed nothing — which is where the playhead
 *  practically always sits, since it is a rAF wall clock that never re-lands on
 *  the exact float a key was stored at.
 *
 *  Derived from fps rather than fixed: 0.017 is half a frame only at 30fps, and
 *  at 60 it is a whole frame — the same mismatch pointing the other way.
 */
export function keyEps(fps: number | undefined): number {
  return Math.max(1e-3, 0.5 / Math.max(1, fps || 30))
}

export function keyframeTimes(clip: unknown, fps?: number): number[] {
  const tx = (clip as { transform?: Record<string, unknown> })?.transform
  if (!tx) return []
  const all: number[] = []
  for (const p of ['x', 'y', 'scale', 'rotation', 'opacity']) {
    const v = tx[p] as KFSpec | undefined
    if (!v || typeof v !== 'object' || !Array.isArray(v.keyframes)) continue
    for (const k of v.keyframes) {
      const t = k?.[0]
      if (typeof t === 'number' && Number.isFinite(t)) all.push(t)
    }
  }
  // Sort BEFORE collapsing: dedup-then-sort keeps whichever property happened
  // to be visited first as the survivor, so the reported time depended on
  // property order. Sorted, the earliest of each cluster always wins.
  all.sort((a, b) => a - b)
  const out: number[] = []
  const eps = keyEps(fps)
  for (const t of all) if (!out.length || t - out[out.length - 1] >= eps) out.push(t)
  return out
}

export interface StickerGeom {
  cx: number; cy: number   // center, display px
  size: number             // glyph box side, display px
  rot: number              // radians
  opa: number
  scale: number            // transform scale (for resize math)
  x: number; y: number     // center in EDL/canvas coords (for committing)
}

// Position/size of a sticker on screen at time `t`. Mirrors TextLayer's draw
// math exactly so the selection box matches the painted glyph.
export function stickerGeom(
  sk: StickerClip, t: number, canvasW: number, canvasH: number,
  width: number, height: number, override?: { x?: number; y?: number; scale?: number },
): StickerGeom {
  const tx = sk.transform ?? {}
  const localT = t - sk.start
  const dsx = width / canvasW
  const dsy = height / canvasH
  const x = override?.x ?? sampleKF(tx.x, localT, canvasW / 2)
  const y = override?.y ?? sampleKF(tx.y, localT, canvasH / 2)
  const scale = override?.scale ?? sampleKF(tx.scale, localT, 1)
  const rot = (sampleKF(tx.rotation, localT, 0) * Math.PI) / 180
  const opa = sampleKF(tx.opacity, localT, 1)
  // Match the server's sticker sizing (render/text_overlay.py: base = max(w,h)).
  // Using min() here made the client glyph and the server-baked PNG diverge in
  // size after an aspect-ratio change (they only agreed on square canvases).
  const baseSize = Math.max(canvasW, canvasH) * 0.22 * scale
  const size = Math.max(20, baseSize * Math.min(dsx, dsy))
  return { cx: x * dsx, cy: y * dsy, size, rot, opa, scale, x, y }
}

/**
 * On-screen box of a PIP (v2+) clip, mirroring render/pip.py exactly.
 *
 * pip.py sizes a PIP by its WIDTH — `target_long = out_long * 0.35 * scale`
 * then `scale=w=target_long:h=-1` — so the height follows the SOURCE's aspect,
 * which the EDL does not record (trim is time-only; a clip's own fields say
 * nothing about its frame size). `lib/media.srcDimsFor` probes it and caches,
 * and the caller passes the result in; with no dims yet, falling back to the
 * canvas aspect keeps the box usable rather than absent.
 *
 * Placement is the clip's x/y as the CENTRE in canvas pixels — pip.py emits
 * `overlay=x='<x>*sx-overlay_w/2'` — with the canvas centre as the default,
 * matching that file's `else` branches. Keep the two in step: this is the
 * geometry a drag commits against, so a divergence puts the handles somewhere
 * the render will not place the picture.
 */
export function pipGeom(
  clip: { start: number; transform?: StickerClip['transform']
          mask?: { type?: string } | null; fit?: string },
  t: number,
  canvas: { w: number; h: number },
  srcAspect: number | null,
  width: number, height: number,
  override?: { x?: number; y?: number; scale?: number },
): { cx: number; cy: number; hw: number; hh: number; rot: number
     x: number; y: number; scale: number } {
  const tx = clip.transform ?? {}
  const localT = t - clip.start
  const scale = override?.scale ?? sampleKF(tx.scale, localT, 1)
  const x = override?.x ?? sampleKF(tx.x, localT, canvas.w / 2)
  const y = override?.y ?? sampleKF(tx.y, localT, canvas.h / 2)
  const rot = (sampleKF(tx.rotation, localT, 0) * Math.PI) / 180
  // Aspect of the element's BOX, matching pip.py's framing block: a circle
  // forces a square (a circular mask over a 16:9 element is an ellipse), and
  // fit='cover' crops to the canvas's shape. Only otherwise does the source's
  // own aspect apply. These must stay in step or the drag handles frame
  // something other than what the renderer draws.
  const isCircle = clip.mask?.type === 'circle'
  const isCover = clip.fit === 'cover'
  const long = Math.max(40, Math.max(canvas.w, canvas.h) * 0.35 * scale)
  // Branch-for-branch with pip.py, in ITS order, rather than a boxAspect plus a
  // rule about which edge `long` lands on. Those are not the same shape of
  // decision and the difference was a live bug: `want_square` is checked FIRST
  // there, so a circle wins outright over a cover fit, while a single-aspect
  // formula applied cover's width rule to it as well and drew the circle
  // 0.5625x too small on a 1080x1920 canvas — reachable by ticking Circle and
  // Fill frame together, which the panel offers side by side.
  let wCanvas: number
  let hCanvas: number
  if (isCircle) {
    // A circle needs a square to live in, or the mask reads as an ellipse.
    wCanvas = long
    hCanvas = long
  } else if (isCover) {
    // The canvas's own shape, so the PIP reads as a small version of the frame.
    // `long` is the box's LONG edge, which is the height on a portrait canvas.
    if (canvas.w >= canvas.h) {
      wCanvas = long
      hCanvas = (long * canvas.h) / Math.max(1, canvas.w)
    } else {
      wCanvas = (long * canvas.w) / Math.max(1, canvas.h)
      hCanvas = long
    }
  } else {
    // pip.py's default is `scale=w=target_long:h=-1` — WIDTH is pinned and the
    // height follows the source, so a portrait source is TALLER than `long`.
    // Not "the long edge is `long`"; deriving it that way caps portrait sources.
    const a = srcAspect && Number.isFinite(srcAspect) && srcAspect > 0
      ? srcAspect
      : canvas.w / Math.max(1, canvas.h)
    wCanvas = long
    hCanvas = long / a
  }
  const dsx = width / canvas.w
  const dsy = height / canvas.h
  return { cx: x * dsx, cy: y * dsy, hw: (wCanvas * dsx) / 2, hh: (hCanvas * dsy) / 2,
           rot, x, y, scale }
}

// ---------------------------------------------------------------------------
// Shared overlay boxes: one description of "a selectable thing on the canvas"
// ---------------------------------------------------------------------------

export interface OverlayBox {
  id: string
  kind: 'sticker' | 'text' | 'pip'
  cx: number; cy: number    // center, display px
  hw: number; hh: number    // half width/height, display px
  rot: number               // radians
  x: number; y: number      // the same center in EDL-canvas px (what we commit)
  sizeCanvasPx?: number     // text only: the resolved style.size a resize scales
  // Text only. TextLayer.resolveAnchor treats these exact canvas-px values as
  // "no explicit anchor — use the role layout", because they are what the
  // construction-site defaults write. A drag that lands on one would commit
  // and then visibly snap back to the role position, so the interaction layer
  // nudges off them. Published from TextLayer so the two lists cannot diverge.
  xSentinels?: number[]
  ySentinels?: number[]
}

export function boxFromStickerGeom(id: string, g: StickerGeom): OverlayBox {
  return { id, kind: 'sticker', cx: g.cx, cy: g.cy, hw: g.size / 2, hh: g.size / 2,
           rot: g.rot, x: g.x, y: g.y }
}

// A pointer position expressed in a box's own unrotated, centered frame.
export function toLocal(px: number, py: number, b: OverlayBox): { lx: number; ly: number } {
  const dx = px - b.cx, dy = py - b.cy
  const cos = Math.cos(-b.rot), sin = Math.sin(-b.rot)
  return { lx: dx * cos - dy * sin, ly: dx * sin + dy * cos }
}

export function hitsBody(px: number, py: number, b: OverlayBox): boolean {
  const { lx, ly } = toLocal(px, py, b)
  return Math.abs(lx) <= b.hw && Math.abs(ly) <= b.hh
}

// Corners in local coords, in the fixed order the chrome draws them.
export const CORNERS = [[-1, -1], [1, -1], [1, 1], [-1, 1]] as const

/** Compositing order with the item being dragged moved to the END (= on top).
 *
 *  A sticker's z is only raised when the gesture COMMITS (set_clip_transform's
 *  raise_to_front). Painting the live drag in stored z-order therefore made an
 *  older sticker slide UNDER a newer one for the whole gesture and jump to the
 *  front on release — "when I drag the previous emoji it gets behind the latest
 *  one, but after placement it works fine". Hoisting it while dragging previews
 *  the committed result. When nothing overlaps at the drop point no raise is
 *  issued, but then the relative order was never visible anyway.
 *
 *  Returns the input array unchanged (same reference) when there is no drag, so
 *  the common per-frame path allocates nothing.
 */
export function paintOrder<T extends { id: string }>(items: T[], dragId?: string): T[] {
  if (!dragId) return items
  const i = items.findIndex((s) => s.id === dragId)
  if (i < 0 || i === items.length - 1) return items
  return [...items.slice(0, i), ...items.slice(i + 1), items[i]]
}

// TextLayer measures + wraps its own text, so it is the only place that knows
// a text clip's real on-screen box. It publishes that here each frame and
// StickerLayer (which owns pointer interaction for the whole preview) reads
// it. A frame of staleness is harmless — both run on rAF against the same
// playhead.
//
// CONTRACT: a published box is already LIVE. TextLayer reads the same
// getOverlayDrag() override to draw its glyphs, so the move offset and the
// resize multiplier are folded in before it lands here. A consumer must NOT
// re-apply them — StickerLayer did, and the selection chrome then moved at
// double the pointer distance (visibly detaching from the text it framed) and
// inflated by mul² on a resize. Under a live resize TextLayer also RE-WRAPS at
// the new size, so line breaks can change and the box is not a uniform scale of
// the previous one: only the publisher can compute it correctly.
let TEXT_BOXES: OverlayBox[] = []

export function publishTextBoxes(boxes: OverlayBox[]): void { TEXT_BOXES = boxes }
export function getTextBoxes(): OverlayBox[] { return TEXT_BOXES }

// Live drag feedback flows the other way: StickerLayer owns the gesture, but
// TextLayer owns the pixels, so the offset being dragged has to reach it
// without a React render (which would re-run the whole draw effect mid-drag).
export interface OverlayDragOverride {
  id: string
  dx: number; dy: number    // display px offset from the drawn position
  sizeMul: number           // 1 while moving; the live factor while resizing
}

let DRAG_OVERRIDE: OverlayDragOverride | null = null

export function setOverlayDrag(o: OverlayDragOverride | null): void { DRAG_OVERRIDE = o }
export function getOverlayDrag(): OverlayDragOverride | null { return DRAG_OVERRIDE }

/** What a drop asked the server for; only the fields it committed are set. */
export interface DropExpectation {
  id: string
  x?: number; y?: number      // canvas px
  scale?: number
  size?: number               // text style.size, canvas px
}

/**
 * Has `clip` caught up with what the drop committed?
 *
 * StickerLayer keeps the drag override alive past pointer-up and releases it
 * only when this returns true, because a commit is not instant: dispatch →
 * ~120 ms debounce → GET /edl. Releasing on pointer-up redrew the overlay from
 * its STORED position for that window, so a drop bounced back to where the drag
 * started and then jumped forward (measured at 233 ms on a real recording).
 *
 * Tolerances exist because the commit ROUNDS — asking for x=511.6 stores 512,
 * and an exact compare would never settle, pinning the overlay until the
 * timeout. A keyframed property is an object rather than a number and can never
 * match; that returns false on purpose and the caller's timeout resolves it,
 * since guessing which key the drag corresponds to would be worse than a
 * bounded wait.
 */
export function dropSettled(clip: unknown, e: DropExpectation): boolean {
  const c = clip as {
    transform?: { x?: unknown; y?: unknown; scale?: unknown }
    style?: { size?: unknown }
  }
  const near = (actual: unknown, want: number | undefined, tol: number): boolean =>
    want === undefined
    || (typeof actual === 'number' && Number.isFinite(actual) && Math.abs(actual - want) <= tol)
  return near(c?.transform?.x, e.x, 1)
      && near(c?.transform?.y, e.y, 1)
      && near(c?.transform?.scale, e.scale, 0.011)
      && near(c?.style?.size, e.size, 1)
}

/**
 * Keep a dragged overlay's CENTRE on the canvas, so at worst half of it hangs
 * off an edge and half stays on frame.
 *
 * KEEPING THE WHOLE OVERLAY ON FRAME WAS TRIED AND DELIBERATELY REVERTED.
 * Bounding the centre to `[half, extent - half]` does stop an emoji being
 * sliced by the frame edge, but a sticker that runs off the edge is a normal
 * composition — "it should be doing as earlier" — and taking it away broke a
 * gesture people use to get an emoji to peek in from outside. It was also
 * aimed at the wrong target: the "coloured strip" that motivated it is not a
 * sliced sticker at all, it is stale ink in the overlay canvas's last device
 * column (see the device-pixel clear in StickerLayer's draw loop). Clamping
 * harder would never have removed it — the strip is there with no sticker
 * anywhere near the edge.
 *
 * Unclamped, one drag can strand a sticker almost entirely off-canvas, leaving
 * a thin vertical band of its artwork down the frame edge — reported as "when i
 * drag the emoji on the right side of the video, then it leaves a phantom
 * color", because a sliver of emoji at the frame edge reads as a rendering
 * artifact rather than as the sticker. Measured with 🤣 at x=1900 on a 1920
 * canvas: the overlay canvas's last 16 columns inked #e6c75e→#d6af4d, 224px
 * tall, and the export carried the same band at columns 1912-1919. It is also
 * close to unrecoverable by hand, since the selection box and its ✕ are off
 * canvas too and the only grab target left is the sliver.
 *
 * CLAMPING THE CENTRE WAS NOT ENOUGH, and this is the third report of the same
 * band. A centre pinned to the frame still lets HALF the artwork hang off, and
 * half an emoji sliced down the frame edge is the very band being complained
 * about — the first fix reduced its width without changing its nature. It is
 * worst at 16:9, where the picture spans the full width of the preview pane so
 * the sliced edge lands flush against the panel seam and reads as UI chrome
 * rather than as the sticker. Measured on the packaged build: 🔥 dragged right
 * on a 1920x1080 canvas settled at x=1860 with ~40% of the artwork off-frame.
 *
 * Applies to the GESTURE only, never to set_clip_transform: deliberate
 * off-canvas placement is how a sticker is animated flying in from outside the
 * frame, so a keyframed x legitimately starts beyond the edge. Direct
 * manipulation is the one case where the user cannot have meant to throw the
 * object away.
 */
export function clampOverlayCentre(
  x: number, y: number, canvas: { w: number; h: number },
): { x: number; y: number } {
  return {
    x: Math.max(0, Math.min(canvas.w, x)),
    y: Math.max(0, Math.min(canvas.h, y)),
  }
}

/** A live gesture in progress, as the interaction layer tracks it. */
export interface LiveDrag {
  id: string
  mode: 'move' | 'resize'
  live: { x?: number; y?: number; scale?: number }
}

/**
 * The transform an overlay should be DRAWN with right now — which is not always
 * the one in the EDL. Precedence: the live gesture, then the committed-but-not-
 * yet-confirmed drop, then (undefined) the stored value.
 *
 * This lives here, apart from the component, because getting the SECOND rung
 * wrong is invisible in review and obvious only under measurement. The first
 * attempt at the drop-bounce fix held `setOverlayDrag()` past pointer-up, which
 * is the registry TextLayer reads to draw glyphs — but StickerLayer resolves
 * sticker geometry itself and consulted its own drag ref in two places, so
 * stickers still snapped back to their stored position for the whole
 * dispatch → ~120 ms debounce → GET /edl round-trip. Measured on the packaged
 * build: dragged to x=287, back to x=190 for 18 frames (274 ms), then forward
 * to x=285. Each layer owns its own draw math, so each must consult this; a
 * single shared override cannot serve both.
 *
 * Mode matters: a move commits x/y and must not disturb scale, and a resize the
 * reverse. Returning a field as `undefined` lets stickerGeom's `?? sampleKF()`
 * fall through to the stored/keyframed value rather than pinning it.
 */
export function resolveLiveOverride(
  id: string,
  drag: LiveDrag | null | undefined,
  pending: DropExpectation | null | undefined,
): { x?: number; y?: number; scale?: number } | undefined {
  if (drag && drag.id === id) {
    return drag.mode === 'move'
      ? { x: drag.live.x, y: drag.live.y }
      : { scale: drag.live.scale }
  }
  if (pending && pending.id === id) {
    return { x: pending.x, y: pending.y, scale: pending.scale }
  }
  return undefined
}

/** Nudge a committed coordinate off a "means unset" sentinel value.
 *
 *  TextLayer.resolveAnchor treats a handful of exact canvas-px values as "no
 *  explicit anchor — use the role layout", because they are what the
 *  construction-site defaults write. A drag that happens to land on one (the
 *  role's own anchor y is a very reachable target — you dragged from there)
 *  would commit successfully and then snap straight back, i.e. look broken.
 *  One pixel is invisible and unambiguous.
 */
export function unsentinel(v: number, sentinels: number[] | undefined): number {
  if (!sentinels?.length) return v
  return sentinels.some((s) => Math.abs(v - s) < 0.5) ? v + 1 : v
}

// ---------------------------------------------------------------------------
// Live transform preview: absolute slider values -> CSS applied over a BAKED frame
// ---------------------------------------------------------------------------

export interface LiveTransformValues {
  scale?: number; rotation?: number; opacity?: number; dx?: number; dy?: number
}

/** What the render currently on screen already has applied. */
export interface BakedTransform { scale: number; rotation: number; opacity: number }

/**
 * Convert an ABSOLUTE transform value into the CSS that shows it, given that the
 * `<video>` underneath is already displaying a render with `baked` applied.
 *
 * Reported as: "when I rotate the screen to any angle, at first it works
 * properly but, when I again rotate the screen from the last placed angle, then
 * it doesn't work according to the angle rotation… when I leave the rotation
 * toggle, it works fine, the issue is only with the preview."
 *
 * The sliders publish the absolute value they hold, and Preview applied it
 * directly — `rotate(${live.rotation}deg)` on top of a frame ALREADY rotated by
 * the last commit. So the eye saw baked + live. Rotate to -16 and it is right
 * (baked 0); drag from there to -30 and the preview shows -46. It looks correct
 * again on release only because the new render lands and the override clears —
 * which is exactly why the report ends "the issue is only with the preview".
 *
 * Scale and opacity had the same defect, multiplicatively: CSS `scale()` and
 * `opacity` compose with what is baked, so the second drag showed scale**2.
 * Only dx/dy were right, because StickerLayer already published deltas.
 *
 * `baked` MUST be the value the visible render was made with, latched at the
 * start of the gesture — NOT re-read from the live EDL. The commit lands ~120ms
 * before its render does, so a freshly-read `baked` equals the new value, the
 * delta collapses to zero, and the preview snaps back to the pre-drag frame for
 * the round-trip. That is the same snap-back the sticker drop and Preview's
 * onLoadedData timing both exist to prevent.
 *
 * Both ratios are approximations in the same class the CSS preview already
 * accepts: the baked frame's black letterbox bars and rotation corners get
 * scaled and rotated along with the picture, so the edges are not what a true
 * re-render produces. The ANGLE and SIZE are right, which is what was reported;
 * the release re-render fixes the edges.
 */
export function liveCssTransform(
  live: LiveTransformValues, baked: BakedTransform,
): { dx: number; dy: number; scaleMul: number; rotateDeg: number; opacityMul: number } {
  // A near-zero denominator cannot be recovered by compositing: a frame baked at
  // scale 0 or opacity 0 carries no picture to enlarge or brighten. Floor it and
  // let the ratio clamp below, rather than emitting Infinity/NaN into a style
  // string (which silently drops the whole transform, i.e. no live preview).
  const bScale = Math.abs(baked.scale) < 1e-3 ? 1e-3 : baked.scale
  const bOpa = baked.opacity < 0.02 ? 0.02 : baked.opacity
  return {
    dx: live.dx ?? 0,
    dy: live.dy ?? 0,
    scaleMul: live.scale === undefined ? 1 : live.scale / bScale,
    rotateDeg: live.rotation === undefined ? 0 : live.rotation - baked.rotation,
    // CSS opacity cannot exceed 1, so a frame baked dim can only be dimmed
    // further, never restored. Clamped instead of left invalid; the release
    // re-render is what actually brightens it.
    opacityMul: live.opacity === undefined
      ? 1
      : Math.max(0, Math.min(1, live.opacity / bOpa)),
  }
}

/** A clip's stored colour grade, in the backend's eq-param space.
 *
 *  Defaults are eq's identity: brightness is an ADDITIVE offset (0 = no change)
 *  while contrast and saturation are multipliers around 1.
 */
export function colorGradeOf(
  clip: unknown,
): { brightness: number; contrast: number; saturation: number } {
  const effects = (clip as { effects?: { type: string; params?: Record<string, number> }[] })?.effects
  const p = effects?.find((e) => e.type === 'color' || e.type === 'color_grade')?.params ?? {}
  return {
    brightness: p.brightness ?? 0,
    contrast: p.contrast ?? 1,
    saturation: p.saturation ?? p.sat ?? 1,
  }
}

/**
 * The colour half of `liveCssTransform`, and the same defect: the Color panel
 * publishes ABSOLUTE eq params and Preview applied them as absolute CSS filters
 * over a frame that already has the previous grade baked in. So the first
 * brightness drag looked right (baked = identity) and every one after it
 * compounded — exactly the rotation report, in the neighbouring three lines.
 *
 * CSS `brightness`/`contrast`/`saturate` all MULTIPLY what is underneath, so the
 * relative form is a ratio in each case.
 *
 * The brightness mapping inherits an approximation that was already here and
 * does not add one: eq's `brightness` is an additive offset on the signal, which
 * the CSS preview has always modelled as `brightness(1+v)`. Under that model the
 * consistent relative value is (1+b1)/(1+b0). The commit's re-render is what
 * makes it exact.
 *
 * `baked` must be latched at the start of the gesture, for the same reason as the
 * transform: the commit lands before its render, so re-reading the clip mid-flight
 * collapses the ratio to 1 and the preview snaps back to the pre-drag grade.
 * Note this makes the panel's stated intent actually work — it seeds the two
 * sliders you are NOT dragging from the clip's current grade so their
 * just-committed values stay visible, which only holds if the comparison is
 * against what is on screen rather than against those same new values.
 */
export function liveCssFilter(
  live: { brightness?: number; contrast?: number; saturation?: number },
  baked: { brightness: number; contrast: number; saturation: number },
): { brightnessMul: number; contrastMul: number; saturateMul: number } {
  const ratio = (want: number | undefined, have: number) => {
    if (want === undefined) return 1
    const d = Math.abs(have) < 1e-3 ? 1e-3 : have
    const r = want / d
    return Number.isFinite(r) ? Math.max(0, r) : 1
  }
  return {
    brightnessMul: ratio(
      live.brightness === undefined ? undefined : 1 + live.brightness,
      1 + baked.brightness),
    contrastMul: ratio(live.contrast, baked.contrast),
    saturateMul: ratio(live.saturation, baked.saturation),
  }
}

// --- who owns a live Transform-slider value --------------------------------
//
// The Properties Transform sliders publish an in-flight {clipId, scale?,
// rotation?, opacity?} while the pointer is down. WHICH element that value may
// be applied to depends on which lane the clip is on, because the preview does
// not composite every lane into the same pixels:
//
//   v1  -> baked into the render, so the <video> IS that clip's picture and a
//          CSS transform/opacity on it is a faithful stand-in.
//   v2+ -> a PIP, whose picture the BROWSER draws (pipDraw/StickerLayer); it is
//          deliberately not in the render at all. A CSS transform/opacity on the
//          <video> therefore changes the main picture and leaves the PIP alone.
//
// Getting this wrong is not subtle once you look for it, but it is easy to miss
// on a scale or rotation slider: reported on the one that shows plainest, as
// "when I lower the opacity for pip, the main video's opacity got also lower".

/** May a live Transform value be applied as CSS on the preview `<video>`? */
export function liveVideoCssApplies(trackId: string | null | undefined): boolean {
  // Only v1. Not "is it a video track" — v2+ ARE video tracks and are exactly
  // the case this exists to exclude. A clip whose lane could not be resolved
  // gets `false`: declining to preview is a missing nicety, while applying it
  // to the wrong element is a visibly wrong picture.
  return trackId === 'v1'
}

/** Fold a live slider `scale` into a pointer-drag override for `pipGeom`.
 *
 *  Precedence is DRAG OVER SLIDER for any field both could name — the drag is
 *  the more direct manipulation — but the two must still compose, so a drag
 *  supplying only x/y keeps them while the slider supplies `scale`. Returning
 *  the override object untouched when there is no live scale keeps the common
 *  path allocation-free and identity-stable.
 */
export function mergeLivePipScale(
  // Mirrors resolveLiveOverride's own return type — `undefined`, not `null` —
  // so the two compose without a cast at the call site.
  override: { x?: number; y?: number; scale?: number } | undefined,
  liveScale: number | undefined,
): { x?: number; y?: number; scale?: number } | undefined {
  if (liveScale === undefined) return override
  return { ...(override ?? {}), scale: override?.scale ?? liveScale }
}
