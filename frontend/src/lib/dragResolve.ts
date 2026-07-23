// Pure drag→outcome math for the timeline. NO React, NO canvas — everything
// here is unit-tested (dragResolve.test.ts). The snap/overlap functions are
// the single source of truth used BOTH by the live drag preview and by the
// release handler, so what you see while dragging equals where it lands.
//
// The snap algorithm is identical to Timeline's existing `firstFreeGap` and
// dispatch.py's `_first_free_gap` (same 1e-9 epsilon), consolidated here.

import type { Track } from '../types'
import { isMediaClip, clipEnd } from '../types'

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
    // clipEnd = start + (out-in)/speed — the clip's TIMELINE footprint, matching
    // the draw loop and dispatch.py's _first_free_gap. Raw `out-in` made a 2x
    // clip occupy twice its drawn width here, so drops snapped past phantom space.
    .map((c): [number, number] => [c.start, clipEnd(c)])
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

const MIN_SPAN = 0.1
const SPEED_MIN = 0.25
const SPEED_MAX = 4

export function resolveMediaTrim(
  clip: { in: number; out: number }, side: 'l' | 'r', deltaSec: number,
  speed: number = 1,
): { in: number; out: number } {
  // `deltaSec` is a TIMELINE-space delta (pixels/zoom at the drag site), but
  // in/out are SOURCE-space. A clip at speed s covers (out-in)/s timeline
  // seconds, so 1 timeline second of edge movement consumes s source seconds.
  // Without this conversion a 0.5x clip dragged +2s grew its source span by
  // only 2s — i.e. its timeline footprint grew 4s, double the drag.
  const srcDelta = deltaSec * (speed > 0 ? speed : 1)
  if (side === 'l') {
    const newIn = Math.min(Math.max(0, clip.in + srcDelta), clip.out - MIN_SPAN)
    return { in: newIn, out: clip.out }
  }
  const newOut = Math.max(clip.out + srcDelta, clip.in + MIN_SPAN)
  return { in: clip.in, out: newOut }
}

export function resolveMediaSpeed(
  sourceDur: number, currentSpeed: number, side: 'l' | 'r', deltaSec: number,
): number {
  const currentFootprint = sourceDur / (currentSpeed || 1)
  // Dragging either edge outward lengthens the footprint; inward shortens it.
  // deltaSec is signed in timeline space: +delta on the right edge or -delta on
  // the left edge both lengthen. We pass the already-signed footprint delta.
  const footprintDelta = side === 'r' ? deltaSec : -deltaSec
  const newFootprint = Math.max(MIN_SPAN, currentFootprint + footprintDelta)
  const factor = sourceDur / newFootprint
  return Math.min(SPEED_MAX, Math.max(SPEED_MIN, factor))
}

export function resolveOverlayTiming(
  clip: { start: number; end: number }, side: 'l' | 'r', deltaSec: number,
): { start: number; end: number } {
  if (side === 'l') {
    const newStart = Math.min(Math.max(0, clip.start + deltaSec), clip.end - MIN_SPAN)
    return { start: newStart, end: clip.end }
  }
  const newEnd = Math.max(clip.end + deltaSec, clip.start + MIN_SPAN)
  return { start: clip.start, end: newEnd }
}
