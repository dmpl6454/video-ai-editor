// Review RE: the timing of the Out and Combo animations StickerLayer draws for
// overlays (PiPs) and stickers was pinned by nothing — a mutant planning over
// a 10 s window stopped every overlay Out in the preview and every test still
// passed. `overlayAnimPose` is the ONE rule both draws use: the pose planned
// over the element's visible RENDER window (pip.py `plan_of(c, re - rs)`,
// text_overlay.py the same), checked here against the Python plan's own
// numbers (clipAnimCases.json, tests/gen_clip_anim_table.py).
import { describe, expect, it } from 'vitest'
import cases from './anim/clipAnimCases.json'
import { animAt, type AnimFields } from './anim/clipAnim'
import { animWindowOf, overlayAnimPose, pipAnimPose } from './pipDraw'
import type { EdlClip } from './preview/timeline/framePlan'

interface Case { spec: AnimFields; window: number; samples: number[][] }
const ALL = (cases as unknown as { cases: Case[] }).cases

/** A 1 s crossfade at layout 4.0: everything from 4.0 on plays 1 s earlier. */
const renderTimeOf = (t: number) => (t >= 4.0 - 1e-9 ? t - 1.0 : t)
// The element spans LAYOUT [1.0, 4.0): 3 s on the timeline, 2 s on screen.
const START = 1.0
const END = 4.0

function caseFor(pred: (s: AnimFields) => boolean, window: number): Case {
  const c = ALL.find((x) => pred(x.spec) && Math.abs(x.window - window) < 1e-9)
  if (!c) throw new Error('no such case')
  return c
}

function expectPoseMatches(fields: AnimFields, c: Case, lo: number, hi: number) {
  let n = 0
  for (const [t, scale, x, y, rot, gain] of c.samples) {
    if (t < lo || t > hi) continue
    const p = overlayAnimPose(fields, START, END, t, renderTimeOf)
    expect(p.scale, `scale at ${t}`).toBeCloseTo(scale, 6)
    expect(p.dx, `x at ${t}`).toBeCloseTo(x, 6)
    expect(p.dy, `y at ${t}`).toBeCloseTo(y, 6)
    expect(p.rotation, `rotation at ${t}`).toBeCloseTo(rot, 5)
    expect(p.alpha, `alpha at ${t}`).toBeCloseTo(gain, 6)
    n++
  }
  expect(n).toBeGreaterThan(5)
}

describe('overlay and sticker animation timing (review RE)', () => {
  it('the render window is re − rs: a seam at the end shortens it', () => {
    expect(animWindowOf(renderTimeOf, START, END)).toBeCloseTo(2.0, 12)
    expect(animWindowOf((t) => t, START, END)).toBeCloseTo(3.0, 12)
  })

  it('an Out (Slide Right) plays over the last part of the RENDER window', () => {
    const c = caseFor((s) => s.anim_out === 'slide_right', 2.0)
    const fields = c.spec
    // the whole Out window, and the frames just before it
    expectPoseMatches(fields, c, 0.9, 2.0)
    // planned over the layout span instead, the Out would not have started yet
    const layout = animAt(fields, 1.8, 3.0)
    const drawn = overlayAnimPose(fields, START, END, 1.8, renderTimeOf)
    expect(Math.abs(drawn.dx - layout.dx)).toBeGreaterThan(0.05)
    expect(Math.abs(drawn.dx)).toBeGreaterThan(0.05)
  })

  it('a Combo (Shake) loops on the same clock and window', () => {
    const c = caseFor((s) => s.anim_combo === 'shake', 2.0)
    expectPoseMatches(c.spec, c, 0, 2.0)
  })

  it('pipAnimPose takes the window it is given (StickerLayer passes re − rs)', () => {
    const c = caseFor((s) => s.anim_out === 'slide_right', 2.0)
    const clip = { ...c.spec, in: 0, out: 3, start: START, src: 'x.mp4', id: 'p' } as unknown as EdlClip
    for (const [t, , x] of c.samples) {
      if (t < 1.0) continue
      expect(pipAnimPose(clip, t, 2.0).dx).toBeCloseTo(x, 6)
    }
  })
})
