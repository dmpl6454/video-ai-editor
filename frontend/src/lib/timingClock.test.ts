// The Inspector's Timing section speaks the RULER's clock (render time), not
// the EDL's layout time.
//
// The defect (final QA, editor-ux): four 5 s clips with a 0.5 s dissolve on
// each of the 3 cuts; a voiceover imported at the playhead 06:00 is stored at
// layout 6.5 and plays at 6.000 s in the export — but the Inspector read
// Start 00:00:06:15, and typing 00:00:08:00 landed it at 07:15 on the ruler.
// A v1 clip read its layout start too (vertical_street: 10.0 shown, drawn and
// exported at 9.0).
import { describe, expect, it } from 'vitest'
import { clockedEdit, mediaTimingEdit, overlayTimingEdit, shownTiming } from './clipTiming'
import { LAYOUT_CLOCK, renderSpanOf, timingClockOf } from './timelineLayout'
import type { AnyClip, EDL } from '../types'

function dissolvedEdl(): EDL {
  const v1 = {
    id: 'v1', type: 'video', z: 0,
    clips: ['beach', 'city', 'vs', 'mountains'].map((id, i) => ({
      id, track: 'v1', src: `${id}.mp4`, in: 0, out: 5, start: i * 5,
    })),
    transitions: [5, 10, 15].map((at) => ({ at, type: 'dissolve', duration: 0.5 })),
  }
  const vo = {
    id: 'vo', type: 'vo', z: 20,
    clips: [{ id: 'vo1', track: 'vo', src: 'voiceover.m4a', in: 0, out: 7, start: 6.5 }],
  }
  const text = {
    id: 'text', type: 'text', z: 10,
    clips: [{ id: 't1', track: 'text', text: 'Hi', start: 11, end: 12 }],
  }
  return {
    version: 1, duration: 18.5, canvas: { w: 1920, h: 1080, fps: 30 },
    tracks: [v1, vo, text],
  } as unknown as EDL
}

const clipOf = (edl: EDL, tid: string, id: string) =>
  edl.tracks.find((t) => t.id === tid)!.clips.find((c) => c.id === id)! as AnyClip

