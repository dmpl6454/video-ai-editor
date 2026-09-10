/**
 * Where things actually LAND in the rendered output.
 *
 * A clip's `start` is its position in the EDL's own coordinate space ("layout
 * time"). The renderer does not play that space verbatim: `xfade` plays two v1
 * clips at once for the transition's duration, so the renderer emits them
 * OVERLAPPED (`compositor.py`: `cur_dur = cur_dur + seg_dur[i] - tdur`). Every
 * clip after a transition therefore plays EARLIER than its `start` says, and
 * the whole timeline ends sooner.
 *
 * `EDL.recompute_duration()` already accounts for that, so the transport and
 * the ruler are right — but `Timeline.tsx` drew each clip rectangle at raw
 * `start * zoom`, which is the un-overlapped position. On a real report: an 8s
 * clip split at 2/4/6 with transitions at 2.0 and 4.0 renders 7.0s, and the
 * strip was drawn out to 8.0s, so the last second was unreachable AND — worse
 * than the stray tail — every clip after the first transition sat up to a
 * second right of where it plays, putting the playhead over clip 3 while the
 * preview showed clip 4.
 *
 * THE SECOND HALF OF THE SAME BUG (2026-09): only v1 was ever pulled left.
 * Every other lane — captions, text, stickers, PiP, music, voiceover — was
 * positioned by the renderer in raw LAYOUT time (`enable='between(t,start,
 * end)'`, `adelay=start*1000`), so after 12 Zoom In transitions on a 71.86 s
 * timeline (renders 69.46 s) captions and the ducked music bed drifted up to
 * 2.4 s LATE relative to the picture and the speech. The renderer now plays
 * every non-v1 lane at
 *
 *     render_time(t) = t − Σ{ d_i : seam s_i ≤ t }
 *
 * over the v1 transitions it will actually apply, and this module is the
 * desktop's copy of that one rule: overlay lanes are DRAWN at `renderWindow`
 * positions, the playhead/ruler stay in render time, and a pointer position
 * on an overlay lane is converted back with `layoutTime` before it is written
 * to the EDL. Layout time stays the EDL's coordinate space — nothing in the
 * data moves, only where the canvas draws it and how a gesture is decoded.
 *
 * This mirrors `EDL.transition_overlap()` in `edl/schema.py` rule for rule.
 * It has to: if the two ever disagree, the canvas promises a layout the
 * renderer will not produce, which is the whole class of bug this replaces.
 * Kept pure and unit-tested here rather than inline in the component for the
 * same reason `frameWalk.ts` is — it is sign-and-ordering logic that a test
 * catches instantly and a screenshot does not.
 */

import { clipDuration, clipEnd, isMediaClip, type AnyClip, type EDL } from '../types'

export interface LayoutClip {
  id: string
  start: number
  /** EFFECTIVE timeline duration — (out - in) / speed, i.e. `clipDuration()`. */
  duration: number
}

export interface LayoutTransition {
  at: number
  duration: number
}

export interface SeamLayout {
  /** The boundary in EDL (layout) time — matches `v1Cuts[].at`. */
  boundary: number
  /** Seconds the two clips overlap here (0 for a hard cut). */
  overlap: number
  /**
   * Overlap accumulated through AND INCLUDING this seam — the Σ in
   * `render_time`. The clip that starts at this seam is pulled left by it.
   */
  cum: number
  /** Where to draw the seam affordance, in OUTPUT (render) time. */
  outAt: number
}

export interface V1Layout {
  /** clip id -> seconds it is pulled LEFT of its `start`. */
  shift: Map<string, number>
  seams: SeamLayout[]
}

// Same 1ms tolerance as compositor._GAP_EPS, and the same 0.05s boundary
// match the renderer and `transition_overlap()` use. Duplicated rather than
// shared because this is the browser side of the same rule.
const GAP_EPS = 0.001
const SEAM_TOL = 0.05
// "seam ≤ t" with float slack: a caption placed exactly on a seam by
// `auto_caption` arrives as e.g. 13.730000000000002 against a boundary of
// 13.73 (start + (out-in)/speed), and a strict compare would leave THAT one
// cue un-pulled while its neighbours moved — the one-frame stutter is
// exactly what a sync rule exists to remove.
const SEAM_EPS = 1e-6

