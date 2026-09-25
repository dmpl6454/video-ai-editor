// The Media panel's rows: the project's library merged with the live timeline.
//
// The bin used to be `edl.tracks → clips → unique src`, so it could only show
// media the timeline was using: delete the last clip of a take and the take
// was gone from the bin — also after a reload — although the file was still
// on disk (QA-010). The library now comes from GET /sessions/:id/media
// (media_library.py), which lists every import whether or not it is used.
//
// This merge keeps two promises the list alone cannot:
//   * used-counts follow the timeline the instant the EDL refreshes, without
//     waiting for the next library fetch — counted from the live EDL, with the
//     server's clip ids as a fallback for a src spelled differently (a symlinked
//     workdir resolves to another string);
//   * the bin never shows LESS than the timeline uses: a src on the timeline
//     that the library has not listed yet (first paint, a failed fetch) gets a
//     row of its own, exactly as the old timeline-derived bin did.

import type { EDL, MediaItem } from '../types'
import { isMediaClip } from '../types'
import { baseName, isAudioPath } from './paths'
import { prettyDiskName } from './mediaNames'

const MEDIA_LANES = new Set(['video', 'audio', 'music', 'vo'])
const AUDIO_LANES = new Set(['audio', 'music', 'vo'])

export interface BinRow {
  /** Library id; null for a timeline-only row the library has not listed. */
  id: string | null
  src: string
  name: string
  kind: 'video' | 'audio'
  duration: number | null
  width: number | null
  height: number | null
  uses: number
  clipIds: string[]
  /** QA-095: the file is gone from disk — offline, with a Relink action. */
  missing: boolean
  /** QA-090: a photo. */
  still: boolean
}

/** src → ids of the timeline clips on media lanes that reference it. */
export function timelineUses(edl: EDL | null): Map<string, string[]> {
  const out = new Map<string, string[]>()
  for (const t of edl?.tracks ?? []) {
    if (!MEDIA_LANES.has(t.type)) continue
    for (const c of t.clips) {
      if (!isMediaClip(c)) continue
      out.set(c.src, [...(out.get(c.src) ?? []), c.id])
    }
  }
  return out
}

export function binRows(items: readonly MediaItem[] | null, edl: EDL | null): BinRow[] {
  const uses = timelineUses(edl)
  const live = new Set([...uses.values()].flat())
  const covered = new Set<string>()
  const rows: BinRow[] = (items ?? []).map((it) => {
    const ids = new Set<string>(uses.get(it.src) ?? [])
    for (const id of it.clip_ids) if (live.has(id)) ids.add(id)
    ids.forEach((id) => covered.add(id))
    return { id: it.id, src: it.src, name: it.name, kind: it.kind, duration: it.duration,
             width: it.width, height: it.height, uses: ids.size, clipIds: [...ids],
             missing: !!it.missing, still: !!it.still }
  })
  // A src on an audio lane is audio whatever its extension (an audio-only
  // .mp4 on the Music lane, QA-092).
  const onAudioLane = new Set((edl?.tracks ?? []).filter((t) => AUDIO_LANES.has(t.type))
    .flatMap((t) => t.clips.filter(isMediaClip).map((c) => c.src)))
  for (const [src, ids] of uses) {
    if (ids.every((id) => covered.has(id))) continue
    rows.push({ id: null, src, name: prettyDiskName(baseName(src)),
                kind: isAudioPath(src) || onAudioLane.has(src) ? 'audio' : 'video',
                duration: null, width: null, height: null, uses: ids.length, clipIds: ids,
                missing: false, still: false })
  }
  return rows
}

/** 4.017 → "0:04", 3725 → "1:02:05". */
export function clockDuration(sec: number | null | undefined): string {
  if (sec === null || sec === undefined || !Number.isFinite(sec) || sec < 0) return ''
  const s = Math.round(sec)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const r = String(s % 60).padStart(2, '0')
  return h ? `${h}:${String(m).padStart(2, '0')}:${r}` : `${m}:${r}`
}

/** The row's second line: length, size, and whether the timeline uses it. */
export function binMeta(row: BinRow): string {
  if (row.missing) {
    // QA-095: the one thing that matters about an offline item.
    return `Offline — file missing${row.uses ? ` · used ×${row.uses}` : ''}`
  }
  const parts = [row.still ? 'photo' : clockDuration(row.duration)]
  if (row.kind === 'video' && row.width && row.height) parts.push(`${row.width}×${row.height}`)
  if (row.kind === 'audio') parts.push('audio')
  parts.push(row.uses ? `used ×${row.uses}` : 'not on timeline')
  return parts.filter(Boolean).join(' · ')
}
