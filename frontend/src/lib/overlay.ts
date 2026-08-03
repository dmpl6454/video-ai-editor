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

// ---------------------------------------------------------------------------
// Shared overlay boxes: one description of "a selectable thing on the canvas"
// ---------------------------------------------------------------------------

export interface OverlayBox {
  id: string
  kind: 'sticker' | 'text'
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

// TextLayer measures + wraps its own text, so it is the only place that knows
// a text clip's real on-screen box. It publishes that here each frame and
// StickerLayer (which owns pointer interaction for the whole preview) reads
// it. A frame of staleness is harmless — both run on rAF against the same
// playhead.
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