/**
 * The seam table — the ONE place the applicability rule lives. `v1Layout`
 * (per-clip pull), `renderTime` (any lane) and `layoutTime` (the inverse)
 * are all derived from it, so they cannot disagree about which transitions
 * count.
 */
export function seamTable(
  clips: LayoutClip[], transitions: LayoutTransition[],
): SeamLayout[] {
  const seams: SeamLayout[] = []
  const sorted = [...clips].sort((a, b) => a.start - b.start)
  let cum = 0
  for (let i = 0; i < sorted.length - 1; i++) {
    const cur = sorted[i]
    const nxt = sorted[i + 1]
    const boundary = cur.start + cur.duration
    let overlap = 0
    // A positive GAP is what makes `_v1_segments` insert black filler, and the
    // renderer leaves that seam a hard cut — so no transition applies and
    // nothing is removed. An OVERLAP is not a gap: the renderer packs it with
    // `max(cursor, start)` and the transition IS applied. Testing `abs()` here
    // would wrongly skip a legacy overlapping pair the renderer does charge.
    if (nxt.start - boundary <= GAP_EPS) {
      // First match wins, as in `transition_overlap()`.
      const m = transitions.find((tr) => Math.abs(tr.at - boundary) < SEAM_TOL)
      if (m) {
        // Never claim more than the shorter side can give — xfade cannot
        // overlap further than a clip is long.
        overlap = Math.max(0, Math.min(m.duration, cur.duration, nxt.duration))
      }
    }
    cum += overlap
    // The seam affordance sits at the MIDDLE of the overlap in output time;
    // with no transition the two edges coincide and this is just the cut.
    seams.push({ boundary, overlap, cum, outAt: boundary - cum + overlap / 2 })
  }
  return seams
}

export function v1Layout(
  clips: LayoutClip[], transitions: LayoutTransition[],
): V1Layout {
  const shift = new Map<string, number>()
  const sorted = [...clips].sort((a, b) => a.start - b.start)
  if (sorted.length === 0) return { shift, seams: [] }
  const seams = seamTable(sorted, transitions)
  shift.set(sorted[0].id, 0)
  // The clip AFTER seam i is pulled by everything consumed through seam i.
  // Per-clip rather than `renderTime(start)` on purpose: a legacy pair whose
  // `start` sits a hair BEFORE its boundary is still packed to that boundary
  // by the renderer, and the per-segment charge is what it actually does.
  for (let i = 0; i < seams.length; i++) shift.set(sorted[i + 1].id, seams[i].cum)
  return { shift, seams }
}

/** Convenience: a clip's start in OUTPUT time. */
export function outStart(clip: LayoutClip, shift: Map<string, number>): number {
  return clip.start - (shift.get(clip.id) ?? 0)
}

/**
 * `render_time(t) = t − Σ{ d_i : seam s_i ≤ t }` — where a LAYOUT instant on
 * any non-v1 lane lands in the output. Identity with no transitions.
 *
 * Deliberately NOT monotone across a seam: `t = s − ε` (clip A's tail) maps
 * to `s − ε − D_prev`, while `t = s` (clip B's first frame) maps to `s − D`
 * which is `d` earlier. Both halves land in the same render window — that IS
 * the crossfade, and the picture there is A and B at once, so an overlay
 * pinned to either side is correctly on screen with the frames it was
 * placed against.
 */
export function renderTime(seams: SeamLayout[], t: number): number {
  let consumed = 0
  for (const s of seams) {
    if (s.boundary <= t + SEAM_EPS) consumed = s.cum
    else break
  }
  return t - consumed
}

export interface RenderWindow {
  start: number
  end: number
  /**
   * True when the mapped window has zero or negative length — an overlay
   * living entirely inside the consumed span around a seam (both its edges
   * within `d` of it, on opposite sides). The renderer DROPS such a window
   * rather than inverting it; the canvas draws it as a flagged sliver so it
   * can still be found and dragged out, never as a negative-width rect.
   */
  dropped: boolean
}

/**
 * A layout window `[start, end)` → `[render_time(start), render_time(end))`.
 * A span that crosses a seam SHRINKS by that seam's overlap (its middle was
 * consumed by the crossfade); one wholly inside a consumed span is `dropped`.
 */
