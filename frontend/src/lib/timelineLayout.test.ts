import { describe, expect, it } from 'vitest'
import {
  clipLocalTime, drawnSpan, edlTimeFromOutput, layoutPlayhead, layoutTime, outStart,
  renderTime, renderWindow, seamTable, v1ClipAt, v1Layout, v1LayoutOf, v1SeamsOf,
  v1TimeFromOutput, type LayoutClip,
} from './timelineLayout'
import type { EDL } from '../types'

const clip = (id: string, start: number, duration: number): LayoutClip =>
  ({ id, start, duration })

/** The four 2s clips from the real report (session s_2cfa632c89). */
const FOUR = [clip('a', 0, 2), clip('b', 2, 2), clip('c', 4, 2), clip('d', 6, 2)]

describe('v1Layout', () => {
  it('shifts nothing when there are no transitions', () => {
    const { shift } = v1Layout(FOUR, [])
    for (const c of FOUR) expect(outStart(c, shift)).toBe(c.start)
  })

  it('reproduces the reported session exactly', () => {
    // 8s split at 2/4/6, transitions at 2.0 and 4.0 only -> renders 7.0s.
    const { shift } = v1Layout(FOUR, [
      { at: 2, duration: 0.5 }, { at: 4, duration: 0.5 },
    ])
    expect(outStart(FOUR[0], shift)).toBeCloseTo(0.0, 6)
    expect(outStart(FOUR[1], shift)).toBeCloseTo(1.5, 6)
    expect(outStart(FOUR[2], shift)).toBeCloseTo(3.0, 6)
    expect(outStart(FOUR[3], shift)).toBeCloseTo(5.0, 6)
    // The last clip must END exactly at the duration the backend reports.
    expect(outStart(FOUR[3], shift) + FOUR[3].duration).toBeCloseTo(7.0, 6)
  })

  it('matches the three-transition case the backend renders as 6.5s', () => {
    const { shift } = v1Layout(FOUR, [
      { at: 2, duration: 0.5 }, { at: 4, duration: 0.5 }, { at: 6, duration: 0.5 },
    ])
    expect(outStart(FOUR[3], shift) + FOUR[3].duration).toBeCloseTo(6.5, 6)
  })

  it('does not charge a transition at a real GAP (the renderer keeps the cut)', () => {
    // clip b starts 1s late: filler goes in, so the transition never applies.
    const gapped = [clip('a', 0, 2), clip('b', 3, 2)]
    const { shift, seams } = v1Layout(gapped, [{ at: 2, duration: 0.5 }])
    expect(outStart(gapped[1], shift)).toBe(3)
    expect(seams[0].overlap).toBe(0)
  })

  it('still charges an OVERLAPPING pair (the renderer packs those)', () => {
    // Testing abs(nxt.start - boundary) would wrongly skip this.
    const over = [clip('a', 0, 2), clip('b', 1.5, 2)]
    const { seams } = v1Layout(over, [{ at: 2, duration: 0.5 }])
    expect(seams[0].overlap).toBeCloseTo(0.5, 6)
  })

  it('never overlaps further than the shorter clip is long', () => {
    const short = [clip('a', 0, 2), clip('b', 2, 0.2)]
    const { shift } = v1Layout(short, [{ at: 2, duration: 1.5 }])
    expect(shift.get('b')).toBeCloseTo(0.2, 6)
  })

  it('matches a transition within 0.05s of the boundary, not beyond', () => {
    const near = v1Layout(FOUR, [{ at: 2.04, duration: 0.5 }])
    expect(near.shift.get('b')).toBeCloseTo(0.5, 6)
    const far = v1Layout(FOUR, [{ at: 2.06, duration: 0.5 }])
    expect(far.shift.get('b')).toBe(0)
  })

  it('accumulates across seams, carrying past an uncharged one', () => {
    // Transition at 2.0 applies; the 4.0 seam has a gap after it, so clip d
    // is still pulled left by the FIRST transition only.
    const withGap = [clip('a', 0, 2), clip('b', 2, 2), clip('c', 5, 2)]
    const { shift } = v1Layout(withGap, [{ at: 2, duration: 0.5 }])
    expect(shift.get('b')).toBeCloseTo(0.5, 6)
    expect(shift.get('c')).toBeCloseTo(0.5, 6)
  })

  it('places the seam affordance at the middle of the overlap', () => {
    const { seams } = v1Layout(FOUR, [{ at: 2, duration: 0.5 }])
    // clip a ends at 2.0 output, clip b starts at 1.5 output -> middle 1.75.
    expect(seams[0].outAt).toBeCloseTo(1.75, 6)
  })

  it('places a hard cut exactly on the boundary', () => {
    const { seams } = v1Layout(FOUR, [])
    expect(seams[0].outAt).toBeCloseTo(2.0, 6)
    expect(seams[1].outAt).toBeCloseTo(4.0, 6)
  })

  it('sorts by start rather than trusting array order', () => {
    const shuffled = [FOUR[2], FOUR[0], FOUR[3], FOUR[1]]
    const { shift } = v1Layout(shuffled, [{ at: 2, duration: 0.5 }])
    expect(shift.get('a')).toBe(0)
    expect(shift.get('b')).toBeCloseTo(0.5, 6)
  })

  it('handles an empty or single-clip track', () => {
    expect(v1Layout([], []).shift.size).toBe(0)
    const one = v1Layout([clip('a', 0, 2)], [{ at: 2, duration: 0.5 }])
    expect(one.shift.get('a')).toBe(0)
    expect(one.seams).toEqual([])
  })

  it('round-trips output time back to EDL time', () => {
    const { shift } = v1Layout(FOUR, [
      { at: 2, duration: 0.5 }, { at: 4, duration: 0.5 },
    ])
    // Each clip's own start must survive the round trip exactly.
    for (const c of FOUR) {
      expect(edlTimeFromOutput(outStart(c, shift), FOUR, shift)).toBeCloseTo(c.start, 6)
    }
    // A point inside clip 4 (output 5.0-7.0) maps back into EDL 6.0-8.0.
    expect(edlTimeFromOutput(6.0, FOUR, shift)).toBeCloseTo(7.0, 6)
    // Past the end, the full accumulated overlap applies.
    expect(edlTimeFromOutput(7.0, FOUR, shift)).toBeCloseTo(8.0, 6)
  })

  it('is the identity when there are no transitions', () => {
    const { shift } = v1Layout(FOUR, [])
    for (const t of [0, 1.3, 5.5, 8]) {
      expect(edlTimeFromOutput(t, FOUR, shift)).toBeCloseTo(t, 6)
    }
  })

  it('respects speed-adjusted durations (effective, not raw out-in)', () => {
    // A 2x clip occupies 1s of timeline; the seam is at 1.0, not 2.0.
    const fast = [clip('a', 0, 1), clip('b', 1, 2)]
    const { shift } = v1Layout(fast, [{ at: 1, duration: 0.5 }])
    expect(shift.get('b')).toBeCloseTo(0.5, 6)
  })
})

