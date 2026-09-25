import { describe, expect, it } from 'vitest'
import { lockedTrackOf, lockedNotice } from './trackLock'
import type { EDL } from '../types'

const edl = (locked: boolean) => ({
  canvas: { w: 1080, h: 1920, fps: 30 },
  tracks: [
    { id: 'v1', type: 'video', z: 0, label: 'Main video', locked,
      clips: [{ id: 'c_red', src: '/r.mp4', in: 0, out: 10, start: 0 }] },
    { id: 'text', type: 'text', z: 5, clips: [{ id: 't_1', text: 'hi', start: 0, end: 1 }] },
  ],
}) as unknown as EDL

describe('lockedTrackOf (QA-023: the Properties panel reflects a locked lane)', () => {
  it('names the locked lane of a clip on it', () => {
    expect(lockedTrackOf(edl(true), 'c_red')?.id).toBe('v1')
    expect(lockedNotice(lockedTrackOf(edl(true), 'c_red')!)).toContain('"Main video" is locked')
  })
  it('is null for a clip on an unlocked lane, a missing clip, or no selection', () => {
    expect(lockedTrackOf(edl(false), 'c_red')).toBeNull()
    expect(lockedTrackOf(edl(true), 't_1')).toBeNull()
    expect(lockedTrackOf(edl(true), 'nope')).toBeNull()
    expect(lockedTrackOf(edl(true), null)).toBeNull()
  })
})
