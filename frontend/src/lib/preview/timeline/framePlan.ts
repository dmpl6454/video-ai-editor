// The v1 FRAME PLAN — a port of the compositor's layout rules (instant
// preview spec §6 R3): `clip_frames`, `_v1_frame_plan`, `seam_table_for`,
// `seam_matching`, the reversed-clip timing swap (`render/reverse.py`
// `reversed_view`) and the segment/seam pairing `_build_filter_complex` does.
// Pinned, with frameMap.ts, by tests/goldens/frame_map/*.json.
//
// Float arithmetic here is deliberately the SAME operations in the SAME order
// as the Python (`start + (out - in) / speed`, `nxt.start - boundary > 1e-3`):
// IEEE doubles then agree bit for bit, and every seconds→frames step goes
// through timebase.ts, which is exact.

import {
  floorToFrame, frameOf, timeOf, type FpsLike,
} from './timebase'

/** A v1 media clip as the EDL JSON carries it (`GET /edl`, dispatch). */
export interface EdlClip {
  id: string
  src: string
  in?: number
  out?: number
  start?: number
  /** A number, or a curve dict (renders at 1x — `Clip.speed_factor`). */
  speed?: number | Record<string, unknown> | null
  reverse?: boolean
  audio?: { keep_pitch?: boolean; [k: string]: unknown } | null
  [k: string]: unknown
}

export interface EdlTransition { at: number; type?: string; duration?: number }

export interface EdlTrack {
  id: string
  type?: string
  clips: unknown[]
  transitions?: EdlTransition[]
  [k: string]: unknown
}

export interface EdlLike {
  duration?: number
  canvas?: { fps?: number; [k: string]: unknown }
  tracks?: EdlTrack[]
  [k: string]: unknown
}

/** Compositor `_GAP_EPS` / schema `V1_GAP_EPS_S`. */
export const V1_GAP_EPS_S = 0.001
/** schema `SEAM_MATCH_TOL_S`. */
export const SEAM_MATCH_TOL_S = 0.05
/** Transition.duration default. */
const DEFAULT_TRANSITION_S = 0.5

export const clipIn = (c: EdlClip): number => c.in ?? 0
export const clipOut = (c: EdlClip): number => c.out ?? 0
export const clipStart = (c: EdlClip): number => c.start ?? 0

/** `Clip.speed_factor`: the scalar speed, 1 for unset / <= 0 / curve dicts. */
export function speedFactor(speed: EdlClip['speed']): number {
  return typeof speed === 'number' && speed > 0 ? speed : 1
}

/** `Clip.effective_duration`: timeline seconds, (out − in) / speed. */
export function effectiveDuration(c: EdlClip): number {
  return Math.max(0, clipOut(c) - clipIn(c)) / speedFactor(c.speed)
}

/** `compositor.clip_frames`: frames the clip occupies (>= 1). */
export function clipFrames(c: EdlClip, fps: FpsLike): number {
  return Math.max(1, frameOf(effectiveDuration(c), fps))
}

/** A media clip (`isinstance(c, Clip)`): has a `src` and an `out`. */
export function isMediaClip(c: unknown): c is EdlClip {
  return typeof c === 'object' && c !== null && 'src' in c && 'out' in c && !('text' in c)
}

/** `compositor._video_clips`: v1 media clips sorted by start (stable). */
export function videoClips(edl: EdlLike): EdlClip[] {
  const v1 = (edl.tracks ?? []).find((t) => t.id === 'v1')
  if (!v1) return []
  return v1.clips.filter(isMediaClip).map((c, i) => [c, i] as const)
    .sort((a, b) => (clipStart(a[0]) - clipStart(b[0])) || (a[1] - b[1]))
    .map(([c]) => c)
}

export function v1Transitions(edl: EdlLike): EdlTransition[] {
  const v1 = (edl.tracks ?? []).find((t) => t.id === 'v1')
  return v1?.transitions ?? []
}

