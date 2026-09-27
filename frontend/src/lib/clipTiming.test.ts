// QA-048 (remainder): one timing model — Start moves, End trims, Duration
// trims — for text, stickers and media alike.
import { describe, expect, it } from 'vitest'
import { mediaTimelineDuration, mediaTimingEdit, overlayTimingEdit, curveClockOf } from './clipTiming'
import { effectiveDuration, sourceOffsetAt } from './preview/timeline/framePlan'

const overlay = { start: 2, end: 5 }

describe('overlays (text and stickers) share one rule', () => {
  it('Start moves the clip and keeps its duration', () => {
    expect(overlayTimingEdit(overlay, 'start', 10)).toEqual({ tool: 'set_clip_timing', args: { start: 10, end: 13 } })
    expect(overlayTimingEdit(overlay, 'start', -4)).toEqual({ tool: 'set_clip_timing', args: { start: 0, end: 3 } })
  })
  it('End trims the end and leaves the start', () => {
    expect(overlayTimingEdit(overlay, 'end', 4)).toEqual({ tool: 'set_clip_timing', args: { end: 4 } })
  })
  it('Duration trims the end: End = Start + Duration', () => {
    expect(overlayTimingEdit(overlay, 'duration', 1.5)).toEqual({ tool: 'set_clip_timing', args: { end: 3.5 } })
  })
  it('refuses an edit that would leave no clip', () => {
    expect(overlayTimingEdit(overlay, 'end', 2)).toBeNull()
    expect(overlayTimingEdit(overlay, 'end', 1)).toBeNull()
    expect(overlayTimingEdit(overlay, 'duration', 0)).toBeNull()
    expect(overlayTimingEdit(overlay, 'start', Number.NaN)).toBeNull()
  })
})

describe('media clips: the same timeline triple, plus the source In / Out', () => {
  const clip = { in: 1, out: 7, start: 3, speed: 2 }   // 6 s of source at 2× → 3 s on the timeline
  it('knows its timeline span', () => {
    expect(mediaTimelineDuration(clip)).toBe(3)
    expect(mediaTimelineDuration({ ...clip, speed: 0 })).toBe(6)
  })
  it('Start moves the clip (its duration is kept)', () => {
    expect(mediaTimingEdit(clip, 'start', 8)).toEqual({ tool: 'move_clip', args: { new_start: 8 } })
  })
  it('End trims the end through the speed', () => {
    // End at 5 → 2 s on the timeline → 4 s of source → Out = 1 + 4.
    expect(mediaTimingEdit(clip, 'end', 5)).toEqual({ tool: 'trim_clip', args: { out: 5 } })
  })
  it('Duration trims the end the same way', () => {
    expect(mediaTimingEdit(clip, 'duration', 2)).toEqual({ tool: 'trim_clip', args: { out: 5 } })
  })
  it('In / Out trim the source', () => {
    expect(mediaTimingEdit(clip, 'in', 2)).toEqual({ tool: 'trim_clip', args: { in: 2 } })
    expect(mediaTimingEdit(clip, 'out', 6)).toEqual({ tool: 'trim_clip', args: { out: 6 } })
  })
  it('refuses an End at or before Start', () => {
    expect(mediaTimingEdit(clip, 'end', 3)).toBeNull()
    expect(mediaTimingEdit(clip, 'duration', 0)).toBeNull()
  })
  it('Start and End mean the same thing for a clip and an overlay at the same place', () => {
    const asOverlay = { start: clip.start, end: clip.start + mediaTimelineDuration(clip) }
    // Moving: both keep their 3 s; trimming the end to 5: both end at 5.
    expect(overlayTimingEdit(asOverlay, 'start', 8)?.args).toEqual({ start: 8, end: 11 })
    expect(mediaTimingEdit(clip, 'start', 8)?.args).toEqual({ new_start: 8 })
    expect(overlayTimingEdit(asOverlay, 'end', 5)?.args).toEqual({ end: 5 })
    const t = mediaTimingEdit(clip, 'end', 5)!
    expect(clip.start + mediaTimelineDuration({ ...clip, out: t.args.out })).toBe(5)
  })
})

describe('a speed-CURVE clip trims through its curve, not its mean speed (review RD3)', () => {
  // The Hero preset over source 5.0-8.0: the clip the reviewer typed a
  // Duration of 00:00:02:00 into and got 00:00:02:07 (out 6.584, the mean).
  const hero = { id: 'c', src: 's', in: 5.0, out: 8.0, start: 0,
    speed: { curve: [[0, 1.0], [0.3, 0.2], [0.7, 0.2], [1, 1.6]] } }
  const clock = curveClockOf(hero)!
  const span = { in: 5.0, out: 8.0, start: 3.0, speed: 3.0 / clock.duration, curve: clock }

  it('shows the curve\'s footprint', () => {
    expect(clock.duration).toBeCloseTo(effectiveDuration(hero as never), 12)
    expect(mediaTimelineDuration(span)).toBe(clock.duration)
  })

  it('a shorter Duration / End asks for the source time the curve reaches there', () => {
    const d = mediaTimingEdit(span, 'duration', 2.0)!
    expect(d.args.out).toBeCloseTo(5.0 + sourceOffsetAt(hero as never, 2.0), 12)
    expect(d.args.out).not.toBeCloseTo(5.0 + 2.0 * span.speed, 3)
    expect(mediaTimingEdit(span, 'end', 3.0 + 2.0)!.args.out).toBe(d.args.out)
  })

  it('a longer one has no curve to read: the mean speed, as before', () => {
    expect(mediaTimingEdit(span, 'duration', clock.duration + 1)!.args.out).toBeCloseTo(5.0 + (clock.duration + 1) * span.speed, 9)
  })

  it('a constant speed or a freeze has no curve clock', () => {
    expect(curveClockOf({ ...hero, speed: 2 })).toBeNull()
    expect(curveClockOf({ ...hero, freeze: 1.5 })).toBeNull()
  })
})
