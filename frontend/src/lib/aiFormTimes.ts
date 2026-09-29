// The AI panel's 'time' fields are RULER times; the tools want LAYOUT times.
//
// Every 'time' widget is seeded from the playhead or the In/Out marks and its
// "⟵ Playhead" button copies the playhead — all RENDER time (the ruler, the
// <video>'s clock; the marks are set from `s.playhead`). But `cut_range`,
// `add_super_text`, `add_lower_third` and `tts_voiceover` take LAYOUT
// seconds. After a transition the two differ by every upstream crossfade, so
// Cut range from marks 14/15 behind one 0.48 s fade removed source 14.0–15.0
// (0.48 s the user kept, and kept 0.48 s inside the marks), and a lower third
// "at the playhead" 06:00 behind two 0.5 s dissolves showed at 05:00 (final
// QA). The form keeps showing ruler numbers — the ones the user set and can
// compare with the ruler — and they are decoded here, once, at submit.
//
// Which inverse (the rule `splitTimeFor` / `mediaInsert` use): on v1 a
// position is a clip SLOT (`v1TimeFromOutput`, a crossfade maps into clip B's
// head — so cutting from marks cuts where "Split at playhead" at those marks
// would). Every other target — v2, a text overlay, a NEW voiceover clip — is
// placed with the overlay inverse (`layoutPlayhead`), which is also what the
// recorder and importer use for a voiceover (`voCapture.voLayoutStart`): a
// sound clip plays from `render_time(start)`.

import type { EDL } from '../types'
import type { Field } from './schemaForm'
import { layoutPlayhead, v1TimeFromOutput } from './timelineLayout'

const round6 = (n: number) => Math.round(n * 1e6) / 1e6

/** Ruler seconds `r` → the layout seconds a tool argument on `track` needs. */
export function rulerToLayout(edl: EDL | null | undefined, track: string | undefined, r: number): number {
  return round6(track === 'v1' ? v1TimeFromOutput(edl, r) : layoutPlayhead(edl, r))
}

/**
 * `buildArgs` output → the args to dispatch: each 'time' field decoded from
 * ruler to layout time. The lane is the tool's own `track` arg when it has one
 * (`cut_range`), else an overlay lane. Returns the same object when there is
 * nothing to decode.
 */
export function layoutTimeArgs(
  fields: readonly Field[], args: Record<string, unknown>, edl: EDL | null | undefined,
): Record<string, unknown> {
  const track = typeof args.track === 'string' ? args.track : undefined
  let out = args
  for (const f of fields) {
    const v = args[f.name]
    if (f.widget !== 'time' || typeof v !== 'number' || !Number.isFinite(v)) continue
    const t = rulerToLayout(edl, track, v)
    if (t !== v) out = { ...out, [f.name]: t }
  }
  return out
}