// ---------------------------------------------------------------------------
// render_time(t) = t − Σ{d_i : seam s_i ≤ t} — the one rule every non-v1 lane
// now follows in the renderer, and therefore on this canvas.
// ---------------------------------------------------------------------------

describe('renderTime / renderWindow', () => {
  it('is the identity without transitions', () => {
    const seams = seamTable(FOUR, [])
    for (const t of [0, 1.3, 2, 5.5, 8]) expect(renderTime(seams, t)).toBe(t)
    expect(renderWindow(seams, 1, 3)).toEqual({ start: 1, end: 3, dropped: false })
  })

  it('pulls everything at or after one seam by its duration, and nothing before it', () => {
    const seams = seamTable(FOUR, [{ at: 2, duration: 0.5 }])
    expect(renderTime(seams, 1.9)).toBeCloseTo(1.9, 9)
    expect(renderTime(seams, 2.0)).toBeCloseTo(1.5, 9)   // seam ≤ t is inclusive
    expect(renderTime(seams, 3.0)).toBeCloseTo(2.5, 9)
    expect(renderTime(seams, 8.0)).toBeCloseTo(7.5, 9)
  })

  it('stacks seams: the pull is the accumulated overlap of every seam ≤ t', () => {
    const seams = seamTable(FOUR, [
      { at: 2, duration: 0.5 }, { at: 4, duration: 0.5 }, { at: 6, duration: 0.5 },
    ])
    expect(renderTime(seams, 3)).toBeCloseTo(2.5, 9)
    expect(renderTime(seams, 5)).toBeCloseTo(4.0, 9)
    expect(renderTime(seams, 7)).toBeCloseTo(5.5, 9)
    // A clip that starts at a seam lands where v1Layout already draws it.
    const { shift } = v1Layout(FOUR, [
      { at: 2, duration: 0.5 }, { at: 4, duration: 0.5 }, { at: 6, duration: 0.5 },
    ])
    for (const c of FOUR) expect(renderTime(seams, c.start)).toBeCloseTo(outStart(c, shift), 9)
  })

  it('treats a seam within float slack of t as ≤ t', () => {
    // auto_caption lands cues on boundaries computed as start + (out-in)/speed.
    const seams = seamTable([clip('a', 0, 13.73), clip('b', 13.73, 5)], [{ at: 13.73, duration: 0.3 }])
    expect(renderTime(seams, 13.730000000000002)).toBeCloseTo(13.43, 9)
    expect(renderTime(seams, 13.729999999999999)).toBeCloseTo(13.43, 9)
  })

  it('ignores an uncharged seam (gap → hard cut) and keeps counting past it', () => {
    const withGap = [clip('a', 0, 2), clip('b', 2, 2), clip('c', 5, 2)]
    const seams = seamTable(withGap, [{ at: 2, duration: 0.5 }, { at: 4, duration: 0.5 }])
    expect(renderTime(seams, 6)).toBeCloseTo(5.5, 9)
  })

  it('shrinks a window that crosses a seam by that seam\'s overlap', () => {
    const seams = seamTable(FOUR, [{ at: 2, duration: 0.5 }])
    const w = renderWindow(seams, 1, 3)
    expect(w.start).toBeCloseTo(1.0, 9)
    expect(w.end).toBeCloseTo(2.5, 9)
    expect(w.dropped).toBe(false)
  })

  it('maps clip B\'s first d seconds onto the crossfade window, and collapses A\'s tail', () => {
    // The crossfade at seam s (d=0.5, D=1.0 through it) occupies render
    // [s−D, s−D+d) = [3.0, 3.5). Clip B's head [s, s+d) maps exactly onto it.
    // Clip A's tail [s−d, s) is the span the crossfade CONSUMED: both its
    // endpoints map to 3.0, so a window covering only it has zero length and
    // is dropped — the picture there is already A and B at once, and the
    // renderer applies the same endpoint rule, never an inverted window.
    const seams = seamTable(FOUR, [{ at: 2, duration: 0.5 }, { at: 4, duration: 0.5 }])
    const bHead = renderWindow(seams, 4.0, 4.5)
    expect(bHead.start).toBeCloseTo(3.0, 9)
    expect(bHead.end).toBeCloseTo(3.5, 9)
    expect(bHead.dropped).toBe(false)
    const aTail = renderWindow(seams, 3.5, 4.0)
    expect(aTail.start).toBeCloseTo(3.0, 9)
    expect(aTail.end).toBeCloseTo(3.0, 9)
    expect(aTail.dropped).toBe(true)
  })

  it('drops (never inverts) a window entirely inside a consumed span', () => {
    const seams = seamTable(FOUR, [{ at: 2, duration: 0.5 }])
    // [1.9, 2.1) straddles the seam by less than d on each side:
    // start → 1.9, end → 2.1 − 0.5 = 1.6 — negative length.
    const w = renderWindow(seams, 1.9, 2.1)
    expect(w.dropped).toBe(true)
    expect(w.end).toBeLessThan(w.start)
    // Exactly zero length is dropped too.
    expect(renderWindow(seams, 1.5, 2.0).dropped).toBe(true)
    // One frame wider on the far side and it survives.
    expect(renderWindow(seams, 1.5, 2.04).dropped).toBe(false)
  })
})

