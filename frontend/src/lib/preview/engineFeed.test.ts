// ProgramFeed (spec §4.1): content ids that survive edits, gaps, pending
// sources, and the span requests the window makes (nearest first).
import { describe, expect, it } from 'vitest'
import { GAP } from './media/laneA'
import { CONTENT_SPAN, ProgramFeed } from './engineFeed'
import type { EngineSourceLookup } from './engine'
import type { EdlLike } from './timeline/framePlan'
import { sourceFromJson } from './timeline/frameMap'
import type { ProxyStore } from './media/proxyIndex'

const R30 = { num: 30, den: 1 }
const CANVAS = { w: 640, h: 360 }

function edl(clips: Array<[string, number, number, number]>, duration: number): EdlLike {
  return {
    duration, canvas: { w: 640, h: 360, fps: 30 },
    tracks: [{ id: 'v1', clips: clips.map(([src, a, b, start], i) => ({ id: `c${i}`, src, in: a, out: b, start })) }],
  }
}

const info = sourceFromJson({ rate: [30, 1], tb: [1, 15360], frames: 900, w: 1280, h: 720 })
const lookup: EngineSourceLookup = (src) => (src === 'nokey' ? { info, proxy: null } : { info, proxy: { key: `K${src}` } })

/** A recording stand-in for ProxyStore (spans of 60 frames). */
function fakeStore() {
  const requests: Array<[string, number, number, boolean]> = []
  const store = {
    requests,
    ranked: [] as number[],
    handle: (key: string) => ({ key, index: { w: 1280, h: 720 }, spanFrames: 60 }),
    spanOf: (_key: string, frame: number) => Math.floor(frame / 60),
    request: (key: string, frame: number, priority: number, urgent: boolean) => { requests.push([key, frame, priority, urgent]) },
    reprioritize: (rank: (k: string, s: number) => number) => { store.ranked.push(rank('KA', 0), rank('KA', 5)) },
    sample: (key: string, frame: number) => ({ format: { initKey: key }, bytes: new Uint8Array([frame]) }),
    open: () => Promise.resolve(),
  }
  return store
}

describe('ProgramFeed', () => {
  it('names every output frame by (proxy slot, source frame); a gap is GAP', () => {
    const feed = new ProgramFeed(fakeStore() as unknown as ProxyStore)
    feed.build(edl([['A', 0, 1, 0], ['B', 2, 3, 1.5]], 2.5), lookup, R30, CANVAS)
    const w = feed.want
    expect(w).toHaveLength(75)
    expect(w[0]).toBe(0 * CONTENT_SPAN + 0)
    expect(w[29]).toBe(29)
    expect(w[30]).toBe(GAP)
    expect(w[45]).toBe(1 * CONTENT_SPAN + 60)
  })

  it('keeps content ids stable across an edit (slots per proxy key)', () => {
    const feed = new ProgramFeed(fakeStore() as unknown as ProxyStore)
    feed.build(edl([['A', 0, 1, 0], ['B', 0, 1, 1]], 2), lookup, R30, CANVAS)
    const bFirst = feed.want[30]
    // swap order: B first now — its frames keep their id
    const diff = feed.build(edl([['B', 0, 1, 0], ['A', 0, 1, 1]], 2), lookup, R30, CANVAS)
    expect(feed.want[0]).toBe(bFirst)
    expect(diff.dirtyFrames).toEqual([[0, 60]])
  })

  it('a source without a proxy is PENDING: no bytes, no requests', () => {
    const store = fakeStore()
    const feed = new ProgramFeed(store as unknown as ProxyStore)
    feed.build(edl([['nokey', 0, 1, 0]], 1), lookup, R30, CANVAS)
    const lp = feed.laneProgram()
    expect(lp.sample(feed.want[0])).toBeNull()
    lp.request(feed.want[0], 0, true)
    expect(store.requests).toHaveLength(0)
    expect(feed.proxyState('nokey')).toBe('pending')
    expect(feed.handleAt(0)).toBeNull()
  })

  it('prefetch asks once per span, nearest the playhead first, and ranks the queue by distance', () => {
    const store = fakeStore()
    const feed = new ProgramFeed(store as unknown as ProxyStore)
    feed.build(edl([['A', 0, 20, 0]], 20), lookup, R30, CANVAS)
    feed.prefetch(300, 300, 900)
    const spans = store.requests.map(([, f]) => Math.floor(f / 60))
    expect(new Set(spans).size).toBe(spans.length)
    expect(spans[0]).toBe(5)                    // frame 300's span first
    expect(store.requests[0][2]).toBe(0)
    expect(store.ranked).toEqual([241, 0])      // span 0's nearest frame (59) is 241 away; span 5 is here
  })

  it('a failed proxy stops being asked for and classifies as failed', () => {
    const store = fakeStore()
    const feed = new ProgramFeed(store as unknown as ProxyStore)
    feed.build(edl([['A', 0, 1, 0]], 1), lookup, R30, CANVAS)
    feed.markFailed('KA')
    feed.laneProgram().request(feed.want[0], 0, false)
    expect(store.requests).toHaveLength(0)
    expect(feed.proxyState('A')).toBe('failed')
    expect(feed.classify()!.ranges[0].reasons).toContain('proxy:degraded')
  })

  it('a proxy that opens after a failure is readable again and reclassified (not degraded for life)', async () => {
    const store = fakeStore()
    const feed = new ProgramFeed(store as unknown as ProxyStore)
    feed.build(edl([['A', 0, 1, 0]], 1), lookup, R30, CANVAS)
    feed.markFailed('KA')
    expect(feed.proxyState('A')).toBe('failed')
    let recovered = 0
    feed.openProxies(() => undefined, () => { recovered++ })
    await new Promise((r) => setTimeout(r, 0))
    expect(recovered).toBe(1)
    expect(feed.proxyState('A')).toBe('ready')
    expect(feed.classify()!.ranges[0].reasons).not.toContain('proxy:degraded')
    feed.laneProgram().request(feed.want[0], 0, false)
    expect(store.requests).toHaveLength(1)
  })

  // Final QA r3 made every frame APPROX until the gain was measured; K2
  // (0.8.0 QA): that put "≈ Loudness" on every fresh project. Only a measured
  // difference over 1 dB is APPROX; a gain not measured yet is not.
  it('a loudness gain played more than 1 dB off the measured one makes every frame APPROX', () => {
    const feed = new ProgramFeed(fakeStore() as unknown as ProxyStore)
    feed.build(edl([['A', 0, 1, 0]], 1), lookup, R30, CANVAS)
    expect(feed.classify()!.ranges).toEqual([{ k0: 0, k1: 30, mode: 0, reasons: [] }])
    expect(feed.classify(undefined, 0.4)!.ranges).toEqual([{ k0: 0, k1: 30, mode: 0, reasons: [] }])
    expect(feed.classify(undefined, 2.5)!.ranges).toEqual([{ k0: 0, k1: 30, mode: 1, reasons: ['audio:loudness'] }])
  })
})
