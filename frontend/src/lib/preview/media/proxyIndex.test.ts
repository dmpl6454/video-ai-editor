// ProxyStore (spec §5.1-5.2, §11.3): index + init per proxy, span packs in a
// byte-bounded LRU, 202 → retry after Retry-After, 410 → failed, and the
// cold-span fast path — the pack header, then the one sample, by HTTP Range.
// Driven by a fake `fetch` that serves real span packs and honours Range.
import { describe, expect, it } from 'vitest'
import { parseInitSegment, writeInitSegment, type TrackFormat } from './fmp4Writer'
import { writeSpanPack } from './spanPack'
import { ProxyStore, type FetchLike } from './proxyIndex'

function be32(n: number) { return [(n >>> 24) & 255, (n >>> 16) & 255, (n >>> 8) & 255, n & 255] }
function rawBox(type: string, payload: number[]): number[] {
  return [...be32(8 + payload.length), ...Array.from(type, (c) => c.charCodeAt(0)), ...payload]
}
function format(w: number, h: number): TrackFormat {
  const sps = [0x67, 0x64, 0x00, 0x29, 0xac, w & 255, h & 255]
  const pps = [0x68, 0xee, 0x3c, 0x80]
  const avcC = rawBox('avcC', [1, 0x64, 0x00, 0x29, 0xff, 0xe1, 0, sps.length, ...sps, 1, 0, pps.length, ...pps])
  const fields = [0, 0, 0, 0, 0, 0, 0, 1, ...new Array(16).fill(0), (w >> 8) & 255, w & 255, (h >> 8) & 255, h & 255,
    0, 0x48, 0, 0, 0, 0x48, 0, 0, 0, 0, 0, 0, 0, 1, ...new Array(32).fill(0), 0, 0x18, 0xff, 0xff]
  const entry = Uint8Array.from(rawBox('avc1', [...fields, ...avcC]))
  const a = Uint8Array.from(avcC.slice(8))
  const hex = Array.from(a, (b) => b.toString(16).padStart(2, '0')).join('')
  return { sampleEntry: entry, width: w, height: h, avcC: a, codec: 'avc1.640029', initKey: hex }
}

const FMT = format(1280, 720)
const INIT = writeInitSegment(FMT)
const sample = (frame: number) => Uint8Array.from([0, 0, 0, 4, 0x65, frame & 255, (frame >> 8) & 255, 7])

interface Served { status: number; body: Uint8Array | object; headers?: Record<string, string> }

