import { describe, expect, it } from 'vitest'
import { CANCELLED, cancellable, levelOf, recordStart } from './voCapture'

describe('voiceover capture helpers (QA-085)', () => {
  it('places the clip on the frame grid', () => {
    expect(recordStart(5.957641, 30)).toBeCloseTo(179 / 30, 9)
    expect(recordStart(5.957641, 30000 / 1001) * (30000 / 1001)).toBeCloseTo(179, 6)
    expect(recordStart(-1, 30)).toBe(0)
  })

  it('meters RMS in dBFS over a -60..0 scale', () => {
    const full = Array.from({ length: 1024 }, (_, i) => Math.sin(i / 5))       // ~ -3 dBFS
    const quiet = full.map((v) => v * 0.01)                                   // ~ -43 dBFS
    expect(levelOf(full).dbfs).toBeCloseTo(-3.0, 0)
    expect(levelOf(quiet).fraction).toBeGreaterThan(0.2)
    expect(levelOf(quiet).fraction).toBeLessThan(levelOf(full).fraction)
    expect(levelOf(new Array(256).fill(0))).toEqual({ dbfs: -Infinity, fraction: 0 })
  })

  it('a pending mic request can be cancelled, and a late grant is released', async () => {
    let grant: (v: string) => void = () => {}
    const late: string[] = []
    const c = cancellable(new Promise<string>((r) => { grant = r }), (v) => late.push(v))
    c.cancel()
    expect(await c.promise).toBe(CANCELLED)
    grant('stream-1')
    await new Promise((r) => setTimeout(r, 0))
    expect(late).toEqual(['stream-1'])
  })

  it('an answered request is passed through untouched', async () => {
    const c = cancellable(Promise.resolve('stream-2'), () => { throw new Error('not late') })
    expect(await c.promise).toBe('stream-2')
  })
})
