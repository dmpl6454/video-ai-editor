// The render-clock adapters StickerLayer's draw loop goes through
// (lib/timelineLayout: activeInRender / renderLocal / layoutClock) — the sign
// and the drop rule, pinned in node (vitest runs without a DOM, so the canvas
// effect itself is not exercised).
//
// The defect this guards: stickers and PiP clips were tested and animated with
// their LAYOUT `start` against the <video>'s RENDER clock. After a v1
// transition the two differ by the overlap the crossfade consumed, so an
// overlay appeared late by the accumulated overlap (up to 2.4 s on the
// reported session) and a PiP's footage began that far into itself. Every
// expectation below is a layout instant that the OLD comparison would have
// answered differently — that is what makes each one a regression test rather
// than a restatement of `renderTime`.
import { describe, expect, it } from 'vitest'
import {
  activeInRender, layoutClock, renderLocal, renderTime, seamTable, type LayoutClip,
} from '../lib/timelineLayout'

const clip = (id: string, start: number, duration: number): LayoutClip => ({ id, start, duration })

/** The four 2s clips from the real report, transitions at 2.0 and 4.0 (d=0.5). */
const FOUR = [clip('a', 0, 2), clip('b', 2, 2), clip('c', 4, 2), clip('d', 6, 2)]
const SEAMS = seamTable(FOUR, [{ at: 2, duration: 0.5 }, { at: 4, duration: 0.5 }])
const NONE = seamTable(FOUR, [])

describe('activeInRender', () => {
  it('is the plain layout test when there are no transitions', () => {
    expect(activeInRender(NONE, 3, 3.5, 3.2, true)).toBe(true)
    expect(activeInRender(NONE, 3, 3.5, 2.9, true)).toBe(false)
    expect(activeInRender(NONE, 3, 3.5, 3.6, true)).toBe(false)
  })

  it('pulls an overlay after a seam left by the consumed overlap', () => {
    // Sticker on clip b, layout [3.0, 3.5]. One 0.5s seam precedes it, so it
    // plays in render [2.5, 3.0].
    expect(activeInRender(SEAMS, 3, 3.5, 2.6, true)).toBe(true)
    // Layout time 3.2 is INSIDE the sticker's own window — the old test said
    // "active" here, and that is exactly the late-by-0.5s drift.
    expect(activeInRender(SEAMS, 3, 3.5, 3.2, true)).toBe(false)
    // Render time 2.4 is before the sticker in both coordinate systems.
    expect(activeInRender(SEAMS, 3, 3.5, 2.4, true)).toBe(false)
  })

  it('accumulates every seam at or before the overlay', () => {
    // On clip c, after both seams: layout [4.5, 5] → render [3.5, 4].
    expect(activeInRender(SEAMS, 4.5, 5, 3.75, false)).toBe(true)
    expect(activeInRender(SEAMS, 4.5, 5, 4.75, false)).toBe(false)
  })

  it('shrinks a window that crosses a seam by that seam\'s overlap', () => {
    // Layout [1.0, 3.0] straddles the seam at 2.0 (d=0.5) → render
    // [1.0, 2.5]: 2 s of layout becomes 1.5 s on screen, because its middle
    // half-second was consumed by the crossfade.
    expect(activeInRender(SEAMS, 1, 3, 2.4, true)).toBe(true)
    // 2.6 is still inside the LAYOUT window — the old test said "active".
    expect(activeInRender(SEAMS, 1, 3, 2.6, true)).toBe(false)
  })

  it('never shows a window the renderer drops', () => {
    // Exactly clip a's consumed tail: render_time(1.5) === render_time(2.0).
    expect(renderTime(SEAMS, 1.5)).toBeCloseTo(renderTime(SEAMS, 2.0), 9)
    for (const t of [1.4, 1.5, 1.75, 2.0]) {
      expect(activeInRender(SEAMS, 1.5, 2.0, t, true)).toBe(false)
    }
  })

  it('keeps each caller\'s end-edge rule', () => {
    // Render window [2.5, 3.0]: a sticker (between() semantics) is still on at
    // its last instant, a PiP is not.
    expect(activeInRender(SEAMS, 3, 3.5, 3.0, true)).toBe(true)
    expect(activeInRender(SEAMS, 3, 3.5, 3.0, false)).toBe(false)
  })
})

describe('renderLocal / layoutClock', () => {
  it('are the identity without transitions', () => {
    expect(renderLocal(NONE, 4, 4.25)).toBeCloseTo(0.25, 9)
    expect(layoutClock(NONE, 4, 4.25)).toBeCloseTo(4.25, 9)
  })

  it('measure a PiP\'s progress from where the renderer starts it', () => {
    // PiP at layout 4.0 (after both seams) starts playing at render 3.0. At
    // render 3.25 it is 0.25s in — the old `t - start` said −0.75s, i.e. the
    // footage clamped to its first frame for three quarters of a second.
    expect(renderTime(SEAMS, 4)).toBeCloseTo(3.0, 9)
    expect(renderLocal(SEAMS, 4, 3.25)).toBeCloseTo(0.25, 9)
  })

  it('give a helper that subtracts the layout start the same local time', () => {
    // stickerGeom/pipGeom compute `clock - start` themselves; layoutClock is
    // defined so that equals renderLocal — one rule, two call shapes.
    for (const [start, t] of [[4, 3.25], [3, 2.6], [0.5, 0.5], [6, 5.9]]) {
      expect(layoutClock(SEAMS, start, t) - start).toBeCloseTo(renderLocal(SEAMS, start, t), 9)
    }
  })

  it('agrees with the media offset syncPipVideo will compute', () => {
    // syncPipVideo wants `in + (t - start)`; we hand it renderTime(start), so
    // the offset must be `in + renderLocal`.
    const inPoint = 1.5
    const want = inPoint + (3.25 - renderTime(SEAMS, 4))
    expect(want).toBeCloseTo(inPoint + renderLocal(SEAMS, 4, 3.25), 9)
  })
})
