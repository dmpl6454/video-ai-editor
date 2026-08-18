// Pure-logic tests for the shared overlay geometry (no DOM/canvas), matching
// this repo's vitest scope. These cover the round-5 M-01 work: text and
// stickers now share one box shape, one hit test and one set of handles, so a
// regression in either kind shows up here rather than as "resize only works on
// stickers" three releases later.

import { describe, expect, it } from 'vitest'
import {
  boxFromStickerGeom, hitsBody, stickerGeom, toLocal, unsentinel,
  getTextBoxes, publishTextBoxes, getOverlayDrag, setOverlayDrag, paintOrder,
  keyframeTimes, dropSettled, resolveLiveOverride, clampOverlayCentre, pipGeom,
  liveCssTransform, liveCssFilter, colorGradeOf,
  liveVideoCssApplies, mergeLivePipScale,
  type OverlayBox, type StickerClip, type LiveDrag,
} from './overlay'
import { maskCuts, pipIsClientDrawn, pipDrawGeom } from './pipDraw'

const box = (over: Partial<OverlayBox> = {}): OverlayBox => ({
  id: 'b1', kind: 'text', cx: 100, cy: 100, hw: 40, hh: 20, rot: 0,
  x: 100, y: 100, ...over,
})

describe('toLocal / hitsBody', () => {
  it('maps a point into an unrotated box frame', () => {
    const { lx, ly } = toLocal(120, 90, box())
    expect(lx).toBeCloseTo(20)
    expect(ly).toBeCloseTo(-10)
  })

  it('undoes rotation, so the hit box follows the drawn box', () => {
    // 90° box: the point 20px to the RIGHT on screen is 20px DOWN in local.
    const b = box({ rot: Math.PI / 2 })
    const { lx, ly } = toLocal(120, 100, b)
    expect(lx).toBeCloseTo(0)
    expect(ly).toBeCloseTo(-20)
  })

  it('hit-tests the rectangle, not a square', () => {
    expect(hitsBody(139, 100, box())).toBe(true)   // inside on the wide axis
    expect(hitsBody(141, 100, box())).toBe(false)
    expect(hitsBody(100, 119, box())).toBe(true)
    expect(hitsBody(100, 121, box())).toBe(false)
  })
})

describe('boxFromStickerGeom', () => {
  it('produces a square box that matches the drawn glyph size', () => {
    const sk: StickerClip = { id: 's1', src: '/x.png', start: 0, end: 2,
                              transform: { x: 540, y: 960, scale: 1 } }
    const g = stickerGeom(sk, 1, 1080, 1920, 270, 480)
    const b = boxFromStickerGeom('s1', g)
    expect(b.kind).toBe('sticker')
    expect(b.hw).toBe(g.size / 2)
    expect(b.hh).toBe(g.size / 2)
    // Center in EDL-canvas coords is what a drag commits.
    expect(b.x).toBe(540)
    expect(b.y).toBe(960)
  })
})

describe('unsentinel', () => {
  // TextLayer treats these exact values as "no explicit anchor — use the role
  // layout". Committing one would make the drag visibly snap back.
  it('nudges off a sentinel', () => {
    expect(unsentinel(540, [540, 1700])).toBe(541)
    expect(unsentinel(1700, [540, 1700])).toBe(1701)
  })

  it('leaves an ordinary coordinate alone', () => {
    expect(unsentinel(300, [540, 1700])).toBe(300)
  })

  it('is a no-op for stickers, which have no sentinels', () => {
    expect(unsentinel(540, undefined)).toBe(540)
    expect(unsentinel(540, [])).toBe(540)
  })
})

describe('cross-layer channels', () => {
  it('round-trips published text boxes', () => {
    publishTextBoxes([box({ id: 't1' })])
    expect(getTextBoxes().map((b) => b.id)).toEqual(['t1'])
    publishTextBoxes([])
    expect(getTextBoxes()).toEqual([])
  })

  it('round-trips the live drag override', () => {
    expect(getOverlayDrag()).toBeNull()
    setOverlayDrag({ id: 't1', dx: 5, dy: -3, sizeMul: 1 })
    expect(getOverlayDrag()).toEqual({ id: 't1', dx: 5, dy: -3, sizeMul: 1 })
    setOverlayDrag(null)
    expect(getOverlayDrag()).toBeNull()
  })
})

