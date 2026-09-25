// QA-054 (zoom to fit that fits, and zooms out far enough) and QA-055 (zoom
// anchored on the pointer).
import { describe, expect, it } from 'vitest'
import {
  anchoredScroll, clampZoom, fitZoom, sliderToZoom, visibleSpanLabel, zoomToSlider, ZOOM_MAX, ZOOM_MIN,
} from './timelineZoom'

describe('fitZoom', () => {
  it('puts a 40 s timeline end inside a 928 px pane (the old formula overflowed to 1280 px)', () => {
    const laneW = 928 - 80
    const z = fitZoom(40, laneW)
    const endX = 80 + 40 * z
    expect(endX).toBeLessThanOrEqual(928)
    expect(endX).toBeGreaterThan(928 - 40)
  })

  it('fits a 12-minute timeline and an hour — below the old 10 px/s floor', () => {
    expect(720 * fitZoom(720, 848)).toBeLessThanOrEqual(848)
    expect(fitZoom(720, 848)).toBeLessThan(10)
    expect(3600 * fitZoom(3600, 1800)).toBeLessThanOrEqual(1800)
  })

  it('never returns a zoom outside the limits', () => {
    expect(fitZoom(0, 800)).toBe(80)
    expect(fitZoom(0.01, 800)).toBe(ZOOM_MAX)
    expect(fitZoom(1e6, 800)).toBe(ZOOM_MIN)
    expect(clampZoom(Number.NaN)).toBe(80)
  })
})

describe('anchoredScroll', () => {
  it('keeps the time under the pointer fixed across a zoom (25.25 s at x=500)', () => {
    const labelW = 80
    const before = { zoom: 80, scroll: 1600 }
    const viewX = 500
    const t = (before.scroll + viewX - labelW) / before.zoom
    const z2 = 92
    const s2 = anchoredScroll(t, viewX, z2, labelW)
    expect((s2 + viewX - labelW) / z2).toBeCloseTo(25.25, 9)
  })

  it('clamps at the start', () => {
    expect(anchoredScroll(0, 300, 200, 80)).toBe(0)
  })
})

describe('the zoom slider', () => {
  it('is logarithmic and round-trips', () => {
    for (const z of [0.5, 1, 10, 80, 600, 1200]) {
      expect(sliderToZoom(zoomToSlider(z)) / z).toBeCloseTo(1, 1)
    }
    // the middle of the travel is a usable mid zoom, not 600 px/s
    expect(sliderToZoom(500)).toBeGreaterThan(10)
    expect(sliderToZoom(500)).toBeLessThan(50)
  })

  it('labels a zoom as the span an editor sees, never a raw float', () => {
    expect(visibleSpanLabel(29.98500749625187, 848)).toBe('28 s')
    expect(visibleSpanLabel(1.1, 848)).toBe('13 min')
    expect(visibleSpanLabel(600, 848)).toBe('1.4 s')
  })
})
