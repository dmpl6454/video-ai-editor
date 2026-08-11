import { describe, expect, it } from 'vitest'
import { cropLayout, dragToOffset } from './cropLayout'

// A 16:9 source on a 9:16 canvas — the case the crop view exists for, and the
// one every reported symptom was observed in.
const BASE = {
  canvasW: 1080, canvasH: 1920,
  srcW: 1920, srcH: 1080,
  paneW: 349, paneH: 620,
  scale: 1, x: 0, y: 0,
}

describe('cropLayout', () => {
  it('centres both boxes when there is no pan', () => {
    const { win, pic } = cropLayout(BASE)
    expect(win.left + win.w / 2).toBeCloseTo(BASE.paneW / 2, 6)
    expect(win.top + win.h / 2).toBeCloseTo(BASE.paneH / 2, 6)
    expect(pic.left + pic.w / 2).toBeCloseTo(BASE.paneW / 2, 6)
    expect(pic.top + pic.h / 2).toBeCloseTo(BASE.paneH / 2, 6)
  })

  it('keeps the window at the canvas aspect ratio', () => {
    const { win } = cropLayout(BASE)
    expect(win.w / win.h).toBeCloseTo(BASE.canvasW / BASE.canvasH, 4)
  })

  it('shows the whole source at 1x, so nothing croppable is off-screen', () => {
    const { pic } = cropLayout(BASE)
    expect(pic.w).toBeLessThanOrEqual(BASE.paneW + 0.01)
    expect(pic.h).toBeLessThanOrEqual(BASE.paneH + 0.01)
    expect(pic.w / pic.h).toBeCloseTo(BASE.srcW / BASE.srcH, 4)
  })

  // --- the reported bugs ----------------------------------------------------

  it('positive x moves the PICTURE right (compositor sign convention)', () => {
    const a = cropLayout(BASE)
    const b = cropLayout({ ...BASE, x: 200 })
    expect(b.pic.left).toBeGreaterThan(a.pic.left)
    // ...by exactly x canvas-px, converted once through winScale.
    expect(b.pic.left - a.pic.left).toBeCloseTo(200 * a.winScale, 6)
  })

  it('the window NEVER moves when x/y change', () => {
    // The old view pinned the picture and slid the window the opposite way, so
    // dragging right sent the yellow rectangle left.
    const a = cropLayout(BASE)
    for (const [x, y] of [[300, 0], [-300, 0], [0, 250], [-120, -90]]) {
      const b = cropLayout({ ...BASE, x, y })
      expect(b.win.left).toBeCloseTo(a.win.left, 6)
      expect(b.win.top).toBeCloseTo(a.win.top, 6)
    }
  })

  it('the window NEVER resizes when scale changes; the picture grows', () => {
    // `fitScale` used to divide by scale, so zooming in shrank the frame
    // instead of enlarging the footage ("there is video behind the framing
    // which gets bigger when I increase the scale").
    const a = cropLayout(BASE)
    for (const scale of [1.5, 2, 4]) {
      const b = cropLayout({ ...BASE, scale })
      expect(b.win.w).toBeCloseTo(a.win.w, 6)
      expect(b.win.h).toBeCloseTo(a.win.h, 6)
      expect(b.winScale).toBeCloseTo(a.winScale, 6)
      expect(b.pic.w).toBeCloseTo(a.pic.w * scale, 6)
      expect(b.pic.h).toBeCloseTo(a.pic.h * scale, 6)
    }
  })

  it('a zoomed picture stays centred on the window when there is no pan', () => {
    const { win, pic } = cropLayout({ ...BASE, scale: 2.5 })
    expect(pic.left + pic.w / 2).toBeCloseTo(win.left + win.w / 2, 6)
    expect(pic.top + pic.h / 2).toBeCloseTo(win.top + win.h / 2, 6)
  })

  it('covers the window on both axes at 1x (this is what "cover" means)', () => {
    // A portrait canvas, a landscape canvas and a square one: the picture must
    // never be smaller than the window, or the crop would expose black.
    for (const [cw, ch] of [[1080, 1920], [1920, 1080], [1080, 1080]]) {
      const { win, pic } = cropLayout({ ...BASE, canvasW: cw, canvasH: ch })
      expect(pic.w).toBeGreaterThanOrEqual(win.w - 0.01)
      expect(pic.h).toBeGreaterThanOrEqual(win.h - 0.01)
    }
  })
})

