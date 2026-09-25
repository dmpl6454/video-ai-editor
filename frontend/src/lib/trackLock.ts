// Track lock, as the UI reads it (QA-023). The backend refuses every mutation
// on a locked lane (dispatch._call_guarded); these helpers let the UI say so
// BEFORE a gesture instead of after a round-trip. `locked` is not on types.ts's
// hand-mirrored Track, so it is read through the repo's cast pattern.
import type { EDL, Track } from '../types'

export function isTrackLocked(t: Track | null | undefined): boolean {
  return !!(t as unknown as { locked?: boolean } | null | undefined)?.locked
}

/** The locked lane holding `clipId`, or null when the clip is editable (or gone). */
export function lockedTrackOf(edl: EDL | null | undefined, clipId: string | null | undefined): Track | null {
  if (!edl || !clipId) return null
  const t = edl.tracks.find((tr) => tr.clips.some((c) => c.id === clipId))
  return t && isTrackLocked(t) ? t : null
}

export function lockedNotice(t: Track): string {
  return `Track "${t.label ?? t.id}" is locked — unlock it (right-click the track) to edit this clip.`
}
