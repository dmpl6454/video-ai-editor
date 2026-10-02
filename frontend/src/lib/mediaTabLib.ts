// The Media tab's data: the project's library (GET /media, merged with the
// live timeline through lib/mediaLibrary) and "append this to the end of its
// lane" (design §2a: the card's ＋). Here, not in the component file, so the
// component module exports only components (react-refresh).
import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { useStore } from '../store'
import type { MediaItem } from '../types'
import { clipEnd, isMediaClip } from '../types'
import { binRows, type BinRow } from './mediaLibrary'
import { useMediaNames } from './mediaNames'
import { videoContentEnd } from './timelineExtent'
import { toFrameGrid } from './frameStep'
import { selectNewClip } from './newClip'

const STILL_SECONDS = 5

export function useMediaLibrary(): { rows: BinRow[]; reload: () => void } {
  const sid = useStore((s) => s.sessionId)
  const edl = useStore((s) => s.edl)
  const uploading = useStore((s) => s.uploading)
  const [library, setLibrary] = useState<{ sid: string; items: MediaItem[] } | null>(null)
  const [tick, setTick] = useState(0)
  useEffect(() => {
    if (!sid) return
    let live = true
    api.listMedia(sid).then((r) => {
      if (!live) return
      setLibrary({ sid, items: r.media })
      useMediaNames.getState().publish(sid, r.media)
    }).catch((e) => console.warn('[mediaTabLib] media library fetch failed:', e))
    return () => { live = false }
  }, [sid, edl, uploading, tick])
  const rows = useMemo(() => binRows(library && library.sid === sid ? library.items : null, edl), [library, sid, edl])
  return { rows, reload: () => setTick((n) => n + 1) }
}

/** Append `row` to the end of its lane (design: "Plus → appended to end of
 *  matching track"), then select the new clip. */
export async function appendToTimeline(row: BinRow): Promise<void> {
  const st = useStore.getState()
  const edl = st.edl
  if (!edl || row.missing) return
  const audio = row.kind === 'audio'
  const track = audio ? (edl.tracks.find((t) => t.type === 'music')?.id ?? 'music') : 'v1'
  const laneEnd = audio
    ? (edl.tracks.find((t) => t.id === track)?.clips.filter(isMediaClip).reduce((m, c) => Math.max(m, clipEnd(c)), 0) ?? 0)
    : videoContentEnd(edl)
  const dur = row.still ? STILL_SECONDS : (row.duration && row.duration > 0 ? row.duration : STILL_SECONDS)
  const r = await st.dispatch('add_clip', { track, src: row.src, in: 0, out: dur, start: toFrameGrid(laneEnd, edl.canvas.fps) })
  if (r) await selectNewClip(r.result)
}

