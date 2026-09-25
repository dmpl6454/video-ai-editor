// QA-051 (the transition bowtie stole the trim zone) and QA-052 (a crosshair
// everywhere: no move / trim / scrub cursors).
import { describe, expect, it } from 'vitest'
import { cursorFor, hitTest, type CutMark, type HitBox, type HitGeometry } from './timelineHit'

const G: HitGeometry = { labelWidth: 80, rulerTop: 0, rulerHeight: 24, playheadX: 80, clipInset: 4 }
// Two abutting v1 clips cut at x = 880 (10 s at 80 px/s), row at y 24..60.
const A: HitBox = { trackId: 'v1', clipId: 'a', x: 80, y: 24, w: 800, h: 36, locked: false }
const B: HitBox = { trackId: 'v1', clipId: 'b', x: 880, y: 24, w: 800, h: 36, locked: false }
const CUT: CutMark = { at: 10, cx: 880, cy: 42, hasTransition: false }

describe('hitTest at a cut', () => {
  it('a grab at mid height just left of the cut trims the outgoing clip, carrying the cut', () => {
    const h = hitTest(878, 42, G, [A, B], [CUT])
    expect(h.kind).toBe('trim-r')
    if (h.kind === 'trim-r') { expect(h.box.clipId).toBe('a'); expect(h.cut?.at).toBe(10) }
  })

  it('just right of the cut trims the incoming clip', () => {
    const h = hitTest(882, 42, G, [A, B], [CUT])
    expect(h.kind).toBe('trim-l')
    if (h.kind === 'trim-l') expect(h.box.clipId).toBe('b')
  })

  it('the part of the bowtie outside both trim zones is the transition target', () => {
    // 7 px right of the cut: past B's 6 px zone, still inside the r=8 circle.
    expect(hitTest(887, 42, G, [A, B], [CUT]).kind).toBe('transition')
  })
})

describe('hitTest elsewhere', () => {
  it('ruler, playhead line, body, empty lane and label column', () => {
    expect(hitTest(300, 10, G, [A, B], []).kind).toBe('ruler')
    expect(hitTest(300, 42, G, [], []).kind).toBe('empty')
    expect(hitTest(400, 42, G, [A], []).kind).toBe('move')
    expect(hitTest(40, 42, G, [A], []).kind).toBe('label')
    expect(hitTest(603, 70, { ...G, playheadX: 600 }, [A], []).kind).toBe('playhead')
  })

  it('a trim zone beats the playhead line sitting on the same edge', () => {
    expect(hitTest(878, 42, { ...G, playheadX: 880 }, [A, B], []).kind).toBe('trim-r')
  })

  it('a locked clip is never a trim target', () => {
    const h = hitTest(878, 42, G, [{ ...A, locked: true }], [])
    expect(h.kind).toBe('move')
    expect(cursorFor(h)).toBe('not-allowed')
  })

  it('a 6 px sliver keeps a body to grab', () => {
    const s: HitBox = { ...A, w: 6 }
    expect(hitTest(83, 42, { ...G, playheadX: 600 }, [s], []).kind).toBe('move')
  })
})

describe('cursorFor', () => {
  it('says what a press would do', () => {
    expect(cursorFor(hitTest(400, 42, G, [A], []))).toBe('grab')
    expect(cursorFor(hitTest(400, 42, G, [A], []), true)).toBe('grabbing')
    expect(cursorFor(hitTest(82, 42, G, [A], []))).toBe('ew-resize')
    expect(cursorFor(hitTest(300, 10, G, [A], []))).toBe('col-resize')
    expect(cursorFor(hitTest(887, 42, G, [A, B], [CUT]))).toBe('pointer')
    expect(cursorFor(hitTest(300, 70, G, [], []))).toBe('default')
  })
})