describe('paintOrder', () => {
  // Reported as: "when I select and drag the previous emoji it gets behind the
  // latest one; after placement it works fine as it should". The raise to front
  // rides on the COMMIT, so only the live gesture was painting in stale order.
  const items = [{ id: 'a' }, { id: 'b' }, { id: 'c' }]

  it('hoists the dragged item to the top without disturbing the rest', () => {
    expect(paintOrder(items, 'a').map((i) => i.id)).toEqual(['b', 'c', 'a'])
    expect(paintOrder(items, 'b').map((i) => i.id)).toEqual(['a', 'c', 'b'])
  })

  it('is identity — same array reference — when nothing is being dragged', () => {
    // Runs every rAF frame; the no-drag path must not allocate.
    expect(paintOrder(items, undefined)).toBe(items)
    expect(paintOrder(items, 'c')).toBe(items)      // already on top
    expect(paintOrder(items, 'gone')).toBe(items)   // stale id (clip deleted)
  })

  it('leaves stored order alone', () => {
    paintOrder(items, 'a')
    expect(items.map((i) => i.id)).toEqual(['a', 'b', 'c'])
  })
})

describe('keyframeTimes', () => {
  // Feeds the diamonds the timeline draws on a clip and the panel's readout.
  // Adding a keyframe used to change nothing visible anywhere ("I can't see
  // any keyframe added in the video").
  const kf = (...ts: number[]) => ({ keyframes: ts.map((t) => [t, 1] as [number, number]) })

  it('unions the times across properties, ascending', () => {
    expect(keyframeTimes({ transform: { scale: kf(2, 0), opacity: kf(1) } }))
      .toEqual([0, 1, 2])
  })

  it('collapses times within half a frame', () => {
    // The five properties are keyed together but the floats arrive by
    // different routes; the panel count and the timeline must not disagree.
    expect(keyframeTimes({ transform: { scale: kf(1.5), x: kf(1.5001), y: kf(1.53) } }))
      .toEqual([1.5, 1.53])
  })

  it('ignores plain values, missing transforms and junk', () => {
    expect(keyframeTimes({ transform: { scale: 1.5, opacity: 1 } })).toEqual([])
    expect(keyframeTimes({})).toEqual([])
    expect(keyframeTimes(null)).toEqual([])
    expect(keyframeTimes({ transform: { scale: { keyframes: [[NaN, 1]] } } })).toEqual([])
  })
})

describe('dropSettled', () => {
  // Gates when StickerLayer releases the drag override after a drop. Releasing
  // too early is the reported bug — the sticker "goes back to the place from
  // where it was picked up and then comes to the place where I placed",
  // measured at 233ms of snap-back on a real recording. Never releasing is
  // worse: the overlay would be pinned to the dragged spot permanently.
  const clip = (t: Record<string, unknown>, style?: Record<string, unknown>) =>
    ({ transform: t, ...(style ? { style } : {}) })

  it('waits while the EDL still holds the pre-drag position', () => {
    expect(dropSettled(clip({ x: 359, y: 200 }), { id: 'a', x: 511, y: 200 })).toBe(false)
  })

  it('settles once the committed value arrives', () => {
    expect(dropSettled(clip({ x: 511, y: 200 }), { id: 'a', x: 511, y: 200 })).toBe(true)
  })

  it('tolerates the rounding the commit applies', () => {
    // The drop sends Math.round(511.6) = 512; an exact compare would never
    // settle and the overlay would hang until the timeout on every drag.
    expect(dropSettled(clip({ x: 512, y: 200 }), { id: 'a', x: 511, y: 200 })).toBe(true)
    expect(dropSettled(clip({ x: 514, y: 200 }), { id: 'a', x: 511, y: 200 })).toBe(false)
  })

  it('only checks the fields the drop actually committed', () => {
    // A move commits x/y and says nothing about scale, so an unrelated scale
    // must not hold the override open.
    expect(dropSettled(clip({ x: 10, y: 20, scale: 3 }), { id: 'a', x: 10, y: 20 })).toBe(true)
  })

  it('handles a scale resize and a text size resize', () => {
    expect(dropSettled(clip({ scale: 1.75 }), { id: 'a', scale: 1.75 })).toBe(true)
    expect(dropSettled(clip({ scale: 1.0 }), { id: 'a', scale: 1.75 })).toBe(false)
    expect(dropSettled(clip({}, { size: 96 }), { id: 'a', size: 96 })).toBe(true)
    expect(dropSettled(clip({}, { size: 64 }), { id: 'a', size: 96 })).toBe(false)
  })

  it('never settles on a keyframed value, leaving it to the timeout', () => {
    // A keyframed property is an object, not a number. Guessing which key the
    // drag corresponds to would be worse than a bounded wait.
    const kf = { keyframes: [[0, 1]] }
    expect(dropSettled(clip({ x: kf, y: 20 }), { id: 'a', x: 10, y: 20 })).toBe(false)
  })

  it('does not settle on junk instead of throwing', () => {
    expect(dropSettled(clip({ x: NaN }), { id: 'a', x: 10 })).toBe(false)
    expect(dropSettled({}, { id: 'a', x: 10 })).toBe(false)
    expect(dropSettled(null, { id: 'a', x: 10 })).toBe(false)
  })
})

