// Final sweep 3, round 2 (render-audio): the Timeline draws a sound run where
// the export plays it (`schema.sound_render_windows`): an unlinked run never
// starts before the unlinked run before it ends, so two voiceover lines laid
// 0.2 s apart after a 0.5 s fade are drawn (and played) back to back, not
// 0.3 s over each other.
import { describe, expect, it } from 'vitest'
import { drawnSpan, soundPull, soundSpan, timingClockOf, v1LayoutOf } from './timelineLayout'
import { splitTimeFor } from './splitTargets'
import type { EDL } from '../types'

type AnyClipLike = EDL['tracks'][number]['clips'][number]

function edlWith(vo: Array<Record<string, unknown>>): EDL {
  return {
    version: 3, duration: 12, canvas: { w: 320, h: 180, fps: 30 },
    tracks: [
      { id: 'v1', type: 'video', z: 0, clips: [0, 1, 2].map((i) => (
        { id: `c${i}`, src: `c${i}.mp4`, in: 0, out: 4, start: 4 * i })),
      transitions: [{ at: 4, type: 'fade', duration: 0.5 }] },
      { id: 'vo', type: 'vo', z: 0, clips: vo },
    ],
  } as unknown as EDL
}

const line = (id: string, start: number, out: number, linked?: string) =>
  ({ id, src: `${id}.wav`, in: 0, out, start, ...(linked ? { linked_to: linked } : {}) })

describe('soundPull / soundSpan: a laid gap never becomes an overlap', () => {
  it('draws line 2 where line 1 stops', () => {
    const edl = edlWith([line('L1', 1, 3.8), line('L2', 5, 3)])
    const layout = v1LayoutOf(edl)
    const lane = edl.tracks[1].clips as AnyClipLike[]
    expect(soundPull(layout.seams, lane[1], lane, layout.end)).toBeCloseTo(0.2, 9)
    const s1 = drawnSpan('vo', lane[0], layout, 'vo', lane)
    const s2 = drawnSpan('vo', lane[1], layout, 'vo', lane)
    expect(s1.start + s1.duration).toBeCloseTo(4.8, 9)
    expect(s2.start).toBeCloseTo(4.8, 9)
    expect(s2.duration).toBeCloseTo(3, 9)
    // the Inspector reads the same clock, the split lands on the same sample
    expect(timingClockOf(edl, 'vo', lane[1]).show(5)).toBeCloseTo(4.8, 9)
    expect(splitTimeFor(edl, 'vo', 6.0, lane[1])).toBeCloseTo(6.2, 9)
  })

  it('keeps a detached sound under its own picture (J/L overlap)', () => {
    const edl = edlWith([line('L1', 1, 3.8, 'c0'), line('L2', 5, 3, 'c1')])
    const layout = v1LayoutOf(edl)
    const lane = edl.tracks[1].clips as AnyClipLike[]
    expect(soundSpan(layout.seams, layout.end, lane[1], lane).start).toBeCloseTo(4.5, 9)
  })

  it('clamps by where the programme end cut the run before', () => {
    const edl = edlWith([line('L1', 3, 10), line('L2', 13.2, 1)])
    const layout = v1LayoutOf(edl)
    const lane = edl.tracks[1].clips as AnyClipLike[]
    expect(soundSpan(layout.seams, layout.end, lane[0], lane).end).toBeCloseTo(12.5, 9)
    expect(soundSpan(layout.seams, layout.end, lane[1], lane).start).toBeCloseTo(12.7, 9)
  })
})
