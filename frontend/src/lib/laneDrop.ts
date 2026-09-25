// An OS file dropped on a timeline lane lands on THAT lane at THAT time
// (QA-093).
//
// The canvas drop handler read only the in-app drag types and ignored
// `dataTransfer.files`, so the window-level importer took every Finder drop
// and appended it to the end of Main video — a file aimed at PIP at 8 s went
// to V1 at 40 s. The timeline now claims a file drop that lands on a lane
// (lib/fileDrop `claimFileDrop`, so the window importer stands down), and
// queues each file on the store's ONE import queue with a placement: the
// queue imports it without the default placement and adds it where it was
// dropped, from what the server ANSWERED (a photo is a 5 s clip, an audio-only
// .mp4 goes to an audio lane) — one pipeline with the Media panel's imports.

import { isAudioFile, type FileLike } from './fileDrop'
import { toFrameGrid } from './frameStep'

const VIDEO_LANES = new Set(['video'])
const MEDIA_LANES = new Set(['video', 'audio', 'music', 'vo'])

export interface LaneTarget { id: string; type: string }

export interface Placement {
  /** The lane under the pointer (already resolved from the new-track row). */
  lane: LaneTarget | null
  /** Where the first file starts, in EDL (layout) seconds, on the frame grid. */
  start: number
  /** The lanes that exist, for the audio fallback. */
  lanes: readonly LaneTarget[]
  /** Main video has no clip yet — the first import sets the canvas and rate. */
  mainEmpty: boolean
}

/** Where one queued import lands (store `ImportOptions.place`). `cursor` is
 *  shared by every file of one drop: each placed file moves it to its own
 *  end, so a multi-file drop lands end to end in drop order. */
export interface DropCursor { next: number }
export interface LanePlacement {
  track: string
  trackType: string
  cursor: DropCursor
  /** The lane an audio answer goes to when the target is a video lane. */
  audioLane: string | null
  fps: number
}

/** What POST /upload or /audio_upload answered (store `ImportAnswer`). */
export interface PlacedAnswer { kind?: string; src?: string; normalized?: string; duration?: number }

/** Mirror of ingest/still.py STILL_DEFAULT_SECONDS: a photo's default length.
 *  Its SOURCE is a long still stitch (300 s), so `duration` is not it. */
export const STILL_DEFAULT_SECONDS = 5

/**
 * The `add_clip` a lane drop means, from the import's ANSWER — never from the
 * file's extension alone: a photo answers kind "image" (a 5 s clip, not its
 * 300 s source), an audio-only .mp4 answers kind "audio" with `src` (no
 * `normalized`) and belongs on an audio lane. Advances the drop's cursor.
 */
export function placementFor(answer: PlacedAnswer | null, sentAs: 'video' | 'audio', place: LanePlacement)
  : { args: { track: string; src: string; in: number; out: number; start: number } | null; notice?: string } {
  if (!answer) return { args: null }
  const isAudio = sentAs === 'audio' || answer.kind === 'audio'
  const src = isAudio ? answer.src : answer.normalized
  if (!src) return { args: null, notice: 'The file was imported but could not be placed on that lane.' }
  let track = place.track
  let notice: string | undefined
  if (isAudio && VIDEO_LANES.has(place.trackType)) {
    track = place.audioLane ?? 'music'
    notice = sentAs === 'video' ? 'That file has no picture — added to the Music lane instead.' : undefined
  }
  const out = answer.kind === 'image'
    ? toFrameGrid(STILL_DEFAULT_SECONDS, place.fps)
    : Math.max(0, Number(answer.duration) || 0)
  const start = place.cursor.next
  place.cursor.next = toFrameGrid(start + out, place.fps)
  return { args: { track, src, in: 0, out, start }, notice }
}

export interface LaneDropDeps<F extends FileLike> {
  /** Put the file on the store's ONE import queue (placeholder row, Cancel,
   *  the panel's busy state) — placed on `place`, or the default import when
   *  it is null. Resolves when that file is done. */
  enqueue(file: F, as: 'video' | 'audio', place: LanePlacement | null): Promise<void>
  notice(message: string): void
}

/** The lane one file lands on, or null for "use the default import". */
export function laneFor(file: FileLike, p: Placement): LaneTarget | null {
  const audio = isAudioFile(file)
  const lane = p.lane
  if (lane && MEDIA_LANES.has(lane.type) && !(audio && VIDEO_LANES.has(lane.type))) {
    // The very first video of a project goes through the default import: it
    // is what matches the canvas and timebase to the footage.
    if (!audio && lane.id === 'v1' && p.mainEmpty) return null
    return lane
  }
  if (audio) return p.lanes.find((t) => t.type === 'music') ?? null
  return null
}

/**
 * Queue `files` for import onto the dropped-on lane, end to end from the drop
 * time (the queue runs them in drop order). Returns how many were aimed at a
 * lane (the rest take the default import). Resolves when all are done.
 */
export async function dropFilesOnLane<F extends FileLike>(
  files: ArrayLike<F> | Iterable<F>, p: Placement, deps: LaneDropDeps<F>, fps: number,
): Promise<number> {
  const cursor: DropCursor = { next: p.start }
  const audioLane = p.lanes.find((t) => t.type === 'music')?.id ?? null
  const runs: Promise<void>[] = []
  let placed = 0
  for (const f of Array.from(files as ArrayLike<F>)) {
    const as = isAudioFile(f) ? 'audio' : 'video'
    const lane = laneFor(f, p)
    if (!lane) {
      runs.push(deps.enqueue(f, as, null))
      continue
    }
    if (p.lane && lane.id !== p.lane.id) {
      deps.notice(`“${f.name}” is audio — added to the ${lane.id === 'music' ? 'Music' : lane.id} lane instead.`)
    }
    runs.push(deps.enqueue(f, as, { track: lane.id, trackType: lane.type, cursor, audioLane, fps }))
    placed++
  }
  await Promise.all(runs)
  return placed
}
