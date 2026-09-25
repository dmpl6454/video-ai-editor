import { describe, expect, it } from 'vitest'
import { rulerLabel } from './rulerLabel'
import { visibleTicks } from './timelineViewport'

// Same step table and pixel spacing as Timeline.tsx's ruler.
const STEPS = [0.1, 0.2, 0.5, 1, 2, 5, 10, 30, 60, 300, 600]
const niceTick = (approx: number) => STEPS.find((c) => c >= approx) ?? 600

describe('rulerLabel', () => {
  it('every tick in view has its own label at every zoom (a 12-min timeline)', () => {
    const labelWidth = 80, dur = 720
    for (const zoom of [10, 40, 80, 160, 200, 300, 450, 600]) {
      const step = niceTick(80 / zoom)
      for (const scroll of [0, 10 * zoom, 58 * zoom, 375 * zoom, 700 * zoom]) {
        const ticks = visibleTicks(scroll, scroll + 1400, labelWidth, zoom, step, dur + 30)
        const labels = ticks.map((t) => rulerLabel(t, step))
        expect(new Set(labels).size, `zoom ${zoom} scroll ${scroll}: ${labels.join(' ')}`).toBe(labels.length)
      }
    }
  })

  it('reads the reported cases correctly', () => {
    expect([58, 58.2, 58.4].map((t) => rulerLabel(t, 0.2))).toEqual(['58.0s', '58.2s', '58.4s'])
    expect([375, 375.2, 375.4, 376].map((t) => rulerLabel(t, 0.2))).toEqual(['6:15.0', '6:15.2', '6:15.4', '6:16.0'])
    expect(rulerLabel(0.30000000000000004, 0.1)).toBe('0.3s')
    expect(rulerLabel(59.99999999, 0.5)).toBe('60.0s')
  })

  it('keeps the old labels at a step of a second or more', () => {
    expect(rulerLabel(1, 1)).toBe('1.0s')
    expect(rulerLabel(12, 1)).toBe('12s')
    expect(rulerLabel(65, 5)).toBe('1:05')
    expect(rulerLabel(600, 60)).toBe('10:00')
  })
})