/** `seam_matching`: the FIRST record within 0.05 s of `boundary`. */
export function seamMatching(transitions: EdlTransition[], boundary: number): EdlTransition | null {
  return transitions.find((tr) => Math.abs(tr.at - boundary) < SEAM_MATCH_TOL_S) ?? null
}

/** The cost `seam_table_for` charges a matched seam: the record's duration
 *  clamped to the shorter side, then (with `fps`) a whole number of frames —
 *  at least one, never more than the shorter side's whole frames. */
export function seamCost(duration: number | undefined, shorter: number, fps?: FpsLike): number {
  let cost = Math.max(0, Math.min(duration ?? DEFAULT_TRANSITION_S, shorter))
  if (fps !== undefined && fps !== null && cost > 0) {
    cost = timeOf(Math.max(1, frameOf(cost, fps)), fps)
    if (cost > shorter + 1e-9) cost = floorToFrame(shorter, fps)
  }
  return cost
}

/** A v1 segment as the seam rule sees it: layout start + EFFECTIVE duration. */
export interface SeamSpan { readonly start: number; readonly duration: number }

/** One adjacent pair of `seam_table_for`: its boundary and what the renderer
 *  charges there (0 for a hard cut: a gap, or no matching record). */
export interface SeamCharge { boundary: number; cost: number }

/** The ONE seam rule (spec R3): for clips ALREADY in timeline order, one
 *  entry per adjacent pair. A positive gap (> V1_GAP_EPS_S) is a hard cut —
 *  the renderer inserts black and applies nothing; an overlap is not a gap.
 *  The FIRST record within SEAM_MATCH_TOL_S of the boundary is charged,
 *  clamped to the shorter side (and to whole frames with `fps`).
 *  `seamTableFor` (engine, classifier) and `timelineLayout.seamTable` (the
 *  timeline UI) are both thin views of this. */
export function seamCharges(
  ordered: readonly SeamSpan[], transitions: readonly EdlTransition[], fps?: FpsLike,
): SeamCharge[] {
  const out: SeamCharge[] = []
  for (let i = 0; i + 1 < ordered.length; i++) {
    const cur = ordered[i]
    const nxt = ordered[i + 1]
    const boundary = cur.start + cur.duration
    let cost = 0
    if (nxt.start - boundary <= V1_GAP_EPS_S) {
      const match = seamMatching(transitions as EdlTransition[], boundary)
      if (match) cost = seamCost(match.duration, Math.min(cur.duration, nxt.duration), fps)
    }
    out.push({ boundary, cost })
  }
  return out
}

/** `seam_table_for`: `[boundary, cost]` for every seam the renderer
 *  cross-fades, ascending. */
export function seamTableFor(
  clips: EdlClip[], transitions: EdlTransition[], fps?: FpsLike,
): Array<[number, number]> {
  if (!transitions.length) return []
  const ordered = clips.map((c, i) => [c, i] as const)
    .sort((a, b) => (clipStart(a[0]) - clipStart(b[0])) || (a[1] - b[1]))
    .map(([c]) => ({ start: clipStart(c), duration: effectiveDuration(c) }))
  return seamCharges(ordered, transitions, fps)
    .filter((s) => s.cost > 0).map((s) => [s.boundary, s.cost] as [number, number])
}

export type PlanSegment =
  | { kind: 'clip'; clip: number; frames: number }
  | { kind: 'gap'; clip: null; frames: number }

/** `_v1_frame_plan`: clips and black gaps on the frame grid. */
export function v1FramePlan(clips: EdlClip[], totalDuration: number, fps: FpsLike): PlanSegment[] {
  const plan: PlanSegment[] = []
  let cursor = 0
  clips.forEach((c, i) => {
    const sf = frameOf(clipStart(c), fps)
    if (sf - cursor >= 1) {
      plan.push({ kind: 'gap', clip: null, frames: sf - cursor })
      cursor = sf
    }
    const n = clipFrames(c, fps)
    plan.push({ kind: 'clip', clip: i, frames: n })
    cursor = Math.max(cursor, sf) + n
  })
  const tail = frameOf(totalDuration, fps) - cursor
  if (tail >= 1) plan.push({ kind: 'gap', clip: null, frames: tail })
  return plan
}