describe('resolveLiveOverride', () => {
  const move = (x: number, y: number): LiveDrag =>
    ({ id: 'a', mode: 'move', live: { x, y } })
  const resize = (scale: number): LiveDrag =>
    ({ id: 'a', mode: 'resize', live: { scale } })

  it('prefers the live gesture over a pending drop', () => {
    // Both can be set for one frame: onUp nulls the drag and arms the pending
    // in the same synchronous block, but a re-entered gesture must win.
    expect(resolveLiveOverride('a', move(10, 20), { id: 'a', x: 99, y: 99 }))
      .toEqual({ x: 10, y: 20 })
  })

  it('keeps drawing the committed drop once the gesture ends', () => {
    // THE REGRESSION. Without this rung the overlay redraws from its stored
    // position for the dispatch -> ~120ms debounce -> GET /edl window, so a
    // drop visibly bounces back to the pick-up point and then jumps forward.
    // Measured on the packaged build at 18 frames / 274ms.
    expect(resolveLiveOverride('a', null, { id: 'a', x: 822, y: 1100 }))
      .toEqual({ x: 822, y: 1100, scale: undefined })
  })

  it('falls through to the stored value when nothing is live', () => {
    expect(resolveLiveOverride('a', null, null)).toBeUndefined()
  })

  it('only applies to the overlay it belongs to', () => {
    // A second sticker must never inherit the dragged one's coordinates.
    expect(resolveLiveOverride('b', move(10, 20), null)).toBeUndefined()
    expect(resolveLiveOverride('b', null, { id: 'a', x: 10, y: 20 })).toBeUndefined()
  })

  it('does not let a move disturb scale, nor a resize disturb position', () => {
    // Each commit writes one pair of fields; leaking the other would silently
    // pin a value the gesture never touched (and defeat sampleKF fallthrough).
    expect(resolveLiveOverride('a', move(10, 20), null)).toEqual({ x: 10, y: 20 })
    expect(resolveLiveOverride('a', resize(1.75), null)).toEqual({ scale: 1.75 })
  })

  it('carries a scale-only pending drop without inventing x/y', () => {
    const ov = resolveLiveOverride('a', null, { id: 'a', scale: 1.75 })
    expect(ov?.scale).toBe(1.75)
    expect(ov?.x).toBeUndefined()
    expect(ov?.y).toBeUndefined()
  })
})

