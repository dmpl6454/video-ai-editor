// Which timeline lanes are drawn, in what order, and what each one is called
// (QA-014).
//
// Every lane of the EDL used to draw whether or not it held anything: eleven
// 40 px rows (468 px) in a 247 px pane, so a new text or sticker landed on a
// row below the fold, the Hook row was cut in half even at 1920, silent lanes
// carried mute boxes and "captions" was the only lowercase name.
//
// Now: a lane draws when it holds a clip, plus the main video lane always.
// Overlay lanes (PIP video, text, stickers, captions) sit ABOVE the main track
// with the highest-z lane on top — the NLE convention, and where a new overlay
// appears — and the audio-family lanes sit below it. While something is being
// dragged, one extra "new track" row at the bottom accepts it and resolves to
// the first empty lane that can hold it (`ghostTarget`), so an empty lane is
// still reachable without being drawn all the time.

import { baseName } from './paths'
import { prettyDiskName } from './mediaNames'
import { isMediaClip, isTextClip, type AnyClip, type Track } from '../types'

export const GHOST_LANE_ID = '__new_track__'

const DRAWN_TYPES = new Set(['video', 'audio', 'music', 'vo', 'text', 'sticker', 'captions'])
const AUDIO_TYPES = new Set(['audio', 'music', 'vo'])
const SOUND_TYPES = new Set(['video', 'audio', 'music', 'vo'])

const DISPLAY_BY_TYPE: Record<string, string> = {
  video: 'Video', audio: 'Audio', music: 'Music', vo: 'Voiceover',
  text: 'Text', sticker: 'Stickers', captions: 'Captions', effect: 'Effects',
}

/** The pseudo-track drawn as the "drop here for a new track" row. */
export function ghostLane(): Track {
  return { id: GHOST_LANE_ID, type: 'ghost', z: 0, label: 'New track', clips: [] }
}

export function isGhostLane(t: Track | undefined | null): boolean {
  return !!t && t.id === GHOST_LANE_ID
}

/** A lane's name as shown in the label column — never a bare lowercase id. */
export function laneName(t: Track): string {
  const raw = (t.label ?? '').trim()
  if (raw) return raw.charAt(0).toUpperCase() + raw.slice(1)
  return DISPLAY_BY_TYPE[t.type] ?? (t.id.charAt(0).toUpperCase() + t.id.slice(1))
}

/** Whether a lane carries sound, i.e. whether its mute toggle means anything. */
export function laneHasSound(t: Track): boolean {
  return SOUND_TYPES.has(t.type)
}

/**
 * The rows to draw, top to bottom: overlay lanes (highest z first), the main
 * video lane, the audio-family lanes, then — only while `withGhost` — the
 * new-track row. A lane is drawn when it holds a clip; `v1` always is.
 */
export function laneRows(tracks: readonly Track[], withGhost = false): Track[] {
  const drawn = tracks.filter((t) => DRAWN_TYPES.has(t.type) && (t.clips.length > 0 || t.id === 'v1'))
  const idx = new Map(tracks.map((t, i) => [t.id, i]))
  const byZDesc = (a: Track, b: Track) =>
    (b.z ?? 0) - (a.z ?? 0) || (idx.get(b.id)! - idx.get(a.id)!)
  const overlays = drawn.filter((t) => t.id !== 'v1' && !AUDIO_TYPES.has(t.type)).sort(byZDesc)
  const main = drawn.filter((t) => t.id === 'v1')
  const audio = drawn.filter((t) => AUDIO_TYPES.has(t.type))
  const rows = [...overlays, ...main, ...audio]
  return withGhost ? [...rows, ghostLane()] : rows
}

/** What is being dropped on the new-track row. */
export type GhostKind = 'video' | 'audio' | 'text' | 'sticker'

/**
 * The real lane a drop on the new-track row lands on: the first EMPTY lane of
 * the right family, in EDL order (video → the first empty non-main video lane,
 * i.e. PIP; audio → Music, then Main audio, then Voiceover; an overlay → an
 * empty lane of its own type). Null when no such lane exists — the caller
 * then says so instead of guessing.
 */
export function ghostTarget(tracks: readonly Track[], kind: GhostKind, originId?: string): Track | null {
  const empty = (t: Track) => t.clips.length === 0 && t.id !== originId
  if (kind === 'video') return tracks.find((t) => t.type === 'video' && t.id !== 'v1' && empty(t)) ?? null
  if (kind === 'audio') {
    for (const type of ['music', 'audio', 'vo']) {
      const hit = tracks.find((t) => t.type === type && empty(t))
      if (hit) return hit
    }
    return null
  }
  return tracks.find((t) => t.type === kind && empty(t)) ?? null
}

/** The text drawn inside a clip on the timeline. A sticker used to get none.
 *  `names` is the media library's src → the user's file name (QA-045); a
 *  media clip used to be labelled with its sanitised disk name. */
export function clipLabel(c: AnyClip, names?: ReadonlyMap<string, string>): string {
  if (isMediaClip(c)) return names?.get(c.src) ?? prettyDiskName(baseName(c.src))
  if (isTextClip(c)) return c.text.trim() || '(empty)'
  const st = c as unknown as { label?: string | null; src?: string }
  if (st.label && st.label.trim()) return st.label.trim()
  return st.src ? baseName(st.src) : 'Sticker'
}
