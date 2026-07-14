import { describe, it, expect } from 'vitest'
import { rangesOverlap, resolveMediaSpeed, resolveMediaTrim, resolveOverlayTiming, snapToFreeGap, wouldOverlap } from './dragResolve'
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

describe('resolveMediaTrim', () => {
  const clip = { in: 2, out: 8 } // 6s source span
  it('right-edge drag extends out', () => {
    expect(resolveMediaTrim(clip, 'r', 2)).toEqual({ in: 2, out: 10 })
  })
  it('left-edge drag moves in', () => {
    expect(resolveMediaTrim(clip, 'l', 1)).toEqual({ in: 3, out: 8 })
  })
  it('clamps so out stays > in (min span 0.1)', () => {
    expect(resolveMediaTrim(clip, 'r', -100)).toEqual({ in: 2, out: 2.1 })
  })
  it('clamps in to >= 0', () => {
    expect(resolveMediaTrim(clip, 'l', -100)).toEqual({ in: 0, out: 8 })
  })
})

describe('resolveMediaSpeed', () => {
  // source span 6s at speed 1 → footprint 6s.
  it('right-edge drag OUT slows down (speed < 1)', () => {
    // new footprint 12s → factor 6/12 = 0.5
    expect(resolveMediaSpeed(6, 1, 'r', 6)).toBeCloseTo(0.5, 5)
  })
  it('right-edge drag IN speeds up (speed > 1)', () => {
    // new footprint 3s → factor 6/3 = 2
    expect(resolveMediaSpeed(6, 1, 'r', -3)).toBeCloseTo(2, 5)
  })
  it('clamps to a maximum of 4x', () => {
    expect(resolveMediaSpeed(6, 1, 'r', -5.9)).toBe(4)
  })
  it('clamps to a minimum of 0.25x', () => {
    expect(resolveMediaSpeed(6, 1, 'r', 100)).toBe(0.25)
  })
  it('accounts for an already-retimed clip footprint', () => {
    // source 6s at speed 2 → current footprint 3s; drag out +3 → 6s → factor 1
    expect(resolveMediaSpeed(6, 2, 'r', 3)).toBeCloseTo(1, 5)
  })
  it('left-edge drag mirrors the right-edge sign convention', () => {
    // Dragging the LEFT edge further left (deltaSec more negative) lengthens the
    // footprint exactly like dragging the right edge further right does — this is
    // the one sign-flip branch (`side === 'r' ? deltaSec : -deltaSec`) that none of
    // the above cases exercise, since they all drag the right edge.
    expect(resolveMediaSpeed(6, 1, 'l', -6)).toBeCloseTo(0.5, 5)
    expect(resolveMediaSpeed(6, 1, 'l', 3)).toBeCloseTo(2, 5)
  })
})

describe('resolveOverlayTiming', () => {
  const clip = { start: 5, end: 8 }
  it('right-edge drag extends end', () => {
    expect(resolveOverlayTiming(clip, 'r', 4)).toEqual({ start: 5, end: 12 })
  })
  it('left-edge drag moves start', () => {
    expect(resolveOverlayTiming(clip, 'l', -3)).toEqual({ start: 2, end: 8 })
  })
  it('clamps so end stays > start (min span 0.1)', () => {
    expect(resolveOverlayTiming(clip, 'r', -100)).toEqual({ start: 5, end: 5.1 })
  })
  it('clamps start to >= 0 and keeps end > start', () => {
    expect(resolveOverlayTiming(clip, 'l', -100)).toEqual({ start: 0, end: 8 })
  })
})