describe('clampOverlayCentre', () => {
  const canvas = { w: 1920, h: 1080 }

  it('leaves an on-canvas position alone', () => {
    expect(clampOverlayCentre(960, 540, canvas)).toEqual({ x: 960, y: 540 })
    expect(clampOverlayCentre(0, 0, canvas)).toEqual({ x: 0, y: 0 })
    expect(clampOverlayCentre(1920, 1080, canvas)).toEqual({ x: 1920, y: 1080 })
  })

  it('stops a drag past the right edge from stranding a sliver', () => {
    // THE REPORT. A 🤣 sticker is max(w,h)*0.22 = 422px, so its half-width is
    // 211: at x=1900 only ~20px stayed on frame and read as a coloured strip.
    // Clamped to 1920 the centre sits on the edge, leaving 211px visible.
    expect(clampOverlayCentre(1900, 540, canvas).x).toBe(1900)
    expect(clampOverlayCentre(2400, 540, canvas).x).toBe(1920)
    expect(clampOverlayCentre(99999, 540, canvas).x).toBe(1920)
  })

  it('clamps every edge, not just the reported one', () => {
    expect(clampOverlayCentre(-500, 540, canvas).x).toBe(0)
    expect(clampOverlayCentre(960, -80, canvas).y).toBe(0)
    expect(clampOverlayCentre(960, 5000, canvas).y).toBe(1080)
  })

  it('clamps the axes independently', () => {
    expect(clampOverlayCentre(-40, 4000, canvas)).toEqual({ x: 0, y: 1080 })
  })

  it('follows the canvas, not a hardcoded size', () => {
    // A vertical project must clamp to its own frame.
    expect(clampOverlayCentre(1500, 2500, { w: 1080, h: 1920 }))
      .toEqual({ x: 1080, y: 1920 })
  })
})

// ---------------------------------------------------------------------------
// pipGeom vs pip.py — the preview draws the PIP, the export bakes it
// ---------------------------------------------------------------------------

/* The preview render deliberately emits NO PIP picture (the `preview` branch in
 * render/pip.py) so a drag moves real frames instead of waiting on ffmpeg. That
 * makes pipGeom the preview's box and pip.py's the exported one, with no shared
 * source — so the box sizes are pinned here against numbers read off pip.py's
 * actual filtergraph, and the same table is pinned on the Python side by
 * tests/test_pip_overlay.py::test_the_box_pip_py_builds_matches_the_table.
 * Divergence between the two is invisible until someone exports.
 *
 * Canvas 1080x1920 at scale 1 => target_long = 1920 * 0.35 = 672.
 */
const PIP_BOXES: Array<{ name: string; clip: Record<string, unknown>
                         srcAspect: number; w: number; h: number }> = [
  // Default: WIDTH is pinned to 672 and the height follows the source.
  { name: 'plain 16:9', clip: {}, srcAspect: 16 / 9, w: 672, h: 378 },
  // A portrait source is therefore TALLER than the canvas long edge * 0.35.
  { name: 'plain 9:16', clip: {}, srcAspect: 9 / 16, w: 672, h: 1195 },
  // `rounded` does NOT force a square — only `circle` does.
  { name: 'rounded 16:9', clip: { mask: { type: 'rounded' } }, srcAspect: 16 / 9,
    w: 672, h: 378 },
  { name: 'circle', clip: { mask: { type: 'circle' } }, srcAspect: 16 / 9,
    w: 672, h: 672 },
  // cover takes the canvas's shape; on a portrait canvas the long edge is the height.
  { name: 'cover', clip: { fit: 'cover' }, srcAspect: 16 / 9, w: 378, h: 672 },
  // The case the single-aspect formula got wrong: want_square is checked FIRST
  // in pip.py, so a circle beats a cover fit outright.
  { name: 'circle+cover', clip: { mask: { type: 'circle' }, fit: 'cover' },
    srcAspect: 16 / 9, w: 672, h: 672 },
  { name: 'circle scale=2', clip: { mask: { type: 'circle' }, transform: { scale: 2 } },
    srcAspect: 16 / 9, w: 1344, h: 1344 },
]

