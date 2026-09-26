// QA-051 hover: empty cuts show their bowtie only near the pointer.
import { describe, expect, it } from 'vitest'
import { drawnCuts, hoveredCut } from './cutHover'

const cuts = [
  { at: 5, cx: 480, cy: 82, hasTransition: false },
  { at: 10, cx: 880, cy: 82, hasTransition: true },
  { at: 12, cx: 1040, cy: 82, hasTransition: false },
]

describe('cut hover', () => {
  it('finds the empty cut within 16 px on the Main video row only', () => {
    expect(hoveredCut(470, 70, cuts, 64, 36)?.at).toBe(5)
    expect(hoveredCut(500, 70, cuts, 64, 36)).toBeNull()        // 20 px away
    expect(hoveredCut(480, 30, cuts, 64, 36)).toBeNull()        // another row
    expect(hoveredCut(880, 82, cuts, 64, 36)).toBeNull()        // transitioned: always drawn anyway
  })
  it('draws transitioned cuts always and an empty one only while hovered', () => {
    expect(drawnCuts(cuts, null).map((c) => c.at)).toEqual([10])
    expect(drawnCuts(cuts, 12).map((c) => c.at)).toEqual([10, 12])
  })
})