describe('cropLayout — clamping to the pan the renderer honours', () => {
  // ffmpeg's `crop` pins its x/y into [0, in_w-out_w], so anything past the
  // margin is silently discarded by the bake. Measured on a 1920x1080 source
  // over a 1080x1920 canvas: y=400 and y=-978 both rendered byte-identically
  // to y=0, while the view happily slid the picture and painted black.
  it('reports zero vertical margin when the source only just covers', () => {
    const { limit } = cropLayout(BASE)
    // 1080 source height * cover(1.7778) == 1920 == canvas height, exactly.
    expect(limit.y).toBeLessThan(1)
    expect(limit.x).toBeGreaterThan(100)   // width genuinely overflows
  })

  it('clamps an over-range pan instead of sliding past the footage', () => {
    const { clamped, limit, pic, win } = cropLayout({ ...BASE, x: 99999, y: 99999 })
    expect(clamped.x).toBeCloseTo(limit.x, 6)
    expect(clamped.y).toBeCloseTo(limit.y, 6)
    // At the limit the window's edge sits exactly on the picture's edge — one
    // more pixel would expose black the export never has.
    expect(pic.left + pic.w).toBeGreaterThanOrEqual(win.left + win.w - 0.01)
    expect(pic.top).toBeLessThanOrEqual(win.top + 0.01)
  })

  it('a same-aspect source cannot pan at all until it is zoomed', () => {
    // The most common upload: a 9:16 phone clip on a 9:16 canvas.
    const same = { ...BASE, srcW: 1080, srcH: 1920 }
    const flat = cropLayout(same)
    expect(flat.limit.x).toBeLessThan(1)
    expect(flat.limit.y).toBeLessThan(1)
    expect(cropLayout({ ...same, x: 500, y: 500 }).clamped).toEqual({ x: 0, y: 0 })
    // ...but zooming in creates real margin on both axes.
    const zoomed = cropLayout({ ...same, scale: 2 })
    expect(zoomed.limit.x).toBeGreaterThan(100)
    expect(zoomed.limit.y).toBeGreaterThan(100)
  })

  it('mirrors the renderer\'s max(1, scale) when panning', () => {
    // compositor.py's cover-with-pan branch uses extra_zoom = max(1, scale):
    // a sub-1 zoom there would leave `crop` with less input than output and
    // render solid black, so it is ignored. Drawing the raw scale showed a
    // half-size picture with black inside the yellow frame.
    const small = cropLayout({ ...BASE, scale: 0.5, x: 300 })
    const unity = cropLayout({ ...BASE, scale: 1, x: 300 })
    expect(small.pic.w).toBeCloseTo(unity.pic.w, 6)
    expect(small.pic.h).toBeCloseTo(unity.pic.h, 6)
    // The picture must still cover the window — that is what cover means.
    expect(small.pic.w).toBeGreaterThanOrEqual(small.win.w - 0.01)
    expect(small.pic.h).toBeGreaterThanOrEqual(small.win.h - 0.01)
  })

  it('survives degenerate inputs instead of emitting NaN', () => {
    for (const bad of [
      { ...BASE, srcW: 0, srcH: 0 },      // dims not probed yet
      { ...BASE, paneW: 0, paneH: 0 },    // pane not laid out yet
      { ...BASE, canvasW: 0 },
    ]) {
      const r = cropLayout(bad)
      for (const n of [r.winScale, r.win.w, r.win.h, r.pic.w, r.pic.h,
        r.pic.left, r.pic.top, r.clamped.x, r.clamped.y]) {
        expect(Number.isFinite(n)).toBe(true)
      }
    }
  })
})

describe('dragToOffset', () => {
  it('follows the pointer on both axes', () => {
    const r = dragToOffset({ x: 0, y: 0 }, { dx: 40, dy: -25 }, 0.1)
    expect(r.x).toBeGreaterThan(0)
    expect(r.y).toBeLessThan(0)
  })

  it('is the exact inverse of the layout conversion, within the pan margin', () => {
    // Drag N screen px, and the picture must move N screen px — no more (an
    // earlier version applied the offset twice) and no less.
    const l = cropLayout(BASE)
    const dragged = dragToOffset({ x: 0, y: 0 }, { dx: 37, dy: 21 }, l.winScale)
    const after = cropLayout({ ...BASE, x: dragged.x, y: dragged.y })
    expect(after.pic.left - l.pic.left).toBeCloseTo(37, 6)
    // ...but ONLY within the margin. This aspect pair (16:9 source, 9:16
    // canvas) has zero vertical margin, so the 21px of downward drag is
    // clamped away — which is exactly what the renderer does, and showing the
    // move anyway is the bug this clamp exists to prevent.
    expect(l.limit.y).toBeLessThan(1)
    expect(after.pic.top - l.pic.top).toBeCloseTo(0, 6)
  })

  it('moves 1:1 on an axis that DOES have margin', () => {
    // Zoom in so both axes have room, then a diagonal drag tracks exactly.
    const zoomed = { ...BASE, scale: 2 }
    const l = cropLayout(zoomed)
    expect(l.limit.y).toBeGreaterThan(50)
    const d = dragToOffset({ x: 0, y: 0 }, { dx: 30, dy: 18 }, l.winScale)
    const after = cropLayout({ ...zoomed, x: d.x, y: d.y })
    expect(after.pic.left - l.pic.left).toBeCloseTo(30, 6)
    expect(after.pic.top - l.pic.top).toBeCloseTo(18, 6)
  })
})
