// The bake splice in the program feed (spec §4.1 step 8, §5.3, §7): BAKED
// frames switch to the bake's frame k only once that bake sample HAS LANDED
// (so laneA never cuts the RAW frames out before the bake is there), bake
// ids route to the bake store, and a structural demotion makes a range BAKED.
import { describe, expect, it } from 'vitest'
import { BAKE, CONTENT_SPAN, ProgramFeed } from './engineFeed'
import type { EngineSourceLookup } from './engine'
import type { EdlLike } from './timeline/framePlan'
import { sourceFromJson } from './timeline/frameMap'
import type { ProxyStore } from './media/proxyIndex'
import { MODE_BAKED } from './timeline/support'
import { fullFrameGeometry } from './render/bakeGeometry'
import { sourceUvAt } from './render/geometry'

const R30 = { num: 30, den: 1 }
const CANVAS = { w: 640, h: 360 }
const info = sourceFromJson({ rate: [30, 1], tb: [1, 15360], frames: 900, w: 1280, h: 720 })
const lookup: EngineSourceLookup = (src) => ({ info, proxy: { key: `K${src}` } })

/** Three 1 s clips; the middle one is colour graded (BAKED in Phase 1). */
const EDL: EdlLike = {
  duration: 3, canvas: { w: 640, h: 360, fps: 30 },
  tracks: [{ id: 'v1', clips: [
    { id: 'a', src: 'A', in: 0, out: 1, start: 0 },
    { id: 'b', src: 'A', in: 2, out: 3, start: 1, effects: [{ type: 'color', params: { brightness: 0.2 } }] },
    { id: 'c', src: 'A', in: 4, out: 5, start: 2 },
  ] }],
}

function store(name: string, landed = new Set<number>()) {
  const requests: Array<[string, number]> = []
  return {
    name, landed, requests,
    handle: (key: string) => ({ key, index: { w: name === 'bake' ? 640 : 1280, h: name === 'bake' ? 360 : 720 }, spanFrames: 60 }),
    spanOf: (_k: string, f: number) => Math.floor(f / 60),
    has: (_k: string, f: number) => landed.has(f),
    request: (key: string, frame: number) => { requests.push([key, frame]) },
    reprioritize: () => {},
    sample: (key: string, frame: number) => ({ format: { initKey: `${name}:${key}` }, bytes: new Uint8Array([frame & 255]) }),
    open: () => Promise.resolve(),
  }
}

function setup(landed: number[] = []) {
  const main = store('proxy')
  const bake = store('bake', new Set(landed))
  const feed = new ProgramFeed(main as unknown as ProxyStore)
  feed.bakeStore = bake as unknown as ProxyStore
  feed.build(EDL, lookup, R30, CANVAS)
  return { feed, main, bake, support: feed.classify()! }
}

describe('bake splice in the program feed', () => {
  it('the graded clip is BAKED; nothing changes before its render lands', () => {
    const { feed, support } = setup([30, 31])
    expect(ProgramFeed.bakedRanges(support)).toEqual([[30, 60]])
    const before = Array.from(feed.want)
    expect(feed.applyBake(support)).toBe(0)          // no bake hash yet
    expect(Array.from(feed.want)).toEqual(before)
  })

  it('switches ONLY the BAKED frames whose bake sample has landed, to bake frame k', () => {
    const { feed, support } = setup([30, 31, 45, 10, 70])
    const before = Array.from(feed.want)
    feed.setBake('0123456789abcdef')
    expect(feed.applyBake(support)).toBe(3)
    for (let k = 0; k < 90; k++) {
      const baked = [30, 31, 45].includes(k)
      expect(feed.isBaked(k)).toBe(baked)
      if (!baked) expect(feed.want[k]).toBe(before[k])   // RAW until its bake lands; outside untouched
    }
    expect(feed.want[31] % CONTENT_SPAN).toBe(31)          // bake frame k is output frame k (R13)
    expect(feed.applyBake(support)).toBe(0)                // idempotent
  })

  it('routes bake ids to the bake store (handle, bytes, requests) and proxy ids to the proxy store', () => {
    const { feed, main, bake, support } = setup([40])
    feed.setBake('0123456789abcdef')
    feed.applyBake(support)
    expect(feed.handleAt(40)!.index.w).toBe(640)
    expect(feed.handleAt(10)!.index.w).toBe(1280)
    const lp = feed.laneProgram()
    expect(lp.sample(feed.want[40])!.format.initKey).toBe('bake:0123456789abcdef')
    expect(lp.sample(feed.want[10])!.format.initKey).toBe('proxy:KA')
    lp.request(feed.want[40], 0, true)
    expect(bake.requests.at(-1)).toEqual(['0123456789abcdef', 40])
    feed.prefetch(40, 60, 60)
    expect(main.requests.every(([k]) => !k.startsWith(BAKE))).toBe(true)
  })

  it('prefetchBake asks the bake store for BAKED frames only, once per span, nearest first', () => {
    const { feed, bake, support } = setup()
    feed.setBake('0123456789abcdef')
    feed.prefetchBake(support, 50, 60, 60)
    // BAKED frames 30..59 all sit in bake span 0: one request, at the playhead
    expect(bake.requests).toEqual([['0123456789abcdef', 50]])
    bake.requests.length = 0
    feed.prefetchBake(support, 10, 5, 10)     // no BAKED frame in [5, 20): nothing
    expect(bake.requests).toEqual([])
  })

  it('a new program forgets the bake: build() rebuilds RAW frames', () => {
    const { feed, support } = setup([35])
    feed.setBake('0123456789abcdef')
    feed.applyBake(support)
    expect(feed.isBaked(35)).toBe(true)
    feed.build(EDL, lookup, R30, CANVAS)
    expect(feed.isBaked(35)).toBe(false)
  })

  it('a structural demotion makes the range BAKED (R14)', () => {
    const { feed } = setup()
    feed.demote = [[5, 12]]
    const s = feed.classify()!
    for (let k = 0; k < 90; k++) expect(s.mode[k] === MODE_BAKED).toBe((k >= 5 && k < 12) || (k >= 30 && k < 60))
  })
})

describe('bake geometry', () => {
  it('stretches the bake over the whole canvas, no clip pass', () => {
    const g = fullFrameGeometry({ w: 640, h: 360 }, { w: 320, h: 180 })
    expect(sourceUvAt(g, 0, 0)).toEqual([0, 0])
    expect(sourceUvAt(g, 320, 180)).toEqual([0.5, 0.5])
    expect(sourceUvAt(g, 640, 360)).toEqual([1, 1])
    expect(g.gain).toBe(1)
  })
})
