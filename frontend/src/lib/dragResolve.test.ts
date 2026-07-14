import { describe, it, expect } from 'vitest'
import { rangesOverlap, snapToFreeGap, wouldOverlap } from './dragResolve'
import type { Track } from '../types'

function mediaTrack(clips: { id: string; start: number; dur: number }[]): Track {
  return {
    id: 'v1', type: 'video', z: 0,
    clips: clips.map((c) => ({ id: c.id, src: 'x.mp4', in: 0, out: c.dur, start: c.start })),
  }
}

describe('rangesOverlap', () => {
  it('detects a true overlap', () => {
    expect(rangesOverlap(0, 5, 3, 8)).toBe(true)
  })
  it('treats exact edge-abutment as non-overlapping', () => {
    expect(rangesOverlap(0, 5, 5, 10)).toBe(false)
  })
  it('returns false for disjoint ranges', () => {
    expect(rangesOverlap(0, 2, 4, 6)).toBe(false)
  })
})

describe('wouldOverlap', () => {
  const track = mediaTrack([{ id: 'a', start: 0, dur: 5 }, { id: 'b', start: 10, dur: 5 }])
  it('is true when the placement lands inside an existing clip', () => {
    expect(wouldOverlap(track, 3, 2, 'ignore')).toBe(true)
  })
  it('is false in a free gap', () => {
    expect(wouldOverlap(track, 3, 6, 'ignore')).toBe(false)
  })
  it('ignores the clip being moved', () => {
    expect(wouldOverlap(track, 5, 0, 'a')).toBe(false)
  })
})

describe('snapToFreeGap', () => {
  const track = mediaTrack([{ id: 'a', start: 0, dur: 5 }, { id: 'b', start: 10, dur: 5 }])
  it('returns the preferred start when the slot is free', () => {
    expect(snapToFreeGap(track, 3, 6, 'ignore')).toBe(6)
  })
  it('snaps forward past a collided clip to its end', () => {
    expect(snapToFreeGap(track, 3, 2, 'ignore')).toBe(5)
  })
  it('clamps a negative preferred start to 0', () => {
    expect(snapToFreeGap(mediaTrack([]), 3, -4, 'ignore')).toBe(0)
  })
  it('excludes the moving clip from occupancy', () => {
    // Dropping clip "a" onto its own original slot must not snap.
    expect(snapToFreeGap(track, 5, 0, 'a')).toBe(0)
  })
  it('snaps forward past two contiguous occupied clips in one call', () => {
    const contiguous = mediaTrack([
      { id: 'a', start: 0, dur: 5 }, { id: 'b', start: 5, dur: 5 }, { id: 'c', start: 10, dur: 5 },
    ])
    expect(snapToFreeGap(contiguous, 3, 1, 'ignore')).toBe(15)
  })
  it('duration=0 still snaps past a clip it would land inside', () => {
    const track = mediaTrack([{ id: 'a', start: 0, dur: 5 }])
    expect(snapToFreeGap(track, 0, 2, 'ignore')).toBe(5)
  })
})