/** A proxy of `frames` frames in spans of `span`, with scripted 202s. */
function server(frames: number, span: number, opts: { pending?: Record<string, number>; gone?: boolean; ignoreRange?: boolean; fail?: Record<string, number>; failed?: boolean } = {}) {
  const log: Array<{ url: string; range: string | null; status: number }> = []
  const pending = { ...(opts.pending ?? {}) }
  const fail = { ...(opts.fail ?? {}) }
  const packs = new Map<number, Uint8Array>()
  for (let n = 0; n * span < frames; n++) {
    const first = n * span
    packs.set(n, writeSpanPack(first, Array.from({ length: Math.min(span, frames - first) }, (_, i) => sample(first + i))))
  }
  const serve = (url: string, range: string | null): Served => {
    const path = url.replace(/^\/px\/K\//, '')
    if (opts.gone) return { status: 410, body: {} }
    if (fail[path]) {
      fail[path]--
      return { status: 500, body: {} }
    }
    if (pending[path]) {
      pending[path]--
      return { status: 202, body: {}, headers: { 'Retry-After': '0.01' } }
    }
    if (path === 'index.json') {
      return { status: 200, body: { frames, w: 1280, h: 720, span_frames: span, spans: packs.size, src_rate: { num: 30, den: 1 }, init_key: FMT.initKey, ...(opts.failed ? { failed: true } : {}) } }
    }
    if (path === 'init.mp4') return { status: 200, body: INIT }
    const m = /^v\/(\d{4})\.bin$/.exec(path)
    const pack = m ? packs.get(Number(m[1])) : undefined
    if (!pack) return { status: 404, body: {} }
    const r = range && !opts.ignoreRange ? /bytes=(\d+)-(\d+)/.exec(range) : null
    if (r) return { status: 206, body: pack.subarray(Number(r[1]), Math.min(pack.byteLength, Number(r[2]) + 1)) }
    return { status: 200, body: pack }
  }
  const fetch: FetchLike = async (url, init) => {
    const range = init?.headers?.Range ?? null
    const s = serve(url, range)
    log.push({ url, range, status: s.status })
    const h = s.headers ?? {}
    return {
      status: s.status, ok: s.status >= 200 && s.status < 300,
      headers: { get: (n: string) => h[n] ?? null },
      arrayBuffer: async () => { const b = s.body as Uint8Array; return b.slice().buffer as ArrayBuffer },
      json: async () => s.body,
    }
  }
  return { fetch, log, packs }
}

const settle = async (store: ProxyStore) => {
  for (let i = 0; i < 500 && store.pending > 0; i++) await new Promise((r) => setTimeout(r, 1))
  await new Promise((r) => setTimeout(r, 1))
}

describe('ProxyStore', () => {
  it('opens a proxy: index + init, one avcC class on both sides', async () => {
    const s = server(120, 60)
    const store = new ProxyStore({ baseUrl: '/px', fetch: s.fetch })
    const h = await store.open('K')
    expect(h.spanFrames).toBe(60)
    expect(h.spanCount).toBe(2)
    expect(h.format.initKey).toBe(parseInitSegment(INIT).initKey)
    expect(await store.open('K')).toBe(h)
    expect(s.log.filter((l) => l.url.endsWith('index.json'))).toHaveLength(1)
  })

  it('fetches a span and serves every sample in it synchronously', async () => {
    const s = server(120, 60)
    const loads: number[][] = []
    const store = new ProxyStore({ baseUrl: '/px', fetch: s.fetch, onLoad: (_k, a, n) => loads.push([a, n]) })
    await store.open('K')
    expect(store.sample('K', 70)).toBeNull()
    store.request('K', 70)
    await settle(store)
    expect(loads).toContainEqual([60, 60])
    for (const f of [60, 70, 119]) expect(Array.from(store.sample('K', f)!.bytes)).toEqual(Array.from(sample(f)))
    expect(store.sample('K', 59)).toBeNull()
  })

  it('retries a 202 (on-demand encode) after Retry-After', async () => {
    const s = server(120, 60, { pending: { 'v/0000.bin': 3 } })
    const store = new ProxyStore({ baseUrl: '/px', fetch: s.fetch })
    await store.open('K')
    store.request('K', 5)
    await settle(store)
    expect(store.sample('K', 5)).not.toBeNull()
    expect(store.stats.retries202).toBe(3)
  })

  it('a cold urgent frame is read by Range: header, then just that sample, then the whole span', async () => {
    const s = server(120, 60)
    const loads: number[][] = []
    const store = new ProxyStore({ baseUrl: '/px', fetch: s.fetch, onLoad: (_k, a, n) => loads.push([a, n]) })
    await store.open('K')
    store.request('K', 75, 0, true)
    await settle(store)
    const spanReqs = s.log.filter((l) => l.url.endsWith('0001.bin'))
    expect(spanReqs[0].range).toBe(`bytes=0-${8 + 4 * 60 - 1}`)
    const off = 8 + 4 * 60 + 15 * sample(0).byteLength
    expect(spanReqs[1].range).toBe(`bytes=${off}-${off + sample(75).byteLength - 1}`)
    expect(spanReqs[2].range).toBeNull()
    expect(loads[0]).toEqual([75, 1]) // the one frame first
    expect(loads[1]).toEqual([60, 60])
    expect(store.stats.rangeReads).toBe(1)
  })

  it('a server that ignores Range still works (the 200 body is the whole pack)', async () => {
    const s = server(120, 60, { ignoreRange: true })
    const store = new ProxyStore({ baseUrl: '/px', fetch: s.fetch })
    await store.open('K')
    store.request('K', 3, 0, true)
    await settle(store)
    expect(Array.from(store.sample('K', 3)!.bytes)).toEqual(Array.from(sample(3)))
    expect(Array.from(store.sample('K', 59)!.bytes)).toEqual(Array.from(sample(59)))
  })

  it('keeps at most maxBytes of spans, evicting the least recently used', async () => {
    const s = server(600, 60)
    const packBytes = s.packs.get(0)!.byteLength
    const store = new ProxyStore({ baseUrl: '/px', fetch: s.fetch, maxBytes: packBytes * 3 + 10, concurrency: 1 })
    await store.open('K')
    for (const f of [0, 60, 120]) store.request('K', f)
    await settle(store)
    store.sample('K', 0) // touch span 0: span 1 is now the oldest
    store.request('K', 180)
    await settle(store)
    expect(store.bytes).toBeLessThanOrEqual(packBytes * 3 + 10)
    expect(store.cachedSpans).toBe(3)
    expect(store.sample('K', 0)).not.toBeNull()
    expect(store.sample('K', 60)).toBeNull()
    expect(store.stats.evictions).toBe(1)
  })

  it('runs the most urgent request first', async () => {
    const s = server(600, 60)
    const store = new ProxyStore({ baseUrl: '/px', fetch: s.fetch, concurrency: 1 })
    await store.open('K')
    store.request('K', 0, 50)   // starts at once (nothing else queued)
    store.request('K', 300, 40)
    store.request('K', 480, 1)
    await settle(store)
    const order = s.log.filter((l) => l.url.includes('/v/')).map((l) => l.url.slice(-8, -4))
    expect(order).toEqual(['0000', '0008', '0005'])
  })

  it('a transient 500 on index.json or init.mp4 at open is retried, never marks the proxy failed', async () => {
    for (const path of ['index.json', 'init.mp4']) {
      const s = server(120, 60, { fail: { [path]: 2 } })
      const errors: string[] = []
      const store = new ProxyStore({ baseUrl: '/px', fetch: s.fetch, onError: (k, e) => errors.push(`${k}:${e}`), sleep: () => Promise.resolve() })
      const h = await store.open('K')
      expect(h.spanCount).toBe(2)
      expect(store.failure('K')).toBeNull()
      expect(errors).toEqual([])
      expect(s.log.filter((l) => l.url.endsWith(path)).map((l) => l.status)).toEqual([500, 500, 200])
      expect(store.stats.openRetries).toBe(2)
    }
  })

  it('a network error at open is retried too', async () => {
    const s = server(120, 60)
    let rejects = 1
    const flaky: FetchLike = (url, init) => (rejects-- > 0 ? Promise.reject(new TypeError('Load failed')) : s.fetch(url, init))
    const store = new ProxyStore({ baseUrl: '/px', fetch: flaky, sleep: () => Promise.resolve() })
    await expect(store.open('K')).resolves.toBeTruthy()
    expect(store.failure('K')).toBeNull()
  })

  it('an open that keeps failing reports a TRANSIENT error, and a later open still succeeds', async () => {
    const s = server(120, 60, { fail: { 'index.json': 50 } })
    const errors: Array<[string, boolean]> = []
    const store = new ProxyStore({ baseUrl: '/px', fetch: s.fetch, sleep: () => Promise.resolve(),
      onError: (k, _e, permanent) => errors.push([k, permanent === true]) })
    await expect(store.open('K')).rejects.toThrow(/500/)
    expect(errors).toEqual([['K', false]])
    expect(store.failure('K')).toBeNull()
    const s2 = server(120, 60)
    ;(store as unknown as { fetchFn: FetchLike }).fetchFn = s2.fetch
    await expect(store.open('K')).resolves.toBeTruthy()
  })

  it('index.json failed: true is permanent (one fetch, no retries)', async () => {
    const s = server(120, 60, { failed: true })
    const errors: Array<[string, boolean]> = []
    const store = new ProxyStore({ baseUrl: '/px', fetch: s.fetch, sleep: () => Promise.resolve(),
      onError: (k, _e, permanent) => errors.push([k, permanent === true]) })
    await expect(store.open('K')).rejects.toThrow(/failed/)
    expect(errors).toEqual([['K', true]])
    expect(store.failure('K')).toMatch(/failed/)
    expect(s.log.filter((l) => l.url.endsWith('index.json'))).toHaveLength(1)
  })

  it('410 marks the proxy failed and reports it', async () => {
    const s = server(120, 60, { gone: true })
    const errors: string[] = []
    const store = new ProxyStore({ baseUrl: '/px', fetch: s.fetch, onError: (k, e) => errors.push(`${k}:${e}`) })
    await expect(store.open('K')).rejects.toThrow(/410/)
    expect(store.failure('K')).toMatch(/410/)
    expect(errors[0]).toMatch(/^K:/)
    expect(s.log.filter((l) => l.url.endsWith('index.json'))).toHaveLength(1)
  })
})