/** `reverse.reversed_frames`: frames the reversed intermediate holds. */
export function reversedFrameCount(c: EdlClip, fps: FpsLike): number {
  return Math.max(1, frameOf(clipOut(c) - clipIn(c), fps))
}

/** `reverse.reversed_view` timing: in 0, out = the intermediate's length. */
export function reversedView(c: EdlClip, fps: FpsLike): EdlClip {
  return { ...c, in: 0, out: timeOf(reversedFrameCount(c, fps), fps), reverse: false }
}

/** A seam the compositor cross-fades, between two ADJACENT clip segments. */
export interface PlanSeam { left: number; right: number; frames: number; type: string }

export interface PlanView {
  fps: FpsLike
  /** v1 clips as the compositor plans them (reversed clips: intermediate timing). */
  planned: EdlClip[]
  /** The ORIGINAL clips, same order. */
  originals: EdlClip[]
  transitions: EdlTransition[]
  seams: Array<[number, number]>
  totalDuration: number
  plan: PlanSegment[]
  /** plan segment index → seam frames, for the seam AFTER that segment. */
  segTrans: Map<number, number>
  /** plan segment index → the same seam's cost in seconds (acrossfade). */
  segCost: Map<number, number>
  seamRows: PlanSeam[]
}

/** Everything `build_program_map` derives before it picks a single frame. */
export function planView(edl: EdlLike, fpsArg?: FpsLike): PlanView {
  const fps = fpsArg ?? edl.canvas?.fps ?? 30
  const originalsSorted = videoClips(edl)
  const anyReversed = originalsSorted.some((c) => c.reverse)
  let planned = originalsSorted
  let originals = originalsSorted
  if (anyReversed) {
    // Mirror `with_reversed_sources` + `_video_clips` on the swapped copy,
    // then map back by id (last clip with an id wins, as a dict would).
    const view = videoClips({
      ...edl,
      tracks: (edl.tracks ?? []).map((t) => (t.id !== 'v1' ? t : {
        ...t, clips: t.clips.map((c) => (isMediaClip(c) && c.reverse ? reversedView(c, fps) : c)),
      })),
    })
    const byId = new Map(originalsSorted.map((c) => [c.id, c] as const))
    planned = view
    originals = view.map((c) => byId.get(c.id)!)
  }
  const transitions = v1Transitions(edl)
  const seams = transitions.length ? seamTableFor(planned, transitions, fps) : []
  let totalDuration = Math.max(0, (edl.duration ?? 0) + seams.reduce((s, [, d]) => s + d, 0))
  if (!planned.length) totalDuration = Math.max(1, totalDuration)
  const plan = v1FramePlan(planned, totalDuration, fps)

  const segOfClip = new Map<number, number>()
  plan.forEach((seg, si) => { if (seg.kind === 'clip') segOfClip.set(seg.clip, si) })
  const segTrans = new Map<number, number>()
  const segCost = new Map<number, number>()
  const seamRows: PlanSeam[] = []
  for (let idx = 0; idx + 1 < planned.length; idx++) {
    const si = segOfClip.get(idx)
    if (si === undefined || segOfClip.get(idx + 1) !== si + 1) continue
    const c = planned[idx]
    const boundary = clipStart(c) + effectiveDuration(c)
    const hit = seams.find(([seam]) => Math.abs(seam - boundary) < 0.001)
    const cost = hit ? hit[1] : 0
    const record = seamMatching(transitions, boundary)
    if (cost > 0 && record) {
      const d = frameOf(cost, fps)
      segTrans.set(si, d)
      segCost.set(si, cost)
      seamRows.push({ left: idx, right: idx + 1, frames: d, type: record.type ?? 'fade' })
    }
  }
  return { fps, planned, originals, transitions, seams, totalDuration, plan, segTrans, segCost, seamRows }
}