describe('layoutTime (the overlay inverse)', () => {
  const TRS = [{ at: 2, duration: 0.5 }, { at: 4, duration: 0.5 }]

  it('is the identity without transitions', () => {
    const seams = seamTable(FOUR, [])
    for (const r of [0, 1.3, 5.5, 8]) expect(layoutTime(seams, r)).toBe(r)
  })

  it('round-trips render → layout → render outside crossfade windows', () => {
    // Windows: [1.5, 2.0) and [3.0, 3.5) in render time.
    const seams = seamTable(FOUR, TRS)
    const inWindow = (r: number) => (r >= 1.5 && r < 2.0) || (r >= 3.0 && r < 3.5)
    for (let i = 0; i <= 140; i++) {
      const r = i * 0.05
      const back = renderTime(seams, layoutTime(seams, r))
      if (inWindow(r)) {
        // Inside a dissolve the snap rule lands on the window's START — where
        // the seam's layout time renders — never anywhere else.
        expect(back).toBeCloseTo(r < 2.0 ? 1.5 : 3.0, 9)
      } else {
        expect(back).toBeCloseTo(r, 9)
      }
    }
  })

  it('round-trips layout → render → layout outside the consumed spans', () => {
    const seams = seamTable(FOUR, TRS)
    for (const t of [0, 1.0, 1.49, 2.5, 3.4, 4.5, 6, 8]) {
      expect(layoutTime(seams, renderTime(seams, t))).toBeCloseTo(t, 9)
    }
  })

  it('snaps a render position inside a crossfade window to the seam\'s layout time', () => {
    // Seam 2.0 with d=0.5: window [1.5, 2.0) in render time.
    const seams = seamTable(FOUR, TRS)
    expect(layoutTime(seams, 1.5)).toBeCloseTo(2.0, 9)
    expect(layoutTime(seams, 1.75)).toBeCloseTo(2.0, 9)
    expect(layoutTime(seams, 1.999)).toBeCloseTo(2.0, 9)
    // Just past the window: clip B's head is over, D = 0.5 applies.
    expect(layoutTime(seams, 2.0)).toBeCloseTo(2.5, 9)
    // Second seam 4.0: window [3.0, 3.5) in render time.
    expect(layoutTime(seams, 3.2)).toBeCloseTo(4.0, 9)
    expect(layoutTime(seams, 3.5)).toBeCloseTo(4.5, 9)
    // A hard cut has no window to snap into.
    const hard = seamTable(FOUR, [{ at: 2, duration: 0.5 }])
    expect(layoutTime(hard, 3.5)).toBeCloseTo(4.0, 9)
  })

  it('agrees with edlTimeFromOutput outside crossfade windows', () => {
    const seams = seamTable(FOUR, TRS)
    const { shift } = v1Layout(FOUR, TRS)
    for (const r of [0, 1, 2.2, 2.9, 3.6, 5, 7]) {
      expect(layoutTime(seams, r)).toBeCloseTo(edlTimeFromOutput(r, FOUR, shift), 9)
    }
  })
})

