// The Inspector's Timing section — ONE layout and ONE rule for every clip
// (QA-048, lib/clipTiming): Start moves the clip, End trims the end, Duration
// trims the end. A media clip also shows which part of its file plays
// (Source in / out). Every field is a TimecodeField (HH:MM:SS:FF, or typed
// seconds / frames), and a refused edit restores the field.
//
// Start / End / Duration speak the RULER's clock (`clock`, from
// lib/timelineLayout `timingClockOf`): after a v1 transition the EDL `start`
// is not where the clip plays, and showing it made a typed Start land half a
// second (per dissolve) away from where it was typed. Source in / out are
// file seconds and need no clock.

import { TimecodeField } from './TimecodeField'
import {
  MIN_SPAN_S, clockedEdit, mediaTimelineDuration, mediaTimingEdit, overlayTimingEdit, shownTiming,
  type FieldClock, type MediaField, type MediaSpan, type OverlaySpan, type TimelineField, type TimingEdit,
} from '../lib/clipTiming'

const LAYOUT: FieldClock = { show: (t) => t, start: (r) => r, end: (r) => r }

type Send = (tool: TimingEdit['tool'], args: Record<string, unknown>) => unknown

function Field({ label, value, min, onCommit }: {
  label: string; value: number; min?: number; onCommit: (v: number) => unknown
}) {
  return (
    <div className="field">
      <label>{label}</label>
      <TimecodeField ariaLabel={label} value={value} min={min} onCommit={onCommit} />
    </div>
  )
}

function Triple({ start, end, commit }: {
  start: number; end: number; commit: (f: TimelineField, v: number) => unknown
}) {
  return (
    <>
      <div className="row two">
        <Field label="Start" value={start} min={0} onCommit={(v) => commit('start', v)} />
        <Field label="End" value={end} min={0} onCommit={(v) => commit('end', v)} />
      </div>
      <div className="row two">
        <Field label="Duration" value={Math.max(0, end - start)} min={MIN_SPAN_S} onCommit={(v) => commit('duration', v)} />
      </div>
    </>
  )
}

/** A refused edit (null) resolves falsy, so the field restores itself. */
const run = (send: Send, edit: TimingEdit | null) => (edit ? send(edit.tool, { ...edit.args }) : null)

/** Text and stickers: the fields inside the Inspector's Timing section. */
export function OverlayTiming({ clipId, span, send, clock = LAYOUT }: {
  clipId: string; span: OverlaySpan; send: Send; clock?: FieldClock
}) {
  const withId: Send = (tool, args) => send(tool, { clip_id: clipId, ...args })
  const shown = shownTiming(span, clock)
  const commit = (f: TimelineField, v: number) => {
    const e = clockedEdit(f, v, shown.start, clock)
    return run(withId, e ? overlayTimingEdit(span, e.field, e.value) : null)
  }
  return <Triple start={shown.start} end={shown.end} commit={commit} />
}

/** Media clips: the same triple, then which part of the file plays. */
export function MediaTiming({ clipId, span, send, clock = LAYOUT }: {
  clipId: string; span: MediaSpan; send: Send; clock?: FieldClock
}) {
  const withId: Send = (tool, args) => send(tool, { clip_id: clipId, ...args })
  const commit = (f: MediaField, v: number) => run(withId, mediaTimingEdit(span, f, v))
  const shown = shownTiming({ start: span.start, end: span.start + mediaTimelineDuration(span) }, clock)
  const commitClocked = (f: TimelineField, v: number) => {
    const e = clockedEdit(f, v, shown.start, clock)
    return e ? commit(e.field, e.value) : null
  }
  return (
    <>
      <Triple start={shown.start} end={shown.end} commit={commitClocked} />
      <div className="row two" title="Which part of the file plays">
        <Field label="Source in" value={span.in} min={0} onCommit={(v) => commit('in', v)} />
        <Field label="Source out" value={span.out} min={0} onCommit={(v) => commit('out', v)} />
      </div>
    </>
  )
}
