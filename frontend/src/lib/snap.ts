// Timeline snapping (QA-050) and the client's frame grid for gestures (QA-049).
//
// The snap targets were 0, the playhead and clip edges — never a marker, which
// is the one thing an editor drops specifically to snap to. The result only
// said WHERE it snapped, not WHETHER, so the drag preview could not show the
// snap it was about to make. And every gesture produced off-grid seconds
// (a ruler click at x=401 gave 4.0125 s, a drag landed at 5.205 s) because
// nothing rounded pixels to frames on the client.

import { clipDuration, isMediaClip, type EDL } from '../types'

export type SnapKind = 'start' | 'playhead' | 'marker' | 'edge'

export interface SnapTarget { t: number; kind: SnapKind }

export interface SnapResult {
  t: number
  /** The target it snapped to, or null when it stayed where it was put. */
  target: SnapTarget | null
}

/**
 * Every snap target in LAYOUT time: the start, the playhead (already decoded
 * to layout by the caller), each marker, and both edges of every clip except
 * `ignoreClipId` (the one being dragged).
 */
export function snapTargets(edl: EDL | null | undefined, playheadLayout: number | null,
                            ignoreClipId?: string): SnapTarget[] {
  const out: SnapTarget[] = [{ t: 0, kind: 'start' }]
  if (playheadLayout != null && Number.isFinite(playheadLayout)) out.push({ t: playheadLayout, kind: 'playhead' })
  for (const m of (edl?.markers ?? []) as { time: number }[]) {
    if (Number.isFinite(m.time)) out.push({ t: m.time, kind: 'marker' })
  }
  for (const tk of edl?.tracks ?? []) {
    for (const c of tk.clips) {
      if (ignoreClipId && c.id === ignoreClipId) continue
      const cs = (c as { start?: number }).start ?? 0
      const ce = isMediaClip(c) ? cs + clipDuration(c) : ((c as { end?: number }).end ?? cs)
      out.push({ t: cs, kind: 'edge' }, { t: ce, kind: 'edge' })
    }
  }
  return out
}

/** Snap `t` to the nearest target within `radiusSec`. Markers and the
 *  playhead win a tie against a clip edge — they are placed on purpose. */
export function snapTo(t: number, targets: readonly SnapTarget[], radiusSec: number): SnapResult {
  let best: SnapTarget | null = null
  let bestD = radiusSec
  const rank = (k: SnapKind) => (k === 'marker' || k === 'playhead' ? 0 : 1)
  for (const cand of targets) {
    const d = Math.abs(t - cand.t)
    if (d < bestD - 1e-9 || (best && Math.abs(d - bestD) <= 1e-9 && rank(cand.kind) < rank(best.kind))) {
      best = cand
      bestD = d
    }
  }
  return best ? { t: best.t, target: best } : { t, target: null }
}

/** The snap guide's caption while dragging. */
export function snapLabel(kind: SnapKind): string {
  return kind === 'playhead' ? 'Playhead' : kind === 'marker' ? 'Marker' : kind === 'start' ? 'Start' : 'Clip edge'
}
