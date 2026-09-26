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

export type TimelineField = 'start' | 'end' | 'duration'
export type MediaField = TimelineField | 'in' | 'out'

/** The shortest span a field may leave (the backend floors overlays at 0.1 s). */
export const MIN_SPAN_S = 0.1

export interface OverlaySpan { start: number; end: number }
export interface MediaSpan { in: number; out: number; start: number; speed: number }

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
      return { tool: 'trim_clip', args: { out: m.in + len * speed } }
    }
  }
}