// ---------------------------------------------------------------------------
// What Timeline.tsx actually draws — `drawnSpan` is the single function its
// draw loop and hit list call, so this IS the canvas test.
// ---------------------------------------------------------------------------

function edlWith(transitions: { at: number; type: string; duration: number }[]): EDL {
  const v1 = {
    id: 'v1', type: 'video', z: 0,
    clips: [
      { id: 'a', track: 'v1', src: 'a.mp4', in: 0, out: 13.73, start: 0 },
      { id: 'b', track: 'v1', src: 'b.mp4', in: 0, out: 10, start: 13.73 },
    ],
    transitions,
  }
  const captions = {
    id: 'captions', type: 'captions', z: 13,
    clips: [{ id: 'cue', track: 'captions', text: 'hello', start: 14.23, end: 16.0 }],
  }
  return {
    version: 1, duration: 23.73, canvas: { w: 1080, h: 1920, fps: 30 },
    tracks: [v1, captions],
  } as unknown as EDL
}

describe('drawnSpan (Timeline.tsx overlay lanes)', () => {
  it('draws a caption at layout 14.23 at 13.93 under a 0.3 s transition at 13.73', () => {
    const edl = edlWith([{ at: 13.73, type: 'zoomin', duration: 0.3 }])
    const layout = { shift: new Map<string, number>(), seams: v1SeamsOf(edl) }
    const cue = edl.tracks[1].clips[0]
    const span = drawnSpan('captions', cue, layout)
    expect(span.start).toBeCloseTo(13.93, 9)
    expect(span.duration).toBeCloseTo(16.0 - 14.23, 9)
    expect(span.dropped).toBe(false)
  })

  it('draws the same caption at its own start with no transitions', () => {
    const edl = edlWith([])
    const layout = { shift: new Map<string, number>(), seams: v1SeamsOf(edl) }
    expect(layout.seams).toEqual([])
    const span = drawnSpan('captions', edl.tracks[1].clips[0], layout)
    expect(span.start).toBeCloseTo(14.23, 9)
  })

  it('keeps v1 on its per-clip pull with its full duration', () => {
    const edl = edlWith([{ at: 13.73, type: 'zoomin', duration: 0.3 }])
    const lc = edl.tracks[0].clips.map((c) => ({
      id: c.id, start: c.start, duration: (c as { out: number }).out,
    }))
    const layout = v1Layout(lc, [{ at: 13.73, duration: 0.3 }])
    const b = drawnSpan('v1', edl.tracks[0].clips[1], layout)
    expect(b.start).toBeCloseTo(13.43, 9)
    expect(b.duration).toBeCloseTo(10, 9)
  })

  it('flags an overlay the renderer will drop instead of drawing it inverted', () => {
    const edl = edlWith([{ at: 13.73, type: 'zoomin', duration: 0.3 }])
    const layout = { shift: new Map<string, number>(), seams: v1SeamsOf(edl) }
    const hugging = { id: 'x', track: 'captions', text: 'x', start: 13.6, end: 13.8 } as unknown as AnyClipLike
    const span = drawnSpan('captions', hugging, layout)
    expect(span.dropped).toBe(true)
    expect(span.duration).toBe(0)
  })
})

