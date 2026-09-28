// ONE timing model for every clip in the Inspector (QA-048 remainder).
//
// Before: media edited In / Out / "Start on timeline", text edited Start / End,
// stickers Start / Duration — and "Start" MOVED a sticker but TRIMMED a text
// clip. Now every clip shows the same timeline triple with one rule each:
//
//   Start     moves the clip; its duration is kept.
//   End       trims the end; the start stays.
//   Duration  trims the end too (End = Start + Duration).
//
// A media clip additionally shows its SOURCE In / Out (which part of the file
// plays); its timeline span is (Out − In) / speed, so End and Duration trim
// Out through the speed. Overlays (text, stickers) answer set_clip_timing;
// media answers trim_clip / move_clip — the same three verbs either way.

import { effectiveDuration, freezeOf, sourceOffsetAt, type EdlClip } from './preview/timeline/framePlan'
import { curvePoints } from './preview/timeline/speedCurve'

export type TimelineField = 'start' | 'end' | 'duration'
export type MediaField = TimelineField | 'in' | 'out'

/** The shortest span a field may leave (the backend floors overlays at 0.1 s). */
export const MIN_SPAN_S = 0.1

export interface OverlaySpan { start: number; end: number }
/** A speed-CURVE clip's clock: its footprint and the source seconds past
 *  `in` at a clip-local timeline time (framePlan.sourceOffsetAt) — the same
 *  shape as dragResolve's CurveTrimClock. */
export interface CurveClock { duration: number; sourceAt: (localT: number) => number }

/** `speed`: the scalar (a curve's MEAN) speed; `curve` for a curve clip,
 *  whose timeline length is its integral, not (out − in) / mean. */
export interface MediaSpan { in: number; out: number; start: number; speed: number; curve?: CurveClock | null }

export interface TimingEdit { tool: 'set_clip_timing' | 'trim_clip' | 'move_clip'; args: Record<string, number> }

const finite = (v: number) => Number.isFinite(v)

/** An overlay field edit → the set_clip_timing args, or null when it would
 *  leave no clip (End at or before Start, a zero Duration). */
export function overlayTimingEdit(span: OverlaySpan, field: TimelineField, value: number): TimingEdit | null {
  if (!finite(value)) return null
  const dur = span.end - span.start
  switch (field) {
    case 'start': {
      const start = Math.max(0, value)
      return { tool: 'set_clip_timing', args: { start, end: start + dur } }
    }
    case 'end':
      if (value - span.start < MIN_SPAN_S - 1e-9) return null
      return { tool: 'set_clip_timing', args: { end: value } }
    case 'duration':
      if (value < MIN_SPAN_S - 1e-9) return null
      return { tool: 'set_clip_timing', args: { end: span.start + value } }
  }
}

/** A media clip's span on the timeline: its source range at its speed. */
export function mediaTimelineDuration(m: MediaSpan): number {
  if (m.curve) return m.curve.duration
  const speed = m.speed > 0 ? m.speed : 1
  return Math.max(0, m.out - m.in) / speed
}

/** A media field edit → the trim_clip / move_clip call, or null for no clip. */
export function mediaTimingEdit(m: MediaSpan, field: MediaField, value: number): TimingEdit | null {
  if (!finite(value)) return null
  const speed = m.speed > 0 ? m.speed : 1
  switch (field) {
    case 'in':
      return { tool: 'trim_clip', args: { in: Math.max(0, value) } }
    case 'out':
      return { tool: 'trim_clip', args: { out: Math.max(0, value) } }
    case 'start':
      return { tool: 'move_clip', args: { new_start: Math.max(0, value) } }
    case 'end':
    case 'duration': {
      const len = field === 'end' ? value - m.start : value
      if (len < MIN_SPAN_S - 1e-9) return null
      // A curve clip SHORTENED: the source time the curve reaches at `len`
      // (review RD3 — through the mean speed, 00:00:02:00 on a Hero clip
      // became 00:00:02:07); the server keeps exactly that piece of the
      // curve (dispatch._trim_curve). Longer: the mean speed, as before.
      if (m.curve && len < m.curve.duration - 1e-9) {
        return { tool: 'trim_clip', args: { out: m.in + m.curve.sourceAt(len) } }
      }
      return { tool: 'trim_clip', args: { out: m.in + len * speed } }
    }
  }
}

/** The curve clock of a speed-curve media clip, or null (a constant speed or
 *  a freeze): what the Inspector's End / Duration and a timeline edge drag
 *  trim through (review RD3). */
export function curveClockOf(c: unknown): CurveClock | null {
  const e = c as EdlClip
  if (!e || freezeOf(e) !== null || !curvePoints(e.speed)) return null
  return { duration: effectiveDuration(e), sourceAt: (t) => sourceOffsetAt(e, t) }
}

// ---------------------------------------------------------------- the ruler's clock

/** The clock the fields speak (lib/timelineLayout `timingClockOf`): shows an
 *  EDL instant on the ruler's clock and decodes a typed one back. */
export interface FieldClock {
  show: (layoutT: number) => number
  start: (r: number) => number
  end: (r: number) => number
}

/** A clip's EDL span as the fields show it: on the ruler's clock, where the
 *  playhead, the Timeline and the export put it. */
export function shownTiming(span: OverlaySpan, clock: FieldClock): OverlaySpan {
  return { start: clock.show(span.start), end: clock.show(span.end) }
}

/** A value typed into Start / End / Duration (ruler time) → the EDL-time
 *  field edit `overlayTimingEdit` / `mediaTimingEdit` take. A Duration is an
 *  End measured from the SHOWN start, so it too goes through the clock. */
export function clockedEdit(
  field: TimelineField, value: number, shownStart: number, clock: FieldClock,
): { field: 'start' | 'end'; value: number } | null {
  if (!finite(value)) return null
  switch (field) {
    case 'start': return { field: 'start', value: clock.start(value) }
    case 'end': return { field: 'end', value: clock.end(value) }
    case 'duration': return { field: 'end', value: clock.end(shownStart + value) }
  }
}