describe('pipGeom matches pip.py box dimensions', () => {
  for (const c of PIP_BOXES) {
    it(c.name, () => {
      // width/height === canvas, so display px are canvas px and the numbers
      // compare directly against the filtergraph's.
      const g = pipGeom({ start: 0, ...c.clip } as never, 0,
        { w: 1080, h: 1920 }, c.srcAspect, 1080, 1920)
      // +-1: pip.py snaps to even dimensions for yuv420p chroma parity, which
      // is a codec constraint with no meaning on a canvas.
      expect(g.hw * 2).toBeCloseTo(c.w, -0.4)
      expect(g.hh * 2).toBeCloseTo(c.h, -0.4)
    })
  }

  it('framing zoom changes the SOURCE rect, never the box', () => {
    // pip.py scales to cover box*zoom and then crops the box back out, so the
    // element's size on the canvas is unchanged — zooming reframes the picture
    // INSIDE the shape, which is the whole point of the control. Drawn by
    // pipDrawGeom; the box here must not move.
    const plain = pipGeom({ start: 0, mask: { type: 'circle' } } as never, 0,
      { w: 1080, h: 1920 }, 16 / 9, 1080, 1920)
    const zoomed = pipGeom({ start: 0, mask: { type: 'circle' },
      framing: { zoom: 2 } } as never, 0, { w: 1080, h: 1920 }, 16 / 9, 1080, 1920)
    expect(zoomed.hw).toBe(plain.hw)
    expect(zoomed.hh).toBe(plain.hh)
  })
})

describe('maskCuts mirrors pip.py _shape_alpha_expr', () => {
  // pip.py implements circle and rounded for a PIP and NOTHING else: rectangle
  // deliberately (it is the frame's own shape) and linear/mirror/heart/star
  // because render_mask_png is v1-only. It emits no alpha for those, so the PIP
  // stays a full rectangle — and the client must agree, or preview and export
  // show different shapes.
  it('cuts only the two implemented shapes', () => {
    expect(maskCuts({ type: 'circle' })).toBe(true)
    expect(maskCuts({ type: 'rounded' })).toBe(true)
    for (const t of ['rectangle', 'linear', 'mirror', 'heart', 'star', '']) {
      expect(maskCuts({ type: t })).toBe(false)
    }
    expect(maskCuts(null)).toBe(false)
    expect(maskCuts(undefined)).toBe(false)
  })

  it('does not let invert resurrect an unimplemented shape', () => {
    // pip.py returns None BEFORE it looks at invert, so an inverted rectangle
    // bakes fully visible. Cutting an even-odd hole here would blank the PIP in
    // preview while it exported intact — a divergence in the safe-looking
    // direction, which is the kind that ships.
    expect(maskCuts({ type: 'rectangle', invert: true })).toBe(false)
    expect(maskCuts({ type: 'star', invert: true })).toBe(false)
    expect(maskCuts({ type: 'circle', invert: true })).toBe(true)
  })
})

describe('pipIsClientDrawn', () => {
  it('hands back every PIP except a chromakeyed one', () => {
    expect(pipIsClientDrawn({})).toBe(true)
    expect(pipIsClientDrawn({ chromakey: null })).toBe(true)
    expect(pipIsClientDrawn({ chromakey: undefined })).toBe(true)
    // A per-pixel key is the one stage a 2D canvas cannot do at 60Hz, so that
    // clip stays baked and keeps being visually true.
    expect(pipIsClientDrawn({ chromakey: { color: '#00FF00' } })).toBe(false)
  })
})

