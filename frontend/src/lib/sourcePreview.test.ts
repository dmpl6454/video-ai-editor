// The source-based live transform preview exists because the v1 bake rotates
// IN PLACE and cuts the corners: measured on a 960x540 preview, a 61° render
// keeps only 72.5% of the picture and the two corners that were white at 0°
// read (0,0,0). So a frame already carrying rotation cannot be re-posed by CSS
// — the pixels for any other angle are gone — and the preview has to start from
// the source instead. These pin the geometry and the (deliberately narrow)
// scope of that takeover.

import { describe, expect, it } from 'vitest'
import { planSourceDraw, sourcePreviewApplies } from './sourcePreview'

const CANVAS = { w: 1920, h: 1080 }
const BOX = { w: 960, h: 540 }          // exactly half — k = 0.5

describe('planSourceDraw', () => {
  it('letterboxes a taller-than-canvas source, in box pixels', () => {
    // 1080x1080 into 1920x1080 → fit = 1.0 on height, so 1080x1080 canvas px,
    // which is 540x540 box px. Bars left/right, matching pad=…:color=black.
    const p = planSourceDraw({ w: 1080, h: 1080 }, CANVAS, BOX, {})!
    expect(p.drawW).toBeCloseTo(540)
    expect(p.drawH).toBeCloseTo(540)
  })

  it('fills exactly when the source matches the canvas aspect', () => {
    const p = planSourceDraw({ w: 3840, h: 2160 }, CANVAS, BOX, {})!
    expect(p.drawW).toBeCloseTo(BOX.w)
    expect(p.drawH).toBeCloseTo(BOX.h)
  })

  it('converts degrees to radians', () => {
    expect(planSourceDraw({ w: 1920, h: 1080 }, CANVAS, BOX, { rotation: 61 })!.rotRad)
      .toBeCloseTo((61 * Math.PI) / 180)
    // The reported gesture ends here, and this is the case the composited
    // preview could not show: back to 0 with a frame baked at 61.
    expect(planSourceDraw({ w: 1920, h: 1080 }, CANVAS, BOX, { rotation: 0 })!.rotRad)
      .toBe(0)
  })

  it('clamps zoom to >= 1, exactly as the renderer does', () => {
    // compositor.py uses max(1, scale): a sub-1 zoom leaves `crop` less input
    // than output and bakes solid black, so the preview must not offer it.
    expect(planSourceDraw({ w: 1920, h: 1080 }, CANVAS, BOX, { scale: 0.1 })!.zoom).toBe(1)
    expect(planSourceDraw({ w: 1920, h: 1080 }, CANVAS, BOX, { scale: 1.75 })!.zoom).toBe(1.75)
  })

  it('scales the pan into box pixels and keeps +x moving the picture right', () => {
    const p = planSourceDraw({ w: 1920, h: 1080 }, CANVAS, BOX, { x: 200, y: -100 })!
    expect(p.panX).toBeCloseTo(100)     // 200 canvas px * k(0.5)
    expect(p.panY).toBeCloseTo(-50)
  })

  it('refuses degenerate geometry rather than emitting NaN', () => {
    expect(planSourceDraw({ w: 0, h: 0 }, CANVAS, BOX, {})).toBeNull()
    expect(planSourceDraw({ w: 1920, h: 1080 }, CANVAS, { w: 0, h: 0 }, {})).toBeNull()
  })

  it('survives a non-finite scale', () => {
    expect(planSourceDraw({ w: 1920, h: 1080 }, CANVAS, BOX, { scale: NaN })!.zoom).toBe(1)
  })
})

describe('sourcePreviewApplies', () => {
  const base = { trackId: 'v1', fit: 'contain', bakedRotation: 61,
                 keyframed: false, hasDims: true }

  it('takes over exactly when the frame on screen is already rotated', () => {
    expect(sourcePreviewApplies(base)).toBe(true)
  })

  it('leaves a clip at rotation 0 on the composited preview', () => {
    // That path is correct today AND higher fidelity — it carries the colour
    // grade and every other layer, which the raw source does not.
    expect(sourcePreviewApplies({ ...base, bakedRotation: 0 })).toBe(false)
    expect(sourcePreviewApplies({ ...base, bakedRotation: 0.0005 })).toBe(false)
  })

  it('does not touch cover framing, which CropReposition owns', () => {
    expect(sourcePreviewApplies({ ...base, fit: 'cover' })).toBe(false)
  })

  it('is v1 only', () => {
    expect(sourcePreviewApplies({ ...base, trackId: 'v2' })).toBe(false)
  })

  it('stands aside for a keyframed transform', () => {
    // One drag cannot express a curve — the same rule CropReposition and
    // StickerLayer already apply to an animated value.
    expect(sourcePreviewApplies({ ...base, keyframed: true })).toBe(false)
  })

  it('needs the source dimensions before it can lay anything out', () => {
    expect(sourcePreviewApplies({ ...base, hasDims: false })).toBe(false)
  })

  it('handles a negative baked rotation, not just a positive one', () => {
    expect(sourcePreviewApplies({ ...base, bakedRotation: -61 })).toBe(true)
  })
})
