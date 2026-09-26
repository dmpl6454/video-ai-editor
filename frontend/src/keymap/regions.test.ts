// F6 / ⇧F6 region cycling (LEFT_RAIL_SPEC §4.1): the pure step rule, and the
// DOM walk on a stand-in document.
import { describe, expect, it } from 'vitest'
import { REGIONS, TIMELINE_CANVAS, cycleRegion, focusTimeline, nextRegion } from './regions'

describe('nextRegion', () => {
  const all = [true, true, true, true, true, true]
  it('walks forward and back with wrap-around', () => {
    expect(nextRegion(0, all, 1)).toBe(1)
    expect(nextRegion(5, all, 1)).toBe(0)
    expect(nextRegion(0, all, -1)).toBe(5)
    expect(nextRegion(3, all, -1)).toBe(2)
  })
  it('starts at the first (F6) or the last (⇧F6) region from outside all of them', () => {
    expect(nextRegion(-1, all, 1)).toBe(0)
    expect(nextRegion(-1, all, -1)).toBe(5)
  })
  it('skips unavailable regions (a collapsed tool panel, no rail foot yet)', () => {
    const avail = [true, true, false, true, true, false]
    expect(nextRegion(1, avail, 1)).toBe(3)
    expect(nextRegion(4, avail, 1)).toBe(0)
    expect(nextRegion(0, avail, -1)).toBe(4)
    expect(nextRegion(-1, avail, -1)).toBe(4)
  })
  it('goes nowhere when nothing else can take focus', () => {
    expect(nextRegion(2, [false, false, true], 1)).toBe(-1)
    expect(nextRegion(-1, [false, false], 1)).toBe(-1)
    expect(nextRegion(0, [], 1)).toBe(-1)
  })
})

describe('cycleRegion on a document', () => {
  it('keeps the spec order: top bar, rail, tool panel, Prompt bar, timeline, right panel, rail foot', () => {
    expect(REGIONS.map((r) => r.id)).toEqual(['topbar', 'rail', 'panel', 'prompt', 'timeline', 'right', 'foot'])
  })

  it('the timeline stop lands on the timeline canvas (review RD2: F6 never reached it)', () => {
    const t = REGIONS.find((r) => r.id === 'timeline')!
    expect(t.focus).toBe(TIMELINE_CANVAS)
    expect(TIMELINE_CANVAS).toContain('canvas[aria-label="Timeline"]')
  })

  // A stand-in document: each region root holds one control; `hidden` roots
  // model a collapsed tool panel, absent ones the rail foot before R3.
  function fakeDoc(opts: { hidden?: string[]; absent?: string[]; active: string | null }) {
    const focused: string[] = []
    const control = (id: string) => ({
      id, tabIndex: 0, disabled: false,
      getClientRects: () => ({ length: opts.hidden?.includes(id) ? 0 : 1 }),
      closest: () => null,
      focus: () => { focused.push(id); doc.activeElement = ctl[id] },
    })
    const ctl: Record<string, ReturnType<typeof control>> = {}
    const roots: Record<string, unknown> = {}
    for (const r of REGIONS) {
      if (opts.absent?.includes(r.id)) continue
      ctl[r.id] = control(r.id)
      roots[r.root] = {
        closest: () => (opts.hidden?.includes(r.id) ? {} : null),
        contains: (n: unknown) => n === ctl[r.id],
        querySelectorAll: () => [ctl[r.id]],
      }
      if (r.focus) roots[r.focus] = ctl[r.id]
    }
    const doc = {
      activeElement: opts.active ? ctl[opts.active] : null,
      querySelector: (sel: string) => roots[sel] ?? null,
    }
    return { doc: doc as unknown as Document, focused }
  }

  it('moves to the next and previous region, skipping the collapsed tool panel and the missing foot', () => {
    let f = fakeDoc({ hidden: ['panel'], absent: ['foot'], active: 'rail' })
    expect(cycleRegion(1, f.doc)).toBe('prompt')
    expect(f.focused).toEqual(['prompt'])
    f = fakeDoc({ active: 'prompt' })
    expect(cycleRegion(1, f.doc)).toBe('timeline')
    expect(f.focused).toEqual(['timeline'])
    f = fakeDoc({ hidden: ['panel'], absent: ['foot'], active: 'right' })
    expect(cycleRegion(1, f.doc)).toBe('topbar')
    f = fakeDoc({ absent: ['foot'], active: 'prompt' })
    expect(cycleRegion(-1, f.doc)).toBe('panel')
    f = fakeDoc({ active: null })
    expect(cycleRegion(-1, f.doc)).toBe('foot')
  })
})

describe('focusTimeline', () => {
  it('focuses the timeline canvas when it can take focus, and says so', () => {
    const focused: string[] = []
    const canvas = { tabIndex: 0, disabled: false, getClientRects: () => ({ length: 1 }), closest: () => null, focus: () => focused.push('canvas') }
    const doc = { querySelector: (sel: string) => (sel === TIMELINE_CANVAS ? canvas : null) } as unknown as Document
    expect(focusTimeline(doc)).toBe(true)
    expect(focused).toEqual(['canvas'])
    expect(focusTimeline({ querySelector: () => null } as unknown as Document)).toBe(false)
  })
})