describe('liveCssTransform', () => {
  // Reported as: rotating to an angle works, rotating AGAIN from that angle does
  // not — "it doesn't work according to the angle rotation… when I leave the
  // rotation toggle, it works fine, the issue is only with the preview". The
  // sliders publish absolute values; CSS composes with the baked frame.
  const baked = (o: Partial<{ scale: number; rotation: number; opacity: number }> = {}) =>
    ({ scale: 1, rotation: 0, opacity: 1, ...o })

  it('passes an absolute value straight through on a virgin clip', () => {
    // The FIRST drag is why this shipped: baked is identity, so absolute and
    // relative agree and the bug is invisible.
    const r = liveCssTransform({ rotation: -16 }, baked())
    expect(r.rotateDeg).toBe(-16)
    expect(liveCssTransform({ scale: 1.5 }, baked()).scaleMul).toBe(1.5)
  })

  it('subtracts the baked angle on the SECOND rotation', () => {
    // The reported case: -16 already baked, dragging to -30 must rotate the
    // visible frame a further -14, not -30 (which read as -46).
    expect(liveCssTransform({ rotation: -30 }, baked({ rotation: -16 })).rotateDeg).toBe(-14)
    // Rotating back toward zero has to go the other way.
    expect(liveCssTransform({ rotation: 0 }, baked({ rotation: -16 })).rotateDeg).toBe(16)
    // Same angle as baked = nothing to do, not a doubling.
    expect(liveCssTransform({ rotation: -16 }, baked({ rotation: -16 })).rotateDeg).toBe(0)
  })

  it('divides for scale, because CSS scale MULTIPLIES what is baked', () => {
    expect(liveCssTransform({ scale: 2 }, baked({ scale: 2 })).scaleMul).toBe(1)
    expect(liveCssTransform({ scale: 3 }, baked({ scale: 1.5 })).scaleMul).toBe(2)
    expect(liveCssTransform({ scale: 1 }, baked({ scale: 2 })).scaleMul).toBe(0.5)
  })

  it('leaves a property the gesture never touched alone', () => {
    // A rotation drag must not also renormalise the baked scale, or dragging one
    // slider would visibly move the others.
    const r = liveCssTransform({ rotation: -30 }, baked({ rotation: -16, scale: 2, opacity: 0.5 }))
    expect(r.scaleMul).toBe(1)
    expect(r.opacityMul).toBe(1)
    expect(r.dx).toBe(0)
    expect(r.dy).toBe(0)
  })

  it('keeps dx/dy as the deltas they already were', () => {
    // StickerLayer's on-canvas video drag publishes deltas, and always did —
    // only the three sliders were absolute.
    const r = liveCssTransform({ dx: 40, dy: -12 }, baked({ rotation: -16 }))
    expect(r.dx).toBe(40)
    expect(r.dy).toBe(-12)
    expect(r.rotateDeg).toBe(0)
  })

  it('clamps opacity into what CSS can express', () => {
    expect(liveCssTransform({ opacity: 0.25 }, baked({ opacity: 0.5 })).opacityMul).toBe(0.5)
    // CSS opacity cannot exceed 1, so a dim baked frame cannot be brightened —
    // clamp rather than emit an invalid style (which drops the transform whole).
    expect(liveCssTransform({ opacity: 1 }, baked({ opacity: 0.5 })).opacityMul).toBe(1)
  })

  it('never emits Infinity or NaN from a degenerate baked value', () => {
    // scale 0 / opacity 0 carry no picture to recover, but an Infinity in the
    // style string silently kills the whole live preview.
    for (const r of [
      liveCssTransform({ scale: 2 }, baked({ scale: 0 })),
      liveCssTransform({ opacity: 1 }, baked({ opacity: 0 })),
    ]) {
      expect(Number.isFinite(r.scaleMul)).toBe(true)
      expect(Number.isFinite(r.opacityMul)).toBe(true)
      expect(Number.isFinite(r.rotateDeg)).toBe(true)
    }
  })
})

