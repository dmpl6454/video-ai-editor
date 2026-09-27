// Keyboard clip selection (review RD3): the clip under the playhead and the
// next / previous clip on a lane, on render time.
import { describe, expect, it } from 'vitest'
import type { EDL } from '../types'
import { adjacentClip, clipAtPlayhead } from './clipSelect'

const clip = (id: string, start: number, dur: number, src = `/m/${id}.normalized.mp4`) =>
  ({ id, src, in: 0, out: dur, start })

function edl(transitions: Array<{ at: number; duration: number }> = []): EDL {
  return {
    version: 3, duration: 9, canvas: { w: 1920, h: 1080, fps: 30, bg: '#000' },
    tracks: [
      { id: 'v1', type: 'video', z: 0, clips: [clip('a', 0, 3), clip('b', 3, 3), clip('c', 6, 3)],
        transitions: transitions.map((t) => ({ ...t, type: 'fade' })) } as never,
      { id: 'v2', type: 'video', z: 1, clips: [clip('p', 1, 2)] },
      { id: 'tx_super', type: 'text', z: 11, clips: [{ id: 't', text: 'Hello', start: 7, end: 8 }] },
    ],
  }
}

describe('clipAtPlayhead', () => {
  it('answers the main track first, then the other lanes', () => {
    expect(clipAtPlayhead(edl(), 0.5)?.id).toBe('a')
    expect(clipAtPlayhead(edl(), 3)?.id).toBe('b')
    expect(clipAtPlayhead(edl(), 8.99)?.id).toBe('c')
    expect(clipAtPlayhead(edl(), 9)).toBeNull()
  })

  it('prefers the lane of the selected clip', () => {
    expect(clipAtPlayhead(edl(), 1.5, 'v2')).toMatchObject({ id: 'p', track: 'v2', name: 'p.mp4' })
    expect(clipAtPlayhead(edl(), 7.5, 'tx_super')?.name).toBe('text "Hello"')
  })

  it('in a crossfade window answers the clip fading in (render time)', () => {
    const e = edl([{ at: 3, duration: 0.5 }])
    expect(clipAtPlayhead(e, 2.6)?.id).toBe('b')         // b plays from 2.5 in render time
    expect(clipAtPlayhead(e, 2.4)?.id).toBe('a')
  })
})

describe('adjacentClip', () => {
  it('walks the selected clip\'s lane, and stops at its ends', () => {
    expect(adjacentClip(edl(), 'a', 0, 1)).toMatchObject({ id: 'b', start: 3 })
    expect(adjacentClip(edl(), 'b', 0, -1)?.id).toBe('a')
    expect(adjacentClip(edl(), 'c', 0, 1)).toBeNull()
    expect(adjacentClip(edl(), 'a', 0, -1)).toBeNull()
    expect(adjacentClip(edl(), 'p', 0, 1)).toBeNull()      // alone on v2
  })

  it('with nothing selected: the main track\'s next / previous clip from the playhead', () => {
    expect(adjacentClip(edl(), null, 1, 1)?.id).toBe('b')
    expect(adjacentClip(edl(), null, 4, -1)?.id).toBe('b')
    expect(adjacentClip(edl(), null, 3, -1)?.id).toBe('a')
    expect(adjacentClip(edl(), null, 0, -1)).toBeNull()
  })
})
