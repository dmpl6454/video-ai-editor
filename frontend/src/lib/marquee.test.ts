// QA-116: a drag on empty lane space selects every clip its box touches.
import { describe, expect, it } from 'vitest'
import { isMarqueeDrag, marqueeHits, marqueeRect } from './marquee'

const boxes = [
  { clipId: 'a', x: 100, y: 24, w: 80, h: 36 },
  { clipId: 'b', x: 200, y: 24, w: 80, h: 36 },
  { clipId: 't', x: 150, y: 64, w: 40, h: 36 },
]

describe('marquee', () => {
  it('normalises a box dragged up and to the left', () => {
    expect(marqueeRect(300, 90, 120, 30)).toEqual({ x0: 120, y0: 30, x1: 300, y1: 90 })
  })
  it('selects the clips the box touches, across rows', () => {
    expect(marqueeHits(marqueeRect(170, 40, 210, 80), boxes, 4)).toEqual(['a', 'b', 't'])
    expect(marqueeHits(marqueeRect(185, 30, 195, 50), boxes, 4)).toEqual([])
  })
  it('uses the drawn rect: the row inset above a clip does not select it', () => {
    // y 60..63 is between the rows' drawn rects (24+36-4 = 56 .. 64+4 = 68)
    expect(marqueeHits(marqueeRect(150, 57, 190, 67), boxes, 4)).toEqual([])
  })
  it('a press that barely moves is a click, not a box', () => {
    expect(isMarqueeDrag(10, 10, 12, 11)).toBe(false)
    expect(isMarqueeDrag(10, 10, 14, 13)).toBe(true)
  })
})
