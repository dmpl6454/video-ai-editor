import { describe, expect, it } from 'vitest'
import { insertAtPlayhead, mainLaneInsert } from './mediaInsert'

const edl = {
  version: 3, duration: 10, canvas: { w: 1920, h: 1080, fps: 30 },
  tracks: [{ id: 'v1', type: 'video', z: 1, clips: [], transitions: [] },
           { id: 'music', type: 'music', z: 0, clips: [] }],
} as never

describe('insert a library item at the playhead (wave-B review, QA-010)', () => {
  it('video goes on the main track at the playhead, on the frame grid', () => {
    expect(insertAtPlayhead({ src: '/u/a.mp4', kind: 'video', duration: 12 }, edl, 3.0137))
      .toEqual({ tool: 'add_clip', args: { track: 'v1', src: '/u/a.mp4', in: 0, out: 12, start: 3 } })
  })
  it('sound goes on the Music lane; a photo is 5 s; an offline item is not insertable', () => {
    expect(insertAtPlayhead({ src: '/u/s.wav', kind: 'audio', duration: 4 }, edl, 1)?.args.track).toBe('music')
    expect(insertAtPlayhead({ src: '/u/p.png', kind: 'video', duration: 300, still: true }, edl, 0)?.args.out).toBe(5)
    expect(insertAtPlayhead({ src: '/u/x.mp4', kind: 'video', duration: 3, missing: true }, edl, 0)).toBeNull()
  })
})

describe('mainLaneInsert (Final QA: the main track is magnetic)', () => {
  const clips = [{ start: 0, d: 20 }]
  const fp = (c: { d: number }) => c.d
  it('a time inside a clip inserts and names the clip it splits', () => {
    expect(mainLaneInsert(clips, 4, fp)).toEqual({ insert: true, under: clips[0] })
  })
  it('a time on a cut inserts without a split', () => {
    const two = [{ start: 0, d: 2 }, { start: 2, d: 2 }]
    expect(mainLaneInsert(two, 2, fp)).toEqual({ insert: true, under: null })
  })
  it('at or past the end appends; an empty lane appends', () => {
    expect(mainLaneInsert(clips, 20, fp)).toEqual({ insert: false, under: null })
    expect(mainLaneInsert([], 3, fp)).toEqual({ insert: false, under: null })
  })
})
