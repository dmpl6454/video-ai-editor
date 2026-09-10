// TextLayer's anim envelope (lib/textAnim) runs on the RENDER window.
//
// The defect: `animEnvelope(c, t, height)` ran the pop/slide/fade curves and
// the keyframed-opacity clock in LAYOUT-local time (`t − c.start`) against the
// render-time `t`, after activity had already been converted. A pop-in hook at
// layout 30–33 s after three 0.5 s dissolves (render 28.5–31.5) sat frozen at
// scale 0.6 for 1.5 s, popped 1.5 s late, and its fade-out never reached the
// window's end. text_overlay.py evaluates `(t − rs) / d` with d clamped
// against `(re − rs)`; so does this now.
import { describe, expect, it } from 'vitest'
import { renderWindow, seamTable, type LayoutClip } from './timelineLayout'
import { animEnvelope } from './textAnim'

const clip = (id: string, start: number, duration: number): LayoutClip => ({ id, start, duration })
// Three 0.5 s dissolves before layout 30: 1.5 s consumed.
const SEAMS = seamTable(
  [clip('a', 0, 10), clip('b', 10, 10), clip('c', 20, 10), clip('d', 30, 10)],
  [{ at: 10, duration: 0.5 }, { at: 20, duration: 0.5 }, { at: 30, duration: 0.5 }],
)
const HOOK = { anim_in: 'pop', anim_out: 'fade' }
const H = 1000

describe('animEnvelope on the render window', () => {
  const win = renderWindow(SEAMS, 30, 33)          // [28.5, 31.5)
  it('starts the pop-in at render_time(start), not at the layout start', () => {
    expect(win.start).toBeCloseTo(28.5, 9)
    expect(animEnvelope(HOOK, 28.5, H, win).scale).toBeCloseTo(0.6, 6)   // first frame: q=0
    // d = min(0.35, 3.0 * 0.4) = 0.35 → settled 0.35 s after the render start.
    expect(animEnvelope(HOOK, 28.5 + 0.35, H, win).scale).toBeCloseTo(1.0, 6)
    // The old layout clock still had q = (28.85 − 30)/d < 0 here: frozen at 0.6.
    expect(animEnvelope(HOOK, 28.85, H, { start: 30, end: 33 }).scale).toBeCloseTo(0.6, 6)
  })
  it('finishes the fade-out exactly at the render window\'s end', () => {
    expect(animEnvelope(HOOK, 31.5, H, win).alpha).toBeCloseTo(0, 6)
    expect(animEnvelope(HOOK, 31.5 - 0.35, H, win).alpha).toBeCloseTo(1, 6)
    // Layout window: alpha would still be 1 at the instant the clip leaves the screen.
    expect(animEnvelope(HOOK, 31.5, H, { start: 30, end: 33 }).alpha).toBeCloseTo(1, 6)
  })
  it('clamps d against the RENDER length of a clip that straddles a seam', () => {
    // Layout 29.9–30.4 crosses the 30.0 seam: 0.5 s authored, 0.0 s... no —
    // renderWindow gives [29.9 − 1.0, 30.4 − 1.5) = [28.9, 28.9): dropped.
    expect(renderWindow(SEAMS, 29.9, 30.4).dropped).toBe(true)
    // Layout 29.8–30.6: render [28.8, 29.1), 0.3 s on screen → d = 0.12, as the server's.
    const w = renderWindow(SEAMS, 29.8, 30.6)
    expect(w.end - w.start).toBeCloseTo(0.3, 9)
    expect(animEnvelope({ anim_in: 'fade', anim_out: null }, w.start + 0.12, H, w).alpha).toBeCloseTo(1, 6)
    expect(animEnvelope({ anim_in: 'fade', anim_out: null }, w.start + 0.06, H, w).alpha).toBeCloseTo(0.5, 6)
  })
  it('is unchanged without transitions', () => {
    const none = renderWindow([], 30, 33)
    expect(animEnvelope(HOOK, 30, H, none).scale).toBeCloseTo(0.6, 6)
    expect(animEnvelope(HOOK, 30.35, H, none).scale).toBeCloseTo(1.0, 6)
    expect(animEnvelope(HOOK, 33, H, none).alpha).toBeCloseTo(0, 6)
  })
})
