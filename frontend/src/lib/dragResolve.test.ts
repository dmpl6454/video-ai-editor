import { describe, it, expect } from 'vitest'
import { rangesOverlap, reorderLanding, resolveMediaSpeed, resolveMediaTrim, resolveOverlayTiming, snapToFreeGap, wouldOverlap } from './dragResolve'
import type { Track } from '../types'

function mediaTrack(clips: { id: string; start: number; dur: number; speed?: number }[]): Track {
  return {
    id: 'v1', type: 'video', z: 0,
    // `speed` isn't on the frontend Clip interface (hand-mirrored, incomplete
    // on purpose) — attach it the way the backend serializes it; clipSpeedFactor
    // reads it via a cast.
    clips: clips.map((c) => ({
      id: c.id, src: 'x.mp4', in: 0, out: c.dur, start: c.start,
      ...(c.speed != null ? { speed: c.speed } : {}),
    })) as Track['clips'],
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
  it('uses the EFFECTIVE (speed-adjusted) footprint of existing clips', () => {
    // 10s of source at 2x occupies only [0,5) on the timeline — placing at 6
    // must be free (raw out-in would say it occupies [0,10) and flag overlap).
    const retimed = mediaTrack([{ id: 'a', start: 0, dur: 10, speed: 2 }])
    expect(wouldOverlap(retimed, 3, 6, 'ignore')).toBe(false)
    expect(wouldOverlap(retimed, 3, 4, 'ignore')).toBe(true)
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
  it('snaps to the EFFECTIVE end of a retimed clip, not its source end', () => {
    // 10s source at 2x ends at timeline 5 — a collided drop snaps to 5, not 10.
    const retimed = mediaTrack([{ id: 'a', start: 0, dur: 10, speed: 2 }])
    expect(snapToFreeGap(retimed, 3, 2, 'ignore')).toBe(5)
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
  it('converts the timeline delta to source space via speed (0.5x)', () => {
    // 0.5x clip dragged +2 TIMELINE seconds on the right edge should consume
    // only 1 SOURCE second (2·0.5) → footprint grows exactly the dragged 2s
    // ((out-in)/speed: 6/0.5=12 → 7/0.5=14). Unscaled it grew 4s.
    expect(resolveMediaTrim(clip, 'r', 2, 0.5)).toEqual({ in: 2, out: 9 })
  })
  it('converts the timeline delta to source space via speed (2x, left edge)', () => {
    // 2x clip: +1 timeline second on the left edge advances `in` by 2 source s.
    expect(resolveMediaTrim(clip, 'l', 1, 2)).toEqual({ in: 4, out: 8 })
  })
  it('treats a missing/invalid speed as 1', () => {
    expect(resolveMediaTrim(clip, 'r', 2, 0)).toEqual({ in: 2, out: 10 })
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

describe('reorderLanding', () => {
  // Three back-to-back clips, the shape every reorder complaint was about.
  const seq = mediaTrack([
    { id: 'a', start: 0, dur: 2 },
    { id: 'b', start: 2, dur: 2 },
    { id: 'c', start: 4, dur: 2 },
  ])

  it('puts a clip dropped at the head FIRST', () => {
    expect(reorderLanding(seq, 0, 'c')).toBe(0)
    // What the old path answered for the same drag: 4 — bit for bit where `c`
    // already was. It walks forward past the very clips the drag is jumping,
    // so dragging the last clip to the front moved it exactly nowhere, and the
    // landing line drew that no-op as if it were the outcome.
    expect(snapToFreeGap(seq, 2, 0, 'c')).toBe(4)
  })

  it('orders by the dropped clip\'s LEFT EDGE against the other starts', () => {
    // Dropped at 2 — a tie with `b`, and a tie goes to the dragged clip, so it
    // lands second and pushes b right.
    expect(reorderLanding(seq, 2, 'c')).toBe(2)
    // Dropped at 2.9 its left edge is past b's, so it lands third — back where
    // it started. One rule, applied on both sides: the left edge decides.
    expect(reorderLanding(seq, 2.9, 'c')).toBe(4)
    expect(reorderLanding(seq, 1.9, 'c')).toBe(2)
  })

  it('sends a clip dropped past the end to the tail', () => {
    expect(reorderLanding(seq, 99, 'a')).toBe(4)
  })

  it('is a no-op when a clip is dropped back where it was', () => {
    expect(reorderLanding(seq, 2, 'b')).toBe(2)
  })

  it('closes the hole the clip leaves behind', () => {
    // `b` moved to the end: `c` slides down into b's old slot, so b lands at 4
    // rather than 6 — the timeline gets shorter, not longer.
    expect(reorderLanding(seq, 99, 'b')).toBe(4)
  })

  it('packs from 0 even when the lane starts with a gap', () => {
    const gapped = mediaTrack([{ id: 'a', start: 5, dur: 2 }, { id: 'b', start: 9, dur: 2 }])
    expect(reorderLanding(gapped, 20, 'a')).toBe(2)
    expect(reorderLanding(gapped, 0, 'b')).toBe(0)
  })

  it('uses the EFFECTIVE footprint of the clips it packs', () => {
    // 10s of source at 2x occupies 5s of timeline. Packing by raw out-in would
    // put the dropped clip 5s too far right of where the backend lands it.
    const retimed = mediaTrack([
      { id: 'a', start: 0, dur: 10, speed: 2 },
      { id: 'b', start: 5, dur: 3 },
    ])
    expect(reorderLanding(retimed, 99, 'b')).toBe(5)
  })
})
