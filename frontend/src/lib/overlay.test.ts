// Pure-logic tests for the shared overlay geometry (no DOM/canvas), matching
// this repo's vitest scope. These cover the round-5 M-01 work: text and
// stickers now share one box shape, one hit test and one set of handles, so a
// regression in either kind shows up here rather than as "resize only works on
// stickers" three releases later.

import { describe, expect, it } from 'vitest'
import {
  boxFromStickerGeom, hitsBody, stickerGeom, toLocal, unsentinel,
  getTextBoxes, publishTextBoxes, getOverlayDrag, setOverlayDrag,
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
