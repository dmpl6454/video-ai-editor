// Shared geometry + keyframe sampling for sticker overlays. Used by both the
// display layer (StickerLayer draws the glyph) and the interaction layer (the
// same code hit-tests + sizes the selection handles), so the box always lines
// up exactly with what's painted.

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
