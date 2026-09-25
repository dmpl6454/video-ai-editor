// QA-050: markers were never snap targets, and the result could not say
// whether (or to what) it snapped, so the drag preview could not show it.
import { describe, expect, it } from 'vitest'
import type { EDL } from '../types'
import { snapTargets, snapTo } from './snap'
import { toFrameGrid } from './frameStep'

const edl = {
  canvas: { w: 1920, h: 1080, fps: 30, bg: '#000' },
  duration: 40,
  markers: [{ id: 'm1', time: 5, label: 'm' }],
  tracks: [
    { id: 'v1', type: 'video', z: 0, clips: [{ id: 'a', src: '/a.mp4', in: 0, out: 40, start: 0 }] },
    { id: 'vo', type: 'vo', z: 0, clips: [{ id: 'vo1', src: '/v.wav', in: 0, out: 8, start: 2 }] },
  ],
} as unknown as EDL

describe('snapTargets', () => {
  it('includes markers and the playhead, and skips the dragged clip', () => {
    const t = snapTargets(edl, 12.5, 'vo1')
    expect(t).toContainEqual({ t: 5, kind: 'marker' })
    expect(t).toContainEqual({ t: 12.5, kind: 'playhead' })
    expect(t.some((x) => x.t === 2 || x.t === 10)).toBe(false)
    expect(t).toContainEqual({ t: 40, kind: 'edge' })
  })
})

describe('snapTo', () => {
  it('a drop 0.05 s from a marker lands on it and says so (8 px at 80 px/s)', () => {
    const r = snapTo(4.95, snapTargets(edl, 0, 'vo1'), 8 / 80)
    expect(r.t).toBe(5)
    expect(r.target?.kind).toBe('marker')
  })

  it('outside the radius nothing moves and no target is reported', () => {
    const r = snapTo(4.8, snapTargets(edl, 0, 'vo1'), 8 / 80)
    expect(r).toEqual({ t: 4.8, target: null })
  })

  it('a marker wins a tie with a clip edge', () => {
    const r = snapTo(10, [{ t: 10.05, kind: 'edge' }, { t: 9.95, kind: 'marker' }], 0.1)
    expect(r.target?.kind).toBe('marker')
  })
})

describe('toFrameGrid (QA-049)', () => {
  it('puts client gesture times on the project grid', () => {
    expect(toFrameGrid(4.0125, 30)).toBe(4)
    expect(toFrameGrid(5.205, 30) * 30).toBeCloseTo(156, 9)
    expect(Math.round(toFrameGrid(2.1625, 30000 / 1001) * (30000 / 1001) * 1e6) / 1e6).toBe(65)
  })
})
