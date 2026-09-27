// Keyboard clip selection (review RD3): a keyboard user could select only
// every clip (⌘A) or the right half a split left selected — never "the first
// clip" to give it a speed, a curve or a text. These answer "the clip under
// the playhead" and "the next / previous clip on this lane", on RENDER time
// (where each clip plays: `renderSpanOf`, the rule the Timeline draws with),
// so a crossfade window answers the clip fading in, as v1ClipAt does.
import type { AnyClip, EDL, Track } from '../types'
import { renderSpanOf } from './timelineLayout'

export interface ClipPick {
  id: string
  track: string
  /** Render time the clip starts playing (where the playhead goes). */
  start: number
  /** What the clip is called out loud: its media's file name or its text. */
  name: string
}

const EPS = 1e-6

function nameOf(c: AnyClip): string {
  if ('text' in c) return `text "${c.text.slice(0, 40)}"`
  const base = c.src.split(/[\\/]/).pop() ?? c.src
  return base.replace(/\.normalized(?=\.[^.]+$)/, '')
}

function tracksInOrder(edl: EDL, prefer?: string | null): Track[] {
  const byId = (id: string) => edl.tracks.find((t) => t.id === id)
  const first = [prefer ? byId(prefer) : undefined, byId('v1')].filter((t): t is Track => !!t)
  return [...new Set([...first, ...edl.tracks])]
}

function spans(edl: EDL, t: Track): Array<{ clip: AnyClip; start: number; end: number }> {
  return t.clips.map((clip) => ({ clip, ...renderSpanOf(edl, t.id, clip) }))
    .filter((s) => !s.dropped && s.end > s.start)
    .sort((a, b) => a.start - b.start)
}

/** The clip playing at the RENDER instant `playhead`: on `prefer`'s lane
 *  (the selected clip's) first, then the main track, then the other lanes
 *  top to bottom. The last match on a lane wins (a crossfade: the clip
 *  fading in). */
export function clipAtPlayhead(edl: EDL | null | undefined, playhead: number, prefer?: string | null): ClipPick | null {
  if (!edl) return null
  for (const t of tracksInOrder(edl, prefer)) {
    let hit: { clip: AnyClip; start: number } | null = null
    for (const s of spans(edl, t)) if (s.start <= playhead + EPS && playhead < s.end - EPS) hit = s
    if (hit) return { id: hit.clip.id, track: t.id, start: hit.start, name: nameOf(hit.clip) }
  }
  return null
}

/** The next (`dir` 1) or previous (−1) clip on the lane of `currentId` (the
 *  main track when nothing is selected), by where it plays. With nothing
 *  selected it is the next clip starting after the playhead / the last one
 *  starting before it. */
export function adjacentClip(
  edl: EDL | null | undefined, currentId: string | null, playhead: number, dir: 1 | -1,
): ClipPick | null {
  if (!edl) return null
  const track = (currentId && edl.tracks.find((t) => t.clips.some((c) => c.id === currentId))) || edl.tracks.find((t) => t.id === 'v1')
  if (!track) return null
  const list = spans(edl, track)
  let i: number
  const at = currentId ? list.findIndex((s) => s.clip.id === currentId) : -1
  if (at >= 0) i = at + dir
  else if (dir > 0) i = list.findIndex((s) => s.start > playhead + EPS)
  else i = list.map((s) => s.start < playhead - EPS).lastIndexOf(true)
  const s = i >= 0 && i < list.length ? list[i] : null
  return s ? { id: s.clip.id, track: track.id, start: s.start, name: nameOf(s.clip) } : null
}
