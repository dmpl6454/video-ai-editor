// Pure-logic tests for the shared overlay geometry (no DOM/canvas), matching
// this repo's vitest scope. These cover the round-5 M-01 work: text and
// stickers now share one box shape, one hit test and one set of handles, so a
// regression in either kind shows up here rather than as "resize only works on
// stickers" three releases later.

import { describe, expect, it } from 'vitest'
import {
  boxFromStickerGeom, hitsBody, stickerGeom, toLocal, unsentinel,
  getTextBoxes, publishTextBoxes, getOverlayDrag, setOverlayDrag, paintOrder,
  keyframeTimes,
  type OverlayBox, type StickerClip,
} from './overlay'

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
