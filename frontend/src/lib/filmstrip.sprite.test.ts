// QA-059 remainder: tiles come out of per-source sprites, one request per
// SPRITE_TILES tiles, and the queue holds while the preview loads.
import { describe, expect, it } from 'vitest'
import { filmstripTiles, spriteSlot, spriteUrl, SPRITE_TILES, ThumbQueue } from './filmstrip'

describe('sprite slots', () => {
  it('every tile of a long clip maps to its grid frame, few sprites for many tiles', () => {
    // 12-min clip at 2 px/s: 1440 px, 128 px tiles → 12 tiles, 64 s apart.
    const tiles = filmstripTiles({ x: 80, w: 1440, clipH: 72, aspect: 16 / 9, srcIn: 0, srcOut: 720, viewL: 0, viewR: 2000 })
    expect(tiles.length).toBe(12)
    const slots = tiles.map((t) => spriteSlot(t, 0, 720)!)
    for (const [i, s] of slots.entries()) {
      expect((s.page * SPRITE_TILES + s.index) * s.step).toBeCloseTo(tiles[i].ts, 6)
    }
    const pages = new Set(slots.map((s) => `${s.step}|${s.page}`))
    expect(pages.size).toBe(1)                // 12 tiles → ONE request
  })

  it('never shows a frame outside the clip window', () => {
    // A clip playing source 7.3–9.1 s at a zoom whose grid is 1 s.
    const s = spriteSlot({ ts: 7.3, step: 1 }, 7.3, 9.1)!
    expect(s.index * s.step + s.page * SPRITE_TILES * s.step).toBe(8)
    // Window with no grid point → null (single-thumb fallback).
    expect(spriteSlot({ ts: 7.3, step: 4 }, 7.3, 7.9)).toBeNull()
  })

  it('addresses pages past the first', () => {
    expect(spriteSlot({ ts: 20, step: 1 }, 0, 60)).toEqual({ step: 1, page: 1, index: 4 })
    expect(spriteUrl('s_1', '/a b.mp4', { step: 0.5, page: 2, index: 0 }, 72))
      .toBe('/api/sessions/s_1/thumbstrip?src=%2Fa%20b.mp4&step=0.5&page=2&n=16&h=72')
  })
})

describe('ThumbQueue.hold', () => {
  it('starts nothing while held, then drains two at a time when released', () => {
    const started: string[] = []
    const q = new ThumbQueue((key) => { started.push(key) }, 2)
    q.hold(true)
    q.frame(); q.want('a', 'u'); q.want('b', 'u'); q.want('c', 'u'); q.pump()
    expect(started).toEqual([])
    q.hold(false)
    expect(started).toEqual(['a', 'b'])
  })
})
