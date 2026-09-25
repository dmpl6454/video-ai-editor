import { describe, expect, it } from 'vitest'
import { insertAtPlayhead } from './mediaInsert'

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