export function renderWindow(seams: SeamLayout[], start: number, end: number): RenderWindow {
  const rs = renderTime(seams, start)
  const re = renderTime(seams, end)
  return { start: rs, end: re, dropped: re - rs <= SEAM_EPS }
}

/**
 * The inverse for overlay lanes: a RENDER instant (the pointer, in the
 * ruler's coordinates) → the LAYOUT time a tool argument needs.
 *
 * `render_time` is not injective: clip A's last `d` seconds and clip B's
 * first `d` seconds both land in the crossfade window `[s − D, s − D + d)`.
 * THE RULE: a render position inside a crossfade window maps to the SEAM's
 * layout time `s`. That is where the crossfade lives, it is the only answer
 * that does not silently pick one clip's side over the other, and its own
 * render position is the window's start — so a drop in the middle of a
 * dissolve visibly snaps to where the dissolve begins, with the frames it
 * will actually be composited over. Outside every window it is exactly
 * `r + Σ{ d_i : window_i entirely before r }`, and `renderTime(layoutTime(r))
 * === r` there (tested). Compare `edlTimeFromOutput` (v1's own inverse),
 * which maps the window into clip B's head because on v1 a position is a
 * clip slot, not a coordinate.
 */
export function layoutTime(seams: SeamLayout[], r: number): number {
  let consumed = 0
  for (const s of seams) {
    // This seam's crossfade window in render time. Its start is where clip B
    // begins (`s − cum`); its end is where clip A stops (`s − cumBefore`).
    const wStart = s.boundary - s.cum
    const wEnd = s.boundary - consumed
    if (s.overlap > 0 && r >= wStart - SEAM_EPS && r < wEnd - SEAM_EPS) return s.boundary
    if (r < wEnd - SEAM_EPS) break
    consumed = s.cum
  }
  return r + consumed
}

/**
 * The seam table straight from an EDL: the v1 media clips' effective
 * durations against the track's `transitions`. `[]` without a v1 track, so
 * every mapping above degrades to the identity — a timeline with no
 * transitions must draw exactly as it always did.
 */
export function v1SeamsOf(edl: EDL | null | undefined): SeamLayout[] {
  const v1 = v1InputsOf(edl)
  if (!v1 || !v1.transitions.length) return []
  return seamTable(v1.clips, v1.transitions)
}

/**
 * The per-clip v1 pull straight from an EDL — what `Timeline.tsx` draws v1
 * against, for callers that hold only the EDL (the preview's source-draw
 * path, which must find the frame the composited `<video>` is showing under
 * the render-time playhead). An empty map without a v1 track or without
 * transitions, so `shift.get(id) ?? 0` is the identity there.
 */
export function v1LayoutOf(edl: EDL | null | undefined): V1Layout {
  const v1 = v1InputsOf(edl)
  if (!v1 || !v1.transitions.length) return { shift: new Map<string, number>(), seams: [] }
  return v1Layout(v1.clips, v1.transitions)
}

/**
 * The v1 media clips (effective durations) and the track's transitions, in
 * the shape `seamTable`/`v1Layout` take. One extraction shared by both
 * EDL-level helpers so they cannot disagree about which clips count (media
 * only — a text clip that strayed onto v1 is not a segment the renderer
 * xfades) or how a duration is measured (`clipDuration`: (out−in)/speed).
 */
function v1InputsOf(
  edl: EDL | null | undefined,
): { clips: LayoutClip[]; transitions: LayoutTransition[] } | null {
  const v1 = (edl?.tracks ?? []).find((t) => t.id === 'v1')
  if (!v1) return null
  // types.ts's Track doesn't declare `transitions` (a hand-mirrored schema,
  // incomplete on purpose) — read via the repo's established cast pattern.
  const trs = (v1 as unknown as { transitions?: LayoutTransition[] }).transitions ?? []
  const clips: LayoutClip[] = v1.clips.filter(isMediaClip).map((c) => ({
    id: c.id, start: c.start, duration: clipDuration(c),
  }))
  return { clips, transitions: trs.map((tr) => ({ at: tr.at, duration: tr.duration })) }
}

