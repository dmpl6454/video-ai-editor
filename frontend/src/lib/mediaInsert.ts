// "Put this library item on the timeline" without a drag (wave-B review,
// QA-010): a Media row was drag-only — double-click did nothing and the row
// was not focusable, so a keyboard user could not reuse an imported file at
// all. Enter / double-click / the row's "Add to timeline" button insert it AT
// THE PLAYHEAD: picture on the main track, sound on the Music lane — the same
// placement rule the panel drag uses (Timeline.onCanvasDrop).

import { toFrameGrid } from './frameStep'
import { layoutPlayhead, v1TimeFromOutput } from './timelineLayout'
import type { EDL } from '../types'

/** Mirror of ingest/still.py STILL_DEFAULT_SECONDS (lib/laneDrop). */
const STILL_SECONDS = 5

export interface InsertableRow {
  src: string
  kind: 'video' | 'audio'
  duration: number | null
  still?: boolean
  missing?: boolean
}

/** The add_clip for `row` at the playhead, or null for an offline item. */
export function insertAtPlayhead(row: InsertableRow, edl: EDL | null | undefined, playhead: number)
  : { tool: 'add_clip'; args: { track: string; src: string; in: number; out: number; start: number } } | null {
  if (row.missing || !row.src) return null
  const fps = edl?.canvas?.fps
  const audio = row.kind === 'audio'
  const track = audio ? (edl?.tracks.find((t) => t.type === 'music')?.id ?? 'music') : 'v1'
  // The playhead is RENDER time; add_clip takes LAYOUT time — v1's own
  // inverse on the main track, the overlay inverse on every other lane.
  const at = audio ? layoutPlayhead(edl, playhead) : v1TimeFromOutput(edl, playhead)
  const dur = row.still ? STILL_SECONDS : (row.duration && row.duration > 0 ? row.duration : STILL_SECONDS)
  return { tool: 'add_clip', args: { track, src: row.src, in: 0, out: dur, start: toFrameGrid(Math.max(0, at), fps) } }
}

/** What a drop / insert at layout time `at` does on the MAIN track (Final
 *  QA): the main track is magnetic, so a time inside it INSERTS there — the
 *  clip under `at` is split (add_clip's server rule) — and a time at or past
 *  its end appends. `under` is the clip that gets split, if any. */
export function mainLaneInsert<C extends { start: number }>(clips: readonly C[], at: number,
  footprint: (c: C) => number): { insert: boolean; under: C | null } {
  const end = clips.reduce((m, c) => Math.max(m, c.start + footprint(c)), 0)
  const under = clips.find((c) => c.start + 1e-6 < at && at < c.start + footprint(c) - 1e-6) ?? null
  return { insert: clips.length > 0 && at < end - 1e-6, under }
}