describe('colorGradeOf / liveCssFilter', () => {
  // The colour twin of the rotation bug, three lines away in the same style
  // block: absolute eq params applied as absolute CSS filters over an
  // already-graded frame, so every drag after the first compounded.
  const clip = (params?: Record<string, number>) =>
    params ? { effects: [{ type: 'color', params }] } : {}

  it('reads the grade, defaulting to eq identity', () => {
    expect(colorGradeOf(clip())).toEqual({ brightness: 0, contrast: 1, saturation: 1 })
    expect(colorGradeOf(clip({ brightness: 0.2, contrast: 1.4, saturation: 0.8 })))
      .toEqual({ brightness: 0.2, contrast: 1.4, saturation: 0.8 })
    // `sat` is the backend's other accepted spelling for the same param.
    expect(colorGradeOf(clip({ sat: 1.5 })).saturation).toBe(1.5)
    expect(colorGradeOf(clip({}))).toEqual({ brightness: 0, contrast: 1, saturation: 1 })
    expect(colorGradeOf(null)).toEqual({ brightness: 0, contrast: 1, saturation: 1 })
    // color_grade is the other effect type name in use.
    expect(colorGradeOf({ effects: [{ type: 'color_grade', params: { contrast: 2 } }] }).contrast).toBe(2)
  })

  const id = { brightness: 0, contrast: 1, saturation: 1 }

  it('passes through on an ungraded clip', () => {
    const r = liveCssFilter({ brightness: 0.2, contrast: 1.5, saturation: 0.5 }, id)
    expect(r.brightnessMul).toBeCloseTo(1.2)
    expect(r.contrastMul).toBe(1.5)
    expect(r.saturateMul).toBe(0.5)
  })

  it('divides out an already-baked grade', () => {
    // Same value as baked => no change, not a doubling.
    const same = liveCssFilter({ brightness: 0.2, contrast: 1.5, saturation: 0.5 },
      { brightness: 0.2, contrast: 1.5, saturation: 0.5 })
    expect(same.brightnessMul).toBeCloseTo(1)
    expect(same.contrastMul).toBeCloseTo(1)
    expect(same.saturateMul).toBeCloseTo(1)
    // A further push is the remaining ratio: contrast 1.5 baked, want 3.0 => 2.0.
    expect(liveCssFilter({ contrast: 3 }, { ...id, contrast: 1.5 }).contrastMul).toBeCloseTo(2)
    // And pulling back below the baked value goes under 1.
    expect(liveCssFilter({ saturation: 0.5 }, { ...id, saturation: 1.5 }).saturateMul).toBeCloseTo(1 / 3)
  })

  it('leaves an untouched channel at 1', () => {
    const r = liveCssFilter({ contrast: 2 }, { brightness: 0.3, contrast: 1, saturation: 2 })
    expect(r.brightnessMul).toBe(1)
    expect(r.saturateMul).toBe(1)
  })

  it('stays finite and non-negative on degenerate input', () => {
    // saturation 0 is a REACHABLE baked value — the slider's own minimum is 0 —
    // and an Infinity in the filter string drops the whole live preview.
    for (const r of [
      liveCssFilter({ saturation: 1 }, { ...id, saturation: 0 }),
      liveCssFilter({ brightness: 0 }, { ...id, brightness: -1 }),
      liveCssFilter({ contrast: 1 }, { ...id, contrast: 0 }),
    ]) {
      expect(Number.isFinite(r.brightnessMul)).toBe(true)
      expect(Number.isFinite(r.contrastMul)).toBe(true)
      expect(Number.isFinite(r.saturateMul)).toBe(true)
      expect(r.brightnessMul).toBeGreaterThanOrEqual(0)
      expect(r.contrastMul).toBeGreaterThanOrEqual(0)
      expect(r.saturateMul).toBeGreaterThanOrEqual(0)
    }
  })
})

describe('pipDrawGeom source rect (measured against ffmpeg)', () => {
  // The companion half of tests/test_pip_framing_matches_client.py, which renders
  // these same cases through REAL ffmpeg on a source that encodes its own
  // coordinates, and asserts the baked crop matches. That test necessarily
  // transcribes this function into Python; this one asserts the actual
  // TypeScript against the identical table, so the transcription cannot drift
  // from the code it stands in for.
  //
  // Source is 640x360 throughout, and the box sizes are the ones pip.py emits
  // for a 1080x1920 canvas at scale 1 (long edge 672, even-snapped).
  const near = (a: number, b: number) => expect(Math.abs(a - b)).toBeLessThan(0.51)

  it('circle: the largest square in the source, centred', () => {
    const g = pipDrawGeom(640, 360, 672, 672, null)
    near(g.sx, 140); near(g.sy, 0); near(g.sw, 360); near(g.sh, 360)
  })

  it('cover on a portrait canvas: a 9:16 slice of a 16:9 source', () => {
    const g = pipDrawGeom(640, 360, 378, 672, null)
    near(g.sx, 218.75); near(g.sy, 0); near(g.sw, 202.5); near(g.sh, 360)
  })

  it('zoom halves the sampled rect and keeps it centred', () => {
    const g = pipDrawGeom(640, 360, 672, 672, { zoom: 2 })
    near(g.sx, 230); near(g.sy, 90); near(g.sw, 180); near(g.sh, 180)
  })

  it('pan runs to the source edges, and the RIGHT way round', () => {
    // An inverted sign swaps these two and nothing else would notice — the
    // picture would just travel the wrong way under an alt-drag.
    const right = pipDrawGeom(640, 360, 672, 672, { zoom: 2, x: 1 })
    const left = pipDrawGeom(640, 360, 672, 672, { zoom: 2, x: -1 })
    near(right.sx, 460)   // flush to the source's right edge (640 - 180)
    near(left.sx, 0)      // flush to its left
    expect(right.sx).toBeGreaterThan(left.sx)
  })

  it('clamps a pan that has no headroom to a no-op', () => {
    // At zoom 1 a cover crop already uses the full height, so a vertical pan has
    // nowhere to go — ffmpeg's `crop` pins x/y into [0, in-out] itself, and the
    // client must agree or it would drag black in that the export never shows.
    const a = pipDrawGeom(640, 360, 672, 672, { y: -1 })
    const b = pipDrawGeom(640, 360, 672, 672, { y: 1 })
    near(a.sy, 0); near(b.sy, 0)
  })

  it('treats zoom below 1 as 1', () => {
    // pip.py's cover_w = max(box_w, round(box_w*zoom)) floors it the same way;
    // a sub-1 zoom would ask crop for more input than exists.
    const z = pipDrawGeom(640, 360, 672, 672, { zoom: 0.25 })
    const one = pipDrawGeom(640, 360, 672, 672, { zoom: 1 })
    expect(z).toEqual(one)
  })
})

