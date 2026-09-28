import { describe, expect, it } from 'vitest'
import { CANCELLED, cancellable, levelOf, recordStart, voLayoutStart } from './voCapture'
import type { EDL } from '../types'

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

// Final QA: the playhead is RENDER time; a voiceover clip's `start` is LAYOUT
// time (audio_mix plays it at render_time(start)). Posting the raw playhead
// put a VO imported at 00:00:05:00 after a 0.5 s dissolve at 4.5 s.
describe('voiceover start on the layout clock', () => {
  const media = (id: string, start: number, len: number) => ({ id, src: `/m/${id}.mp4`, in: 0, out: len, start })
  const edl = (transitions: unknown[]): EDL => ({
    version: 2, duration: 20, canvas: { w: 1920, h: 1080, fps: 30, bg: '#000' },
    tracks: [{ id: 'v1', type: 'video', z: 0, clips: [media('a', 0, 5), media('b', 5, 5), media('c', 10, 5)], transitions }],
  } as unknown as EDL)

  it('an import at render 5.0 after a 0.5 s dissolve at 5 s lands at layout 5.5', () => {
    expect(voLayoutStart(edl([{ at: 5, type: 'fade', duration: 0.5 }]), 5.0)).toBeCloseTo(5.5, 9)
  })

  it('is the frame-snapped playhead without transitions', () => {
    expect(voLayoutStart(edl([]), 5.957641)).toBeCloseTo(179 / 30, 9)
    expect(voLayoutStart(null, -1)).toBe(0)
  })
})
