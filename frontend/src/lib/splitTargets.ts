/**
 * What ⌘B / "Split at playhead" cuts, and WHERE in layout time.
 *
 * The playhead is RENDER time; `split_at` takes a LAYOUT `time`. The store
 * used the playhead for both the containment test (`c.start <= t <
 * clipEnd(c)`) and the dispatched time, on every lane. For v1 that was off by
 * the clip's per-clip pull; for overlay lanes it became wrong the moment they
 * were drawn and played at render positions: a caption at layout 20.0–23.0
 * after 1.5 s of consumed overlap is on screen at render 18.5, so with the
 * playhead at render 19.0 the layout test said "not inside the caption" and
 * cut v1 instead — at layout 19.0, 1.5 s before the frame shown.
 *
 * Pure, so the rule is a table test rather than a store mock: v1 decodes
 * through v1's own inverse (`v1TimeFromOutput` — a crossfade maps into clip
 * B's head, a slot not a coordinate), every other lane through `layoutTime`
 * (a crossfade snaps to the seam). Both are the inverses the Timeline already
 * uses for drops on those lanes, so a split lands where a drop would.
 *
 * SOUND lanes (vo, music, type "audio") are the exception: a sound clip
 * plays WHOLE from where its run starts (`soundPull`/`soundSpan`), so the
 * overlay inverse — which adds every upstream overlap — cut a voiceover one
 * second after the playhead behind two 0.5 s dissolves (final sweep 2). A
 * sound clip decodes through its own pull: `t = playhead + soundPull(clip)`,
 * the exact inverse of `drawnSpan`'s sound branch.
 */
import { clipEnd, isMediaClip, type AnyClip, type EDL } from '../types'
import {
  isSoundLane, layoutTime, renderSpanOf, soundPull, v1LayoutOf, v1SeamsOf, v1TimeFromOutput,
} from './timelineLayout'

export interface SplitTarget {
  track: string
  /** LAYOUT time — what `split_at` takes. */
  time: number
}

/**
 * The layout time `split_at(track, …)` needs for the playhead on `trackId`.
 * On a sound lane pass the `clip` being cut: its time is its own pull, not
 * the overlay inverse (without one — a paste, a drop — the overlay inverse
 * still places a NEW clip where the playhead is).
 */
export function splitTimeFor(
  edl: EDL | null | undefined, trackId: string, playhead: number, clip?: AnyClip | null,
): number {
  if (trackId === 'v1') return v1TimeFromOutput(edl, playhead)
  const seams = v1SeamsOf(edl)
  const track = edl?.tracks.find((t) => t.id === trackId)
  if (clip && isSoundLane(trackId, track?.type)) {
    return playhead + soundPull(seams, clip, track?.clips, v1LayoutOf(edl).end)
  }
  return layoutTime(seams, playhead)
}

/** The media clip on SOUND lane `trackId` audible at render instant
 *  `playhead` (where the Timeline draws it); undefined off a sound lane. */
export function soundClipUnder(
  edl: EDL | null | undefined, trackId: string, playhead: number,
): AnyClip | undefined {
  const track = edl?.tracks.find((t) => t.id === trackId)
  if (!track || !isSoundLane(trackId, track.type)) return undefined
  return track.clips.find((c) => {
    if (!isMediaClip(c)) return false
    const sp = renderSpanOf(edl, trackId, c)
    return !sp.dropped && sp.start <= playhead && playhead < sp.end
  })
}

/**
 * One split per distinct track holding a SELECTED clip that contains the
 * playhead (tested in that track's own layout time); v1 when none does —
 * the historical default.
 */
export function splitTargets(
  edl: EDL | null | undefined, selected: ReadonlySet<string>, playhead: number,
): SplitTarget[] {
  const out: SplitTarget[] = []
  if (edl && selected.size) {
    for (const tk of edl.tracks) {
      if (out.some((o) => o.track === tk.id)) continue
      for (const c of tk.clips) {
        if (!selected.has(c.id)) continue
        // per clip: a sound clip decodes through its own run's pull
        const time = splitTimeFor(edl, tk.id, playhead, c)
        if (c.start <= time && time < clipEnd(c)) { out.push({ track: tk.id, time }); break }
      }
    }
  }
  if (!out.length) out.push({ track: 'v1', time: splitTimeFor(edl, 'v1', playhead) })
  return out
}
