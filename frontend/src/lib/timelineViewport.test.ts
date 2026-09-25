import { describe, expect, it } from 'vitest'
import { contentTransform, spanVisible, viewportCanvasSize, visibleColumns, visibleTicks } from './timelineViewport'

// Chromium refuses a 2D canvas wider than this; WKWebView has an area limit.
const CHROMIUM_MAX = 32767

describe('viewportCanvasSize (QA-024)', () => {
  it('a 12-min timeline at max zoom and dpr 2 stays far inside the canvas limit', () => {
    const zoom = 600, dur = 720, labelWidth = 80
    const contentW = labelWidth + (dur + 30) * zoom          // 450,080 CSS px
    const vs = viewportCanvasSize(1400, contentW, 300, 2)
    expect(vs.pxW).toBe(2800)
    expect(vs.pxW).toBeLessThan(CHROMIUM_MAX)
    expect(vs.pxW * vs.pxH).toBeLessThan(16_777_216)
  })

  it('a short timeline never exceeds its own content', () => {
    expect(viewportCanvasSize(1400, 900, 200, 1)).toEqual({ cssW: 900, cssH: 200, pxW: 900, pxH: 200 })
  })

  it('rounds the backing store at a fractional dpr and never returns 0', () => {
    expect(viewportCanvasSize(333, 1000, 101, 1.5)).toMatchObject({ pxW: 500, pxH: 152 })
    expect(viewportCanvasSize(0, 0, 0, 2)).toMatchObject({ pxW: 2, pxH: 2 })
  })
})

describe('contentTransform', () => {
  it('maps content x to viewport device px', () => {
    const [a, , , d, e] = contentTransform(2, 1000)
    const xContent = 1250
    expect(a * xContent + e).toBe(500)   // (1250 - 1000) CSS px * 2
    expect(d).toBe(2)
  })
})

describe('visibleColumns / spanVisible', () => {
  it('limits a huge clip to the on-screen columns', () => {
    // clip from content x=80, 432,000 px wide; viewport 100,000..101,400
    expect(visibleColumns(80, 432_000, 100_000, 101_400)).toEqual([99_920, 101_320])
  })
  it('is empty for a clip entirely off-screen', () => {
    const [a, b] = visibleColumns(5000, 100, 0, 1400)
    expect(b - a).toBe(0)
    expect(spanVisible(5000, 100, 0, 1400)).toBe(false)
    expect(spanVisible(1395, 100, 0, 1400)).toBe(true)
  })
})

describe('visibleTicks', () => {
  it('starts at the first tick at/left of the viewport, on the global grid', () => {
    const ticks = visibleTicks(10_080, 10_400, 80, 100, 1, 1000)
    expect(ticks[0]).toBe(100)            // x = 80 + 100*100 = 10,080
    expect(ticks.at(-1)).toBe(103)        // x = 10,380 ≤ 10,400
  })
  it('stops at the timeline end and never drifts', () => {
    const t = visibleTicks(0, 1e9, 80, 0.5, 0.1, 50)
    expect(t.length).toBe(501)
    expect(t.at(-1)).toBeCloseTo(50, 9)
  })
})
