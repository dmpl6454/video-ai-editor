// The Text tool's inserts (docs/design/LEFT_RAIL_SPEC.md §2.5, R2): the
// one-click default text, the style gallery and the four templates. Moved
// verbatim out of the top bar's TextTool so the Text panel — and, from R4, the
// ⌥T `addText` command — take the SAME path.
//
// Two contract details from agent/dispatch.py's add_text handler:
//   - It returns {summary, id}: the id selects + flashes the new clip
//     (lib/newClip) so the Properties inspector opens on it at once.
//   - By default it REPLACES any same-role text clip on the same track whose
//     [start, end) overlaps the new one (the "double subtitle" guard for
//     chat/MCP callers). A UI insert must never silently delete content, so
//     every insert here passes allow_stack: true — and a stacked text goes on
//     the first text lane free for its window, a new "Text 2"… if none is
//     (final sweep 2: two texts on one lane drew as one block).
//
// Templates go through `apply_text_template` (same handler file):
// {name, start, end, fields:{text, hashtag, handle}}. A template that needs a
// field the user has not typed is disabled rather than producing an empty clip.
import type { EDL } from '../types'
import { layoutPlayhead } from './timelineLayout'
import { defaultOverlayEnd, videoContentEnd } from './timelineExtent'
import { textStyleArgs, type TextStylePreset } from './textStyles'

export interface TextTemplate {
  /** apply_text_template's preset name. */
  name: string
  label: string
  title: string
  /** Disabled until the field has text (it would otherwise be empty). */
  needsField: boolean
}

export const TEXT_TEMPLATES: readonly TextTemplate[] = [
  { name: 'countdown_3_2_1', label: '3 · 2 · 1', title: 'Center-screen countdown (pop in, fade out)', needsField: false },
  { name: 'callout_arrow', label: 'Callout →', title: 'Arrow callout label (uses the text above, or → alone)', needsField: false },
  { name: 'hashtag_chunky', label: '#Hashtag', title: 'Chunky hashtag near the bottom — type the hashtag above first', needsField: true },
  { name: 'watermark_handle', label: '@Handle', title: 'Corner watermark — type your handle above first', needsField: true },
]

/** The default overlay length (s) before it is clipped to the picture. */
export const DEFAULT_TEXT_S = 3

/** Whether a template can run with what is typed in the field. */
export function templateEnabled(t: TextTemplate, fieldText: string): boolean {
  return !t.needsField || fieldText.trim().length > 0
}

/** Where a new overlay goes: [start, end) in LAYOUT time.
 *
 *  The playhead is RENDER time and `start` is LAYOUT time
 *  (lib/timelineLayout `layoutPlayhead`, the inverse the Timeline uses for a
 *  sticker drop): handing the playhead straight through put the new clip
 *  Σoverlap seconds before the frame under the playhead once overlays played
 *  on the render clock (1.5 s early after three 0.5 s dissolves).
 *
 *  The 3 s default must not outlive the PICTURE: dropped at the playhead it
 *  used to run past the end of v1, and since edl.duration is a max over every
 *  track that lone overlay held the timeline open and playback ran on into
 *  black ("the total duration was 4 secs but the video ran to 4.4 secs"). */
export function overlaySpan(edl: EDL | null | undefined, playhead: number): { start: number; end: number } {
  const start = layoutPlayhead(edl, playhead)
  return { start, end: defaultOverlayEnd(start, DEFAULT_TEXT_S, videoContentEnd(edl ?? null)) }
}

/** Whether a caption cue is on screen during [start, end) (review RE). */
export function captionsOverlap(edl: EDL | null | undefined, start: number, end: number): boolean {
  const cap = edl?.tracks.find((t) => t.type === 'captions')
  return !!cap?.clips.some((c) => {
    const x = c as { start?: number; end?: number }
    return (x.start ?? 0) < end && (x.end ?? 0) > start
  })
}

/** add_text for the one-click default text. Review RE: with captions on
 *  screen the default place IS the caption band (both unreadable, and the
 *  export draws the caption over it) — it goes to the upper third instead. */
export function defaultTextArgs(start: number, end: number, edl?: EDL | null): Record<string, unknown> {
  const args: Record<string, unknown> = {
    text: 'Your text',
    start,
    end,
    role: 'super',
    // Never replace an existing overlay from the UI tool (see the header note).
    allow_stack: true,
  }
  return { ...args, ...captionClearPlacement(edl, start, end, 'super') }
}

/** Roles the renderer anchors in the lower band (`_y_for_role` → 0.75·h),
 *  the band captions occupy. hook (mid), lower_third and watermark differ. */
const LOWER_BAND_ROLES = new Set(['super', 'label', 'default'])

/** x/y that keep a new overlay off the captions: the upper third when a cue
 *  is on screen during [start, end) and the role would land in the caption
 *  band; `{}` (the role places it) otherwise. One rule for the default text
 *  and the style gallery (Final QA: five gallery looks landed on captions). */
export function captionClearPlacement(
  edl: EDL | null | undefined, start: number, end: number, role: unknown,
): { x?: number; y?: number } {
  if (!edl || !LOWER_BAND_ROLES.has(typeof role === 'string' ? role : 'default')) return {}
  if (!captionsOverlap(edl, start, end)) return {}
  return { x: Math.round(edl.canvas.w / 2), y: Math.round(edl.canvas.h * UPPER_THIRD) }
}

/** Where the default text sits when captions hold the lower band. */
export const UPPER_THIRD = 0.3

/** apply_text_template for a template. It picks the slot it needs per
 *  preset; the one typed value goes into all three so the UI stays one field. */
export function templateArgs(name: string, fieldText: string, start: number, end: number): Record<string, unknown> {
  const v = fieldText.trim()
  // allow_stack: never replace a text from the UI; over another text the
  // server puts the new one on its own lane (`_free_text_lane`).
  return { name, start, end, fields: { text: v, hashtag: v, handle: v }, allow_stack: true }
}

/** The editor state an insert reads, and how it dispatches. */
export interface TextInsertDeps {
  state(): { edl: EDL | null; playhead: number }
  dispatch(tool: string, args: Record<string, unknown>): Promise<{ result: unknown } | null>
  select(result: unknown): Promise<void>
}

async function insert(deps: TextInsertDeps, tool: string, args: (s: number, e: number) => Record<string, unknown>) {
  const { edl, playhead } = deps.state()
  const { start, end } = overlaySpan(edl, playhead)
  const res = await deps.dispatch(tool, args(start, end))
  if (res) await deps.select(res.result)
  return res
}

/** "Add text at playhead": the default text, selected (clear of captions). */
export function insertDefaultText(deps: TextInsertDeps) {
  const { edl } = deps.state()
  return insert(deps, 'add_text', (s, e) => defaultTextArgs(s, e, edl))
}

/** A gallery look: ONE add_text carrying its whole style (one undo step, QA-078). */
export function insertTextStyle(deps: TextInsertDeps, p: TextStylePreset, fieldText: string) {
  const { edl } = deps.state()
  return insert(deps, 'add_text', (s, e) => ({
    ...textStyleArgs(p, fieldText, s, e), ...captionClearPlacement(edl, s, e, p.args.role),
  }))
}

/** A template, filled from the field. */
export function insertTextTemplate(deps: TextInsertDeps, name: string, fieldText: string) {
  return insert(deps, 'apply_text_template', (s, e) => templateArgs(name, fieldText, s, e))
}