export interface DrawnSpan {
  /** Left edge in OUTPUT time. */
  start: number
  /** Drawn width in seconds (≥ 0; a dropped overlay reports 0). */
  duration: number
  /** The renderer will not show this clip at all — see `RenderWindow.dropped`. */
  dropped: boolean
}

/**
 * Where a clip on ANY lane is drawn — the one function the Timeline's draw
 * loop AND its hit list call, so a click can never land on a clip other than
 * the one under the pointer. v1 keeps its per-clip pull and its full
 * duration (the overlap is drawn overlapping — that is what a dissolve looks
 * like); every other lane gets the `renderWindow` of its `[start, end)`.
 */
export function drawnSpan(
  trackId: string, clip: AnyClip, layout: V1Layout,
): DrawnSpan {
  if (trackId === 'v1') {
    return {
      start: clip.start - (layout.shift.get(clip.id) ?? 0),
      duration: clipDuration(clip),
      dropped: false,
    }
  }
  const w = renderWindow(layout.seams, clip.start, clipEnd(clip))
  return { start: w.start, duration: Math.max(0, w.end - w.start), dropped: w.dropped }
}

/**
 * The inverse for v1: an OUTPUT time (what a pointer position means once the
 * canvas draws shifted clips) back to the EDL time a tool argument needs.
 *
 * Needed because `move_clip`/`add_clip` take EDL `start`, while the pointer is
 * now over output coordinates. Without it, dropping media onto a v1 lane that
 * already has transitions lands the clip earlier than where it was dropped, by
 * the accumulated overlap. Piecewise: the applicable shift is that of the last
 * clip starting at or before this point, and past the end it is the total.
 * Overlay lanes use `layoutTime` instead (see the crossfade-window rule there).
 */
export function edlTimeFromOutput(
  outTime: number, clips: LayoutClip[], shift: Map<string, number>,
): number {
  const sorted = [...clips].sort((a, b) => a.start - b.start)
  let applicable = 0
  for (const c of sorted) {
    if (outStart(c, shift) <= outTime + 1e-9) applicable = shift.get(c.id) ?? 0
    else break
  }
  return outTime + applicable
}

// ---------------------------------------------------------------------------
// EDL-level conveniences for the places that hold a PLAYHEAD (render time) and
// need a LAYOUT number, or a clip and need where it PLAYS. Each is one line on
// top of the helpers above — they exist so a component cannot pick the wrong
// half of the rule (the v1 per-clip pull vs. the overlay `renderWindow`) at the
// call site, which is exactly how four of them did.
// ---------------------------------------------------------------------------

/**
 * "Add it at the playhead": the playhead is RENDER time (the `<video>`'s
 * clock) and every tool's `start`/`time` argument is LAYOUT time. TextTool
 * and StickerPanel handed the playhead straight through, so once overlays
 * played at `render_time(start)` a text clip added under a playhead sitting
 * after three 0.5 s dissolves landed 1.5 s BEFORE the frame the user was
 * looking at (and 1.5 s left of the playhead on the Timeline). Overlay
 * inverse, not v1's: the new clip goes on an overlay lane.
 */
export function layoutPlayhead(edl: EDL | null | undefined, playhead: number): number {
  return layoutTime(v1SeamsOf(edl), playhead)
}

/**
 * v1's own inverse from an EDL: a render instant → the layout time a v1 tool
 * argument (`split_at` on v1, `move_clip`) needs. Inside a crossfade this
 * maps into clip B's head, because on v1 a position is a clip slot
 * (`edlTimeFromOutput`).
 */
export function v1TimeFromOutput(edl: EDL | null | undefined, r: number): number {
  const v1 = v1InputsOf(edl)
  if (!v1 || !v1.transitions.length) return r
  return edlTimeFromOutput(r, v1.clips, v1Layout(v1.clips, v1.transitions).shift)
}

export interface RenderSpan {
  /** First render instant the clip is on screen / audible. */
  start: number
  /** One past the last (exclusive). `end − start` is the span keyframes run over. */
  end: number
  dropped: boolean
}

/**
 * Where a clip on ANY lane PLAYS, in render time — `drawnSpan` for callers
 * that hold only the EDL. v1: `start − shift` for its full duration; every
 * other lane: `renderWindow(start, end)`. This is the origin of a clip-local
 * keyframe clock on the renderer (`text_overlay`/`pip.py` evaluate `(t −
 * rs)`), on StickerLayer (`renderLocal`) and on the composited v1 picture.
 */