describe('Timing section clock', () => {
  it('shows a voiceover imported at the playhead where the ruler draws it', () => {
    const edl = dissolvedEdl()
    const vo = clipOf(edl, 'vo', 'vo1')
    const clock = timingClockOf(edl, 'vo', vo)
    const shown = shownTiming({ start: 6.5, end: 13.5 }, clock)
    expect(shown.start).toBeCloseTo(6.0, 9)
    // Same place renderSpanOf (the Timeline's drawn span) puts it.
    expect(shown.start).toBeCloseTo(renderSpanOf(edl, 'vo', vo).start, 9)
  })

  it('a typed Start lands the clip at that ruler time', () => {
    const edl = dissolvedEdl()
    const vo = clipOf(edl, 'vo', 'vo1')
    const clock = timingClockOf(edl, 'vo', vo)
    const e = clockedEdit('start', 8.0, clock.show(6.5), clock)!
    const edit = mediaTimingEdit({ in: 0, out: 7, start: 6.5, speed: 1 }, e.field, e.value)!
    expect(edit.tool).toBe('move_clip')
    const moved = { ...vo, start: edit.args.new_start } as AnyClip
    expect(renderSpanOf(edl, 'vo', moved).start).toBeCloseTo(8.0, 9)
  })

  it('shows a v1 clip at its pulled start, and End / Duration keep their length', () => {
    const edl = dissolvedEdl()
    const vs = clipOf(edl, 'v1', 'vs')
    const clock = timingClockOf(edl, 'v1', vs)
    const shown = shownTiming({ start: 10, end: 15 }, clock)
    expect(shown.start).toBeCloseTo(9.0, 9)
    expect(shown.end).toBeCloseTo(14.0, 9)
    // Duration 4 s → the clip ends 4 s after its (unchanged) start.
    const d = clockedEdit('duration', 4, shown.start, clock)!
    expect(mediaTimingEdit({ in: 0, out: 5, start: 10, speed: 1 }, d.field, d.value))
      .toEqual({ tool: 'trim_clip', args: { out: 4 } })
    // End typed on the ruler (13.0) trims to the same 4 s.
    const e = clockedEdit('end', 13.0, shown.start, clock)!
    expect(mediaTimingEdit({ in: 0, out: 5, start: 10, speed: 1 }, e.field, e.value))
      .toEqual({ tool: 'trim_clip', args: { out: 4 } })
    // A Start on v1 decodes the per-clip pull (Timeline dropStart's rule).
    const s = clockedEdit('start', 9.0, shown.start, clock)!
    expect(s.value).toBeCloseTo(10.0, 9)
  })

  it('an overlay (text) Start / End round-trip through the ruler', () => {
    const edl = dissolvedEdl()
    const t1 = clipOf(edl, 'text', 't1')
    const clock = timingClockOf(edl, 'text', t1)
    const shown = shownTiming({ start: 11, end: 12 }, clock)
    expect(shown.start).toBeCloseTo(10.0, 9)   // two seams (5, 10) before it
    const e = clockedEdit('start', 12.0, shown.start, clock)!
    const edit = overlayTimingEdit({ start: 11, end: 12 }, e.field, e.value)!
    expect(edit.args.start).toBeCloseTo(13.0, 9)
    const end = clockedEdit('end', 10.5, shown.start, clock)!
    expect(overlayTimingEdit({ start: 11, end: 12 }, end.field, end.value)!.args.end).toBeCloseTo(11.5, 9)
  })

  it('a voiceover across seams keeps its whole length: Start, End and Duration agree with the Speed panel', () => {
    // Final QA (round 3): a 7 s voiceover typed at ruler 04:00 over the
    // dissolves at 5 and 10 read End 10:03 / Duration 06:03 while Speed >
    // Duration said 7.00 s, and the export cut its last words. A sound lane
    // plays whole from where its start plays (`clock.sound_window`).
    const edl = dissolvedEdl()
    const vo = { ...clipOf(edl, 'vo', 'vo1'), start: 4 } as AnyClip
    const clock = timingClockOf(edl, 'vo', vo)
    const shown = shownTiming({ start: 4, end: 11 }, clock)
    expect(shown.start).toBeCloseTo(4.0, 9)
    expect(shown.end).toBeCloseTo(11.0, 9)
    const span = renderSpanOf(edl, 'vo', vo)
    expect(span.start).toBeCloseTo(4.0, 9)
    expect(span.end - span.start).toBeCloseTo(7.0, 9)
    // End typed on the ruler (10.0) trims it to 6 s of source.
    const e = clockedEdit('end', 10.0, shown.start, clock)!
    expect(mediaTimingEdit({ in: 0, out: 7, start: 4, speed: 1 }, e.field, e.value))
      .toEqual({ tool: 'trim_clip', args: { out: 6 } })
    // One seam before it: shown pulled by 0.5 s, still 7 s long.
    const late = timingClockOf(edl, 'vo', clipOf(edl, 'vo', 'vo1'))
    const s2 = shownTiming({ start: 6.5, end: 13.5 }, late)
    expect(s2.start).toBeCloseTo(6.0, 9)
    expect(s2.end).toBeCloseTo(13.0, 9)
  })

  it('is the identity without transitions, and refuses a non-number', () => {
    expect(shownTiming({ start: 3, end: 4 }, LAYOUT_CLOCK)).toEqual({ start: 3, end: 4 })
    expect(clockedEdit('duration', 2, 3, LAYOUT_CLOCK)).toEqual({ field: 'end', value: 5 })
    expect(clockedEdit('start', Number.NaN, 3, LAYOUT_CLOCK)).toBeNull()
  })
})
