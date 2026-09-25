// What the user calls each piece of media, and which of it is offline — shared
// by every surface that names a clip (QA-045 / QA-095).
//
// The bin showed the project's real file names once the media library landed
// (QA-010), but everything else still printed the sanitised DISK name:
// timeline clip labels, the inspector header, the music panel and the AI tool
// clip pickers read `baseName(clip.src)`, so "पहला वीडियो".mp4 was labelled
// `upload_6f3d3a.normalized.mp4`, and after an AI step `denoise_2da3c30f.mp4`.
// The library (GET /sessions/:id/media) already knows the real name of every
// src the timeline uses — including derived renders ("take.mp4 (denoised)")
// and missing files — so it is kept here, in one small store, and every label
// asks it first.
//
// MediaBin fetches the library (on every EDL change) and publishes it here;
// nothing else fetches, so there is still exactly one request per refresh.

import { create } from 'zustand'
import type { MediaItem } from '../types'
import { baseName } from './paths'

interface MediaNamesState {
  sid: string | null
  items: readonly MediaItem[]
  publish(sid: string, items: readonly MediaItem[]): void
}

export const useMediaNames = create<MediaNamesState>((set) => ({
  sid: null,
  items: [],
  publish: (sid, items) => set({ sid, items }),
}))

const EMPTY: readonly MediaItem[] = []

/** The published library for `sid`, or nothing when it belongs to another project. */
export function itemsFor(state: Pick<MediaNamesState, 'sid' | 'items'>, sid: string | null): readonly MediaItem[] {
  return sid && state.sid === sid ? state.items : EMPTY
}

/** src → the user's name for it. */
export function namesBySrc(items: readonly MediaItem[]): Map<string, string> {
  const out = new Map<string, string>()
  for (const it of items) if (it.name) out.set(it.src, it.name)
  return out
}

/** The srcs whose file is missing on disk. */
export function offlineSrcs(items: readonly MediaItem[]): Set<string> {
  return new Set(items.filter((it) => it.missing).map((it) => it.src))
}

/** The name to show for `src`: the library's, else the file name (never a path). */
export function displayNameFor(src: string, names: ReadonlyMap<string, string>): string {
  return names.get(src) ?? prettyDiskName(baseName(src))
}

/** `take_1a2b3c4d.normalized.mp4` → `take.mp4` — the upload's own suffixes
 *  removed, for a src the library has not listed yet (first paint). */
export function prettyDiskName(name: string): string {
  return name
    .replace(/\.normalized\.mp4$/i, '.mp4')
    .replace(/_[0-9a-f]{8}(\.[^.]+)$/i, '$1')
}

/** A file name cut into the pieces a line may break BETWEEN (wave-B review):
 *  after `_`, `-`, a space or a dot inside the stem — never inside the
 *  extension, which stays glued to the last piece. `overflow-wrap: anywhere`
 *  split short names mid-extension ("scene_16x9.mp / 4"). */
export function nameBreaks(name: string): string[] {
  const m = /^(.*?)(\.[A-Za-z0-9]{1,5})?$/.exec(name) ?? [name, name, '']
  const stem = m[1] ?? name
  const ext = m[2] ?? ''
  const parts = stem.split(/(?<=[_\-. ])/).filter((p) => p.length > 0)
  if (!parts.length) return [name]
  parts[parts.length - 1] += ext
  return parts
}