// --- a live Transform slider must move only its OWN element ----------------
//
// Reported as "when I lower the opacity for pip, the main video's opacity got
// also lower". One cause, three sliders: Properties' Transform section is shared
// by v1 clips and PIPs, and Preview.tsx applied its in-flight value as CSS on
// the <video> for BOTH. That is right for v1 (the <video> is that clip's
// picture) and wrong for a PIP, whose picture the browser draws separately and
// which is deliberately absent from the render.

describe('liveVideoCssApplies', () => {
  it('allows the CSS stand-in for a v1 clip', () => {
    expect(liveVideoCssApplies('v1')).toBe(true)
  })

  it('refuses it for every PIP lane — the reported bug', () => {
    // v2+ are video tracks TOO, so a "is this a video track" test would have
    // passed them straight through. The lane id is the whole distinction.
    for (const id of ['v2', 'v3', 'v4']) {
      expect(liveVideoCssApplies(id)).toBe(false)
    }
  })

  it('refuses it for overlay lanes and for an unresolved clip', () => {
    // Declining to preview costs a nicety; applying it to the wrong element
    // is a visibly wrong picture, so an unknown lane must fail closed.
    for (const id of ['tx_super', 'stickers', 'captions', null, undefined, '']) {
      expect(liveVideoCssApplies(id)).toBe(false)
    }
  })
})

describe('mergeLivePipScale', () => {
  it('passes the drag override straight through when no slider is live', () => {
    const ov = { x: 10, y: 20 }
    expect(mergeLivePipScale(ov, undefined)).toBe(ov)   // identity, not a copy
    expect(mergeLivePipScale(undefined, undefined)).toBeUndefined()
  })

  it('supplies the slider scale when there is no drag at all', () => {
    expect(mergeLivePipScale(undefined, 1.8)).toEqual({ scale: 1.8 })
  })

  it('COMPOSES: a drag giving only x/y keeps them while the slider scales', () => {
    // The bug this guards: replacing the override wholesale would drop the
    // live x/y and snap the PIP back to its stored position mid-gesture.
    expect(mergeLivePipScale({ x: 10, y: 20 }, 1.8)).toEqual({ x: 10, y: 20, scale: 1.8 })
  })

  it('gives the DRAG precedence when both name scale', () => {
    // A pointer resize is the more direct manipulation of the two.
    expect(mergeLivePipScale({ scale: 2 }, 1.8)).toEqual({ scale: 2 })
  })

  it('treats scale 0 as a real value, not as absent', () => {
    // `??` not `||` — a falsy-but-present scale must survive.
    expect(mergeLivePipScale(undefined, 0)).toEqual({ scale: 0 })
    expect(mergeLivePipScale({ scale: 0 }, 1.8)).toEqual({ scale: 0 })
  })
})
