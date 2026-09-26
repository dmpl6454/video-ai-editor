// QA-117: the wheel does what Help says it does.
import { describe, expect, it } from 'vitest'
import { wheelAction } from './timelineWheel'

const w = (o: Partial<Parameters<typeof wheelAction>[0]>) =>
  ({ deltaX: 0, deltaY: 0, shiftKey: false, ctrlKey: false, metaKey: false, ...o })

describe('wheelAction', () => {
  it('plain wheel scrolls the tracks when they overflow', () => {
    expect(wheelAction(w({ deltaY: 100 }), true)).toEqual({ kind: 'native' })
  })
  it('plain wheel pans when every track already fits', () => {
    expect(wheelAction(w({ deltaY: 100 }), false)).toEqual({ kind: 'pan', dx: 100 })
  })
  it('Shift+wheel pans even when the tracks overflow (and on macOS, where it arrives as deltaX)', () => {
    expect(wheelAction(w({ deltaY: 120, shiftKey: true }), true)).toEqual({ kind: 'pan', dx: 120 })
    expect(wheelAction(w({ deltaX: -80, shiftKey: true }), true)).toEqual({ kind: 'pan', dx: -80 })
  })
  it('a sideways trackpad swipe is left to the browser (native horizontal scroll)', () => {
    expect(wheelAction(w({ deltaX: 40, deltaY: 3 }), true)).toEqual({ kind: 'native' })
    expect(wheelAction(w({ deltaX: 40, deltaY: 3 }), false)).toEqual({ kind: 'native' })
  })
  it('⌘/Ctrl+wheel zooms: a notch is 1.15×, a pinch step is proportional and small', () => {
    const notch = wheelAction(w({ deltaY: -100, metaKey: true }), true)
    expect(notch.kind).toBe('zoom')
    expect((notch as { factor: number }).factor).toBeCloseTo(1.15, 5)
    const pinch = wheelAction(w({ deltaY: 4, ctrlKey: true }), true) as { factor: number }
    expect(pinch.factor).toBeLessThan(1)
    expect(pinch.factor).toBeGreaterThan(0.99)
    const huge = wheelAction(w({ deltaY: -5000, ctrlKey: true }), true) as { factor: number }
    expect(huge.factor).toBe(1.5)
  })
  it('line-mode deltas (Firefox) are scaled to pixels', () => {
    expect(wheelAction(w({ deltaY: 3, deltaMode: 1 }), false)).toEqual({ kind: 'pan', dx: 48 })
  })
})

describe('Help states the wheel rule (QA-117)', async () => {
  const { gestureRows } = await import('./helpShortcuts')
  it('lists pan, scroll, zoom, ⌘-click and box selection', () => {
    const rows = Object.fromEntries(gestureRows(true).map((r) => [r.id, r.keys]))
    expect(rows['gesture:panWheel']).toEqual(['Shift + scroll', 'Swipe sideways'])
    expect(rows['gesture:scrollWheel']).toEqual(['Scroll'])
    expect(rows['gesture:shiftClick']).toContain('⌘-click')
    expect(rows['gesture:marquee']).toBeTruthy()
    // What Help promises is what wheelAction does.
    const base = { deltaX: 0, shiftKey: false, ctrlKey: false, metaKey: false }
    expect(wheelAction({ ...base, deltaY: 100, shiftKey: true }, true).kind).toBe('pan')
    expect(wheelAction({ ...base, deltaY: 100 }, true).kind).toBe('native')
  })
})
