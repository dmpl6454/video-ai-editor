// QA-010: the Media panel lists the project's LIBRARY, merged with the live
// timeline for used-counts. Before the fix the bin was
// `edl.tracks → clips → unique src`: an empty timeline meant an empty bin.
import { describe, expect, it } from 'vitest'
import type { EDL, MediaItem } from '../types'
import { binMeta, binRows, clockDuration } from './mediaLibrary'

const item = (over: Partial<MediaItem> = {}): MediaItem => ({
  id: 'aaaaaaaaaaaa', src: '/wd/s_1/uploads/take/take.normalized.mp4', name: 'take one.mp4',
  kind: 'video', origin: 'upload', duration: 4.017, width: 640, height: 360, added: 1,
  uses: 0, clip_ids: [], ...over,
})

const edl = (clips: { id: string; src: string; track?: string; type?: string }[]): EDL => ({
  canvas: { w: 1920, h: 1080, fps: 30 }, duration: 4,
  tracks: ['v1', 'music', 't1'].map((id) => ({
    id, type: id === 'v1' ? 'video' : id === 'music' ? 'music' : 'text',
    clips: clips.filter((c) => (c.track ?? 'v1') === id)
      .map((c) => ({ id: c.id, src: c.src, in: 0, out: 4, start: 0 })),
  })),
} as unknown as EDL)

describe('binRows', () => {
  it('keeps an imported item after its last clip is deleted', () => {
    const rows = binRows([item({ uses: 1, clip_ids: ['c1'] })], edl([]))
    expect(rows).toHaveLength(1)
    expect(rows[0]).toMatchObject({ name: 'take one.mp4', uses: 0, clipIds: [] })
    expect(binMeta(rows[0])).toBe('0:04 · 640×360 · not on timeline')
  })

  it('counts uses from the live timeline, not the last library fetch', () => {
    const it0 = item()
    const rows = binRows([it0], edl([{ id: 'c1', src: it0.src }, { id: 'c2', src: it0.src }]))
    expect(rows[0].uses).toBe(2)
    expect(rows[0].clipIds.sort()).toEqual(['c1', 'c2'])
    expect(binMeta(rows[0])).toBe('0:04 · 640×360 · used ×2')
  })

  it('never shows less than the timeline uses (library not loaded yet)', () => {
    const rows = binRows(null, edl([{ id: 'c1', src: '/wd/s_1/cache/reframe_ab.mp4' }]))
    expect(rows).toEqual([expect.objectContaining({ id: null, name: 'reframe_ab.mp4', uses: 1, clipIds: ['c1'] })])
  })

  it('matches by clip id when the server spells the path differently (symlinked workdir)', () => {
    const it0 = item({ src: '/private/wd/s_1/take.normalized.mp4', uses: 1, clip_ids: ['c1'] })
    const rows = binRows([it0], edl([{ id: 'c1', src: '/wd/s_1/take.normalized.mp4' }]))
    expect(rows).toHaveLength(1)
    expect(rows[0].uses).toBe(1)
  })

  it('ignores stale server clip ids and non-media lanes', () => {
    const it0 = item({ uses: 1, clip_ids: ['gone'] })
    const rows = binRows([it0], edl([{ id: 'x', src: it0.src, track: 't1' }]))
    expect(rows.map((r) => r.uses)).toEqual([0])
  })

  it('lists audio with its own meta', () => {
    const rows = binRows([item({ kind: 'audio', name: 'bed.wav', width: null, height: null, duration: 125 })], null)
    expect(binMeta(rows[0])).toBe('2:05 · audio · not on timeline')
  })
})

describe('clockDuration', () => {
  it('formats m:ss and h:mm:ss, blank for unknown', () => {
    expect(clockDuration(4.017)).toBe('0:04')
    expect(clockDuration(3725)).toBe('1:02:05')
    expect(clockDuration(null)).toBe('')
    expect(clockDuration(Number.NaN)).toBe('')
  })
})
