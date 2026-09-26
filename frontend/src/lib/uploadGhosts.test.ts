// QA-044: an import in flight is a ghost clip where it will land.
import { describe, expect, it } from 'vitest'
import type { UploadItem } from './uploadQueue'
import { GHOST_GAP, GHOST_W, uploadGhosts } from './uploadGhosts'

const item = (o: Partial<UploadItem>): UploadItem => ({
  id: 'u', name: 'long.mp4', kind: 'video', stage: 'uploading', progress: 0.42,
  stageStartedAt: 0, addToTimeline: true, ...o,
})
const rows = [{ id: 'v1', type: 'video' }, { id: 'v2', type: 'video' }, { id: 'music', type: 'music' }]
const laneEnd = (id: string) => ({ v1: 500, v2: 80, music: 300 } as Record<string, number>)[id]
const xAt = (_id: string, t: number) => 80 + t * 10

describe('uploadGhosts', () => {
  it('queues video at the end of Main video, stacked in drop order, with the stage label', () => {
    const g = uploadGhosts([item({ id: 'a' }), item({ id: 'b', name: 'b.mov', stage: 'queued' })], rows, laneEnd, xAt)
    expect(g).toEqual([
      { id: 'a', laneId: 'v1', x: 500, w: GHOST_W, label: 'Uploading 42% · long.mp4' },
      { id: 'b', laneId: 'v1', x: 500 + GHOST_W + GHOST_GAP, w: GHOST_W, label: 'Waiting · b.mov' },
    ])
  })
  it('puts a lane drop at its drop time and audio on the Music lane', () => {
    const g = uploadGhosts([
      item({ id: 'd', lane: 'v2', laneStart: 12 }),
      item({ id: 'm', kind: 'audio', stage: 'processing', progress: 0 }),
    ], rows, laneEnd, xAt)
    expect(g.map((x) => [x.id, x.laneId, x.x])).toEqual([['d', 'v2', 200], ['m', 'music', 300]])
  })
  it('skips import-only, failed, and lanes that are not drawn', () => {
    expect(uploadGhosts([
      item({ addToTimeline: false }), item({ stage: 'failed' }), item({ lane: 'tx_hook', laneStart: 1 }),
    ], rows, laneEnd, xAt)).toEqual([])
  })
})