type AnyClipLike = EDL['tracks'][number]['clips'][number]

// ---------------------------------------------------------------------------
// The EDL-level v1 helpers — what Preview.tsx's source-draw path and any
// other EDL-only caller use. They must agree with the clip-list forms above.
// ---------------------------------------------------------------------------

describe('v1LayoutOf / v1SeamsOf (EDL-level)', () => {
  it('is the identity (empty pull, no seams) without transitions or without v1', () => {
    const none = v1LayoutOf(edlWith([]))
    expect(none.shift.size).toBe(0)
    expect(none.seams).toEqual([])
    expect(v1LayoutOf(null).shift.get('b') ?? 0).toBe(0)
    expect(v1SeamsOf(undefined)).toEqual([])
  })

  it('pulls the clip after a seam by the transition it will actually apply', () => {
    // 13.73 s clip A, then B; 0.3 s dissolve at the seam → B renders at 13.43.
    const edl = edlWith([{ at: 13.73, type: 'zoomin', duration: 0.3 }])
    const { shift, seams } = v1LayoutOf(edl)
    expect(shift.get('a')).toBe(0)
    expect(shift.get('b')).toBeCloseTo(0.3, 9)
    // The render start Preview.tsx syncs the source frame against.
    expect(edl.tracks[0].clips[1].start - (shift.get('b') ?? 0)).toBeCloseTo(13.43, 9)
    // One extraction feeds both helpers, so the seam tables are identical.
    expect(seams).toEqual(v1SeamsOf(edl))
  })

  it('ignores a non-media clip that strayed onto v1', () => {
    const edl = edlWith([{ at: 13.73, type: 'zoomin', duration: 0.3 }])
    const stray = { id: 't', track: 'v1', text: 'x', start: 5, end: 6 } as unknown as AnyClipLike
    const withStray = { ...edl, tracks: [{ ...edl.tracks[0], clips: [stray, ...edl.tracks[0].clips] }, edl.tracks[1]] } as EDL
    const { shift } = v1LayoutOf(withStray)
    expect(shift.has('t')).toBe(false)
    expect(shift.get('b')).toBeCloseTo(0.3, 9)
  })
})

// ---------------------------------------------------------------------------
// The EDL-level conveniences the playhead-holding components go through.
// One 0.5 s seam at 2.0 (the fixture tests/test_transition_overlay_sync.py
// renders): render_time(t) = t for t < 2, t − 0.5 from the seam on.
// ---------------------------------------------------------------------------

function twoClipEdl(transitions: { at: number; type: string; duration: number }[]): EDL {
  return {
    version: 1, duration: 3.5, canvas: { w: 320, h: 180, fps: 30 },
    tracks: [
      { id: 'v1', type: 'video', z: 0, transitions, clips: [
        { id: 'a', track: 'v1', src: 'a.mp4', in: 0, out: 2, start: 0 },
        { id: 'b', track: 'v1', src: 'b.mp4', in: 0, out: 2, start: 2 },
      ] },
      { id: 'stickers', type: 'sticker', z: 12, clips: [
        { id: 'st', track: 'stickers', src: 'x.png', start: 3.0, end: 3.5 },
      ] },
      { id: 'captions', type: 'captions', z: 13, clips: [
        { id: 'cue', track: 'captions', text: 'hi', start: 3.0, end: 4.0 },
      ] },
    ],
  } as unknown as EDL
}
const FADE = [{ at: 2, type: 'fade', duration: 0.5 }]