export function renderSpanOf(edl: EDL | null | undefined, trackId: string, clip: AnyClip): RenderSpan {
  const span = drawnSpan(trackId, clip, v1LayoutOf(edl))
  return { start: span.start, end: span.start + span.duration, dropped: span.dropped }
}

/**
 * Clip-local time at the playhead, clamped to the clip's RENDER span —
 * what Properties shows and what `add_keyframe` / `set_clip_transform`
 * take as `time`. It used `playhead − clip.start` (layout-local) for every
 * lane, so after a 0.5 s dissolve a sticker's "Add keyframe" wrote a key
 * 0.5 s before the frame the user posed it on, and the panel displayed the
 * pose of a different instant than the one on screen. Clamped to the render
 * span rather than the layout span for the same reason the renderer clamps
 * an anim duration against `(re − rs)`: that is how long the clip is on
 * screen, and a key past it is unreachable.
 */
export function clipLocalTime(
  edl: EDL | null | undefined, trackId: string, clip: AnyClip, playhead: number,
): number {
  const span = renderSpanOf(edl, trackId, clip)
  return Math.min(Math.max(0, playhead - span.start), Math.max(0, span.end - span.start))
}

/**
 * The v1 clip under a RENDER instant: each clip tested at its OUTPUT
 * position (`start − shift`), LAST match wins so a crossfade window answers
 * clip B — the rule mobile/lib/timeline.ts `clipAt` documents, and the one
 * the composited `<video>` actually shows (B is fading in there). The old
 * layout-time test (`c.start <= t < clipEnd(c)`) answered B for the first
 * `Σoverlap` seconds of C's picture, so a framing drag on C was refused —
 * or reframed B — for 2.2 s per clip on the twelve-transition session.
 */
export function v1ClipAt<T extends AnyClip>(
  clips: readonly T[], shift: Map<string, number>, t: number,
): T | undefined {
  let found: T | undefined
  for (const c of [...clips].sort((a, b) => a.start - b.start)) {
    const s = c.start - (shift.get(c.id) ?? 0)
    if (s <= t + SEAM_EPS && t < s + clipDuration(c) - SEAM_EPS) found = c
  }
  return found
}

// ---------------------------------------------------------------------------
// The live preview's adapters (StickerLayer.tsx; TextLayer.tsx uses
// `renderWindow` directly). Thin wrappers over the table above, never a
// second rule.
// ---------------------------------------------------------------------------

/**
 * Is the layout window `[start, end]` on screen at RENDER instant `t`?
 * A window the renderer DROPS (wholly inside a consumed span, e.g. an overlay
 * covering exactly clip A's crossfaded tail) is never active here either —
 * the preview must not show something the export will not contain.
 * `endInclusive` preserves each caller's existing edge rule: stickers test
 * `t <= end` (matching the server's `between()`), PiP clips `t < end`.
 */
export function activeInRender(
  seams: SeamLayout[], start: number, end: number, t: number, endInclusive: boolean,
): boolean {
  const w = renderWindow(seams, start, end)
  if (w.dropped) return false
  return w.start <= t && (endInclusive ? t <= w.end : t < w.end)
}

/**
 * Clip-local time on the render clock: how far into the clip the picture is
 * at RENDER instant `t`. This is what keyframes and a PiP's media offset run
 * on — the renderer evaluates both relative to the clip's enable window,
 * which begins at `render_time(start)`, not at `start`.
 */
export function renderLocal(seams: SeamLayout[], start: number, t: number): number {
  return t - renderTime(seams, start)
}

/**
 * The instant to hand a geometry helper that subtracts the LAYOUT `start`
 * itself (`stickerGeom`/`pipGeom` in lib/overlay compute `t - clip.start`
 * internally): chosen so that `layoutClock − start === renderLocal`. Those
 * helpers are shared with tests pinned to the server's box tables, so they
 * keep their signature and the conversion happens at the call site.
 */
export function layoutClock(seams: SeamLayout[], start: number, t: number): number {
  return start + renderLocal(seams, start, t)
}
