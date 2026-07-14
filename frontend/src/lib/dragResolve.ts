// Pure drag→outcome math for the timeline. NO React, NO canvas — everything
// here is unit-tested (dragResolve.test.ts). The snap/overlap functions are
// the single source of truth used BOTH by the live drag preview and by the
// release handler, so what you see while dragging equals where it lands.
//
// The snap algorithm is identical to Timeline's existing `firstFreeGap` and
// dispatch.py's `_first_free_gap` (same 1e-9 epsilon), consolidated here.

import type { AnyClip, Track } from '../types'
import { isMediaClip } from '../types'

const EPS = 1e-9

export function rangesOverlap(
  aStart: number, aEnd: number, bStart: number, bEnd: number,
): boolean {
  return aStart < bEnd - EPS && aEnd > bStart + EPS
}

function occupiedRanges(track: Track, ignoreClipId: string): [number, number][] {
  return track.clips
    .filter(isMediaClip)
    .filter((c) => c.id !== ignoreClipId)
    .map((c): [number, number] => [c.start, c.start + (c.out - c.in)])
    .sort((a, b) => a[0] - b[0])
}

export function wouldOverlap(
  track: Track, duration: number, start: number, ignoreClipId: string,
): boolean {
  const end = start + duration
  return occupiedRanges(track, ignoreClipId).some(
    ([oStart, oEnd]) => rangesOverlap(start, end, oStart, oEnd),
  )
}

export function snapToFreeGap(
  track: Track, duration: number, preferredStart: number, ignoreClipId: string,
): number {
  const occupied = occupiedRanges(track, ignoreClipId)
  let candidate = Math.max(0, preferredStart)
  const overlaps = (start: number) => {
    const end = start + duration
    return occupied.some(([oStart, oEnd]) => rangesOverlap(start, end, oStart, oEnd))
  }
  if (!overlaps(candidate)) return candidate
  for (const [oStart, oEnd] of occupied) {
    if (candidate < oEnd && candidate + duration > oStart) candidate = oEnd
  }
  for (let i = 0; i < occupied.length; i++) {
    if (!overlaps(candidate)) break
    for (const [oStart, oEnd] of occupied) {
      if (candidate < oEnd && candidate + duration > oStart) candidate = oEnd
    }
  }
  return candidate
}

// eslint keeps AnyClip imported for the resolve* functions added in Task 4.
export type { AnyClip }