describe('layoutPlayhead (TextTool / StickerPanel / add_marker)', () => {
  it('decodes the render-time playhead to the layout time a tool argument needs', () => {
    // Playhead at render 3.0 is layout 3.5 — the raw playhead would have
    // authored the clip 0.5 s before the frame under it.
    expect(layoutPlayhead(twoClipEdl(FADE), 3.0)).toBeCloseTo(3.5, 9)
    expect(layoutPlayhead(twoClipEdl(FADE), 1.0)).toBeCloseTo(1.0, 9)
  })
  it('is the identity without transitions and without an EDL', () => {
    expect(layoutPlayhead(twoClipEdl([]), 3.0)).toBe(3.0)
    expect(layoutPlayhead(null, 3.0)).toBe(3.0)
  })
})

describe('clipLocalTime (Properties keyframe-at-playhead)', () => {
  it('measures an overlay from render_time(start)', () => {
    // Sticker at layout 3.0 plays at render 2.5; the playhead at render 3.0 is
    // 0.5 s into it (the layout-local answer was 0.0).
    const edl = twoClipEdl(FADE)
    const st = edl.tracks[1].clips[0]
    expect(clipLocalTime(edl, 'stickers', st, 3.0)).toBeCloseTo(0.5, 9)
    // Clamped to the RENDER span: [2.5, 3.0) is 0.5 s long.
    expect(clipLocalTime(edl, 'stickers', st, 3.4)).toBeCloseTo(0.5, 9)
    expect(clipLocalTime(edl, 'stickers', st, 2.0)).toBe(0)
  })
  it('measures a v1 clip from its pulled start', () => {
    const edl = twoClipEdl(FADE)
    const b = edl.tracks[0].clips[1]
    // Clip B starts at render 1.5; the playhead at render 2.0 is 0.5 s in.
    expect(clipLocalTime(edl, 'v1', b, 2.0)).toBeCloseTo(0.5, 9)
    // Its full 2 s remain addressable (v1 keeps its length; the overlap is drawn overlapping).
    expect(clipLocalTime(edl, 'v1', b, 3.6)).toBeCloseTo(2.0, 9)
  })
  it('is playhead − start without transitions', () => {
    const edl = twoClipEdl([])
    expect(clipLocalTime(edl, 'stickers', edl.tracks[1].clips[0], 3.2)).toBeCloseTo(0.2, 9)
    expect(clipLocalTime(edl, 'v1', edl.tracks[0].clips[1], 3.2)).toBeCloseTo(1.2, 9)
  })
})

describe('v1ClipAt (StickerLayer framing gate)', () => {
  // Two seams (0.5 each): C starts at layout 4 = render 3.0 on FOUR.
  const { shift } = v1Layout(FOUR, [{ at: 2, duration: 0.5 }, { at: 4, duration: 0.5 }])
  const clips = FOUR.map((c) => ({ id: c.id, src: 'x', in: 0, out: c.duration, start: c.start }))
  it('answers the clip whose PICTURE is on screen, not the layout slot', () => {
    // render 3.0–3.5 is C alone (B ended at render 3.5 − wait: B plays 1.5–3.5,
    // C 3.0–5.0; their crossfade is 3.0–3.5) — the last match is C.
    expect(v1ClipAt(clips, shift, 3.25)?.id).toBe('c')
    // render 3.5–4.5: only C. The layout test said B until layout 4 = render 3.5+.
    expect(v1ClipAt(clips, shift, 3.6)?.id).toBe('c')
    expect(v1ClipAt(clips, shift, 2.9)?.id).toBe('b')
    expect(v1ClipAt(clips, shift, 0.5)?.id).toBe('a')
    expect(v1ClipAt(clips, shift, 7.5)).toBeUndefined()      // past the 7.0 s render
  })
  it('keeps the LAST match inside a crossfade (B, the clip fading in)', () => {
    // render 1.5–2.0 is A's tail crossfaded with B's head.
    expect(v1ClipAt(clips, shift, 1.75)?.id).toBe('b')
  })
  it('is the layout test without transitions', () => {
    const none = new Map<string, number>()
    expect(v1ClipAt(clips, none, 3.6)?.id).toBe('b')
    expect(v1ClipAt(clips, none, 4.0)?.id).toBe('c')
  })
})

describe('v1TimeFromOutput', () => {
  it('maps a render instant on v1 back through the per-clip pull', () => {
    expect(v1TimeFromOutput(twoClipEdl(FADE), 2.7)).toBeCloseTo(3.2, 9)
    expect(v1TimeFromOutput(twoClipEdl(FADE), 1.0)).toBeCloseTo(1.0, 9)
    expect(v1TimeFromOutput(twoClipEdl([]), 2.7)).toBe(2.7)
    expect(v1TimeFromOutput(null, 2.7)).toBe(2.7)
  })
})
