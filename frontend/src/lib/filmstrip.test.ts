// QA-059: twelve stretched tiles per clip (an 85 s clip at 80 px/s got 567 px
// tiles cut from a 6 px band of each thumb) and every tile requested at once.
import { describe, expect, it } from 'vitest'
import { filmstripTiles, thumbGrid, ThumbQueue } from './filmstrip'

describe('filmstripTiles', () => {
  const clip85 = { x: 80, w: 85 * 80, clipH: 28, aspect: 16 / 9, srcIn: 0, srcOut: 85, viewL: 0, viewR: 928 }

  it('tiles have the frame’s own shape, however long the clip', () => {
    const tiles = filmstripTiles(clip85)
    for (const t of tiles) expect(t.w).toBeCloseTo(28 * 16 / 9, 6)
    const long = filmstripTiles({ ...clip85, w: 720 * 80, srcOut: 720 })
    for (const t of long) expect(t.w).toBeCloseTo(28 * 16 / 9, 6)
  })

  it('only the visible tiles exist', () => {
    const tiles = filmstripTiles(clip85)
    expect(tiles.length).toBe(Math.floor((928 - 80) / (28 * 16 / 9)) + 1)
    expect(tiles[0].x).toBe(80)
    const scrolled = filmstripTiles({ ...clip85, viewL: 4000, viewR: 4928 })
    expect(scrolled[0].x).toBeLessThanOrEqual(4000)
    expect(scrolled[scrolled.length - 1].x).toBeLessThanOrEqual(4928)
  })

  it('each tile shows the source time under its own centre (on a zoom-stable grid)', () => {
    const tiles = filmstripTiles(clip85)
    for (const t of tiles) {
      const mid = (t.x - 80 + t.w / 2) / 80
      expect(Math.abs(t.ts - mid)).toBeLessThanOrEqual(thumbGrid(t.w / 80) / 2 + 1e-9)
    }
  })

  it('honours the trimmed source window', () => {
    const tiles = filmstripTiles({ ...clip85, srcIn: 30, srcOut: 40, w: 800 })
    for (const t of tiles) { expect(t.ts).toBeGreaterThanOrEqual(30); expect(t.ts).toBeLessThan(40) }
  })
})

describe('ThumbQueue', () => {
  function harness(max = 2) {
    const started: string[] = []
    const done: Record<string, (ok: boolean) => void> = {}
    const q = new ThumbQueue((key, _url, cb) => { started.push(key); done[key] = cb }, max)
    return { q, started, done }
  }

  it('keeps at most two requests in flight', () => {
    const { q, started, done } = harness()
    q.frame()
    for (let i = 0; i < 12; i++) q.want(`k${i}`, `/u${i}`)
    q.pump()
    expect(started).toEqual(['k0', 'k1'])
    done.k0(true)
    expect(started).toEqual(['k0', 'k1', 'k2'])
    expect(q.inFlight()).toBe(2)
  })

  it('forgets tiles scrolled away before their turn, and re-asks later', () => {
    const { q, started, done } = harness()
    q.frame(); ['a', 'b', 'c', 'd'].forEach((k) => q.want(k, k)); q.pump()
    q.frame(); ['a', 'b', 'd'].forEach((k) => q.want(k, k))   // c scrolled out
    done.a(true); done.b(true)
    expect(started).toEqual(['a', 'b', 'd'])
    expect(q.statusOf('c')).toBeUndefined()
    q.frame(); q.want('c', 'c'); q.pump()
    expect(started).toContain('c')
  })

  it('never retries a failure and never re-requests a success', () => {
    const { q, started, done } = harness()
    q.frame(); q.want('x', 'x'); q.pump(); done.x(false)
    q.frame(); q.want('x', 'x'); q.pump()
    expect(started).toEqual(['x'])
  })
})
