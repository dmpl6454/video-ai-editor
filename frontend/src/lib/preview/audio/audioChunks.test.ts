// AudioChunks: route shape, 202 retry, headroom gain, de-duplication, the
// 96 MB LRU, and the PcmReader's sample addressing across chunk edges,
// backwards, and outside the source. (Decoding real FLAC — and proving it
// sample-exact — happens in WebKit: tests/wk/test_wk_audio.py.)
import { describe, expect, it } from 'vitest'
import { AudioChunkError, AudioChunks, chunkReader, parseLayout } from './audioChunks'

const CS = 1000                                           // chunk samples (tests)

function fakeServer(opts: { samples?: number; pending?: number; gains?: Record<string, number>; status?: number; silent?: boolean; peaks?: number[] } = {}) {
  const samples = opts.samples ?? 2500
  const hits: string[] = []
  let pending = opts.pending ?? 0
  const index = { audio: { rate: 48000, chunk_samples: CS, samples, chunks: Math.ceil(samples / CS), silent: !!opts.silent, chunk_gain: opts.gains ?? {},
    ...(opts.peaks ? { chunk_peak: opts.peaks } : {}) } }
  const fetch = async (url: string): Promise<Response> => {
    hits.push(url)
    if (url.endsWith('index.json')) return new Response(JSON.stringify(index))
    if (opts.status) return new Response('', { status: opts.status })
    if (pending > 0) { pending--; return new Response('{}', { status: 202, headers: { 'Retry-After': '0.2' } }) }
    const n = Number(/a\/(\d+)\.flac$/.exec(url)![1])
    const len = Math.min(CS, samples - n * CS)
    // "FLAC" bytes: the chunk number and length (the fake decoder builds the ramp).
    const g = opts.gains?.[String(n)]
    return new Response(new Uint32Array([n, len]).buffer, { headers: g ? { 'X-Audio-Gain': String(g) } : {} })
  }
  const decode = async (bytes: ArrayBuffer): Promise<AudioBuffer> => {
    const [n, len] = new Uint32Array(bytes)
    const div = opts.gains?.[String(n)] ?? 1
    const L = Float32Array.from({ length: len }, (_v, i) => (n * CS + i) / div)
    const R = Float32Array.from({ length: len }, (_v, i) => -(n * CS + i) / div)
    return { sampleRate: 48000, numberOfChannels: 2, length: len, getChannelData: (c: number) => (c ? R : L) } as unknown as AudioBuffer
  }
  return { fetch, decode, hits }
}

describe('AudioChunks', () => {
  it('fetches <base>/<key>/a/NNNN.flac and multiplies the headroom gain back', async () => {
    const s = fakeServer({ gains: { 1: 4 } })
    const ch = new AudioChunks({ base: '/api/proxies/', fetch: s.fetch, decode: s.decode })
    const c1 = await ch.chunk('k', 1)
    expect(s.hits).toEqual(['/api/proxies/k/index.json', '/api/proxies/k/a/0001.flac'])
    expect(c1.L[5]).toBe(1005)                             // stored /4, multiplied back ×4
    expect(c1.R[5]).toBe(-1005)
    expect(ch.bytes).toBe(2 * 4 * CS)
  })

  it('retries a 202 (on-demand encode) and de-duplicates concurrent requests', async () => {
    const s = fakeServer({ pending: 2 })
    const slept: number[] = []
    const ch = new AudioChunks({ fetch: s.fetch, decode: s.decode, sleep: async (ms) => { slept.push(ms) } })
    const [a, b] = await Promise.all([ch.chunk('k', 0), ch.chunk('k', 0)])
    expect(a).toBe(b)
    expect(slept).toEqual([200, 200])
    expect(ch.stats.retries).toBe(2)
    expect(s.hits.filter((h) => h.endsWith('.flac'))).toHaveLength(3)
  })

  it('retries a dropped connection, then gives up', async () => {
    const s = fakeServer()
    let drops = 2
    const flaky = async (url: string) => {
      if (url.endsWith('.flac') && drops-- > 0) throw new TypeError('Failed to fetch')
      return s.fetch(url)
    }
    const ch = new AudioChunks({ fetch: flaky, decode: s.decode, sleep: async () => {} })
    expect((await ch.chunk('k', 0)).L[3]).toBe(3)
    const dead = new AudioChunks({ fetch: async (u) => { if (u.endsWith('.flac')) throw new TypeError('Failed to fetch'); return s.fetch(u) },
      decode: s.decode, sleep: async () => {} })
    await expect(dead.chunk('k', 0)).rejects.toBeInstanceOf(TypeError)
  })

  it('fails loudly on a failed proxy (410)', async () => {
    const s = fakeServer({ status: 410 })
    const ch = new AudioChunks({ fetch: s.fetch, decode: s.decode })
    await expect(ch.chunk('k', 0)).rejects.toBeInstanceOf(AudioChunkError)
  })

  it('evicts least-recently-used chunks past its byte cap', async () => {
    const s = fakeServer({ samples: 10 * CS })
    const ch = new AudioChunks({ fetch: s.fetch, decode: s.decode, capBytes: 3 * 8 * CS })
    await ch.chunk('k', 0)
    await ch.chunk('k', 1)
    await ch.chunk('k', 2)
    await ch.chunk('k', 0)                                  // refresh 0
    await ch.chunk('k', 3)                                  // evicts 1
    expect([ch.has('k', 0), ch.has('k', 1), ch.has('k', 2), ch.has('k', 3)]).toEqual([true, false, true, true])
    expect(ch.bytes).toBeLessThanOrEqual(3 * 8 * CS)
    expect(ch.stats.evicted).toBe(1)
  })

  it('parses a layout with defaults', () => {
    expect(parseLayout({ audio: { chunk_gain: { 2: 2, x: 'no' }, silent: true } })).toEqual({
      rate: 48000, chunkSamples: 240000, samples: null, chunks: null, silent: true, chunkGain: { 2: 2 }, chunkPeak: null })
  })

  it('parses the recorded chunk peaks (gate RX), and drops a malformed list', () => {
    expect(parseLayout({ audio: { chunk_peak: [0.5, 1.7, 0] } }).chunkPeak).toEqual([0.5, 1.7, 0])
    expect(parseLayout({ audio: { chunk_peak: [0.5, 'x'] } }).chunkPeak).toBeNull()
    expect(parseLayout({ audio: { chunk_peak: [-1] } }).chunkPeak).toBeNull()
  })
})

describe('chunkReader', () => {
  it('bounds a source range by its chunks\' recorded peaks (gate RX)', async () => {
    const s = fakeServer({ samples: 3500, peaks: [0.25, 0.9, 0.5, 0.125] })
    const ch = new AudioChunks({ fetch: s.fetch, decode: s.decode })
    const r = chunkReader(ch, (src) => (src === 'none' ? null : 'k'))
    expect(r.peak!('a', 0, 10)).toBeNull()                 // layout not loaded: unknown
    await ch.layout('k')
    expect(r.peak!('a', 0, CS)).toBe(0.25)
    expect(r.peak!('a', CS - 1, CS + 1)).toBe(0.9)         // straddles chunks 0 and 1
    expect(r.peak!('a', 2 * CS, 3500)).toBe(0.5)
    expect(r.peak!('none', 0, 10)).toBe(0)                 // no proxy: silence
    const quiet = fakeServer({ samples: 2500 })
    const ch2 = new AudioChunks({ fetch: quiet.fetch, decode: quiet.decode })
    const r2 = chunkReader(ch2, () => 'k')
    await ch2.layout('k')
    expect(r2.peak!('a', 0, 10)).toBeNull()                // an index without peaks: unknown
  })

  async function reader(samples = 2500) {
    const s = fakeServer({ samples })
    const ch = new AudioChunks({ fetch: s.fetch, decode: s.decode })
    const r = chunkReader(ch, (src) => (src === 'none' ? null : 'k'))
    await r.load('a', 0, samples)
    return r
  }

  it('copies forward across a chunk edge', async () => {
    const r = await reader()
    const L = new Float32Array(10), R = new Float32Array(10)
    expect(r.copy('a', 995, 10, 1, L, R, 0)).toBe(true)
    expect(Array.from(L)).toEqual([995, 996, 997, 998, 999, 1000, 1001, 1002, 1003, 1004])
    expect(R[9]).toBe(-1004)
  })

  it('copies backwards across a chunk edge (reverse)', async () => {
    const r = await reader()
    const L = new Float32Array(6), R = new Float32Array(6)
    expect(r.copy('a', 1002, 6, -1, L, R, 0)).toBe(true)
    expect(Array.from(L)).toEqual([1002, 1001, 1000, 999, 998, 997])
  })

  it('is silent outside the source, both ways', async () => {
    const r = await reader()
    const L = new Float32Array(6), R = new Float32Array(6)
    r.copy('a', 2497, 6, 1, L, R, 0)
    expect(Array.from(L)).toEqual([2497, 2498, 2499, 0, 0, 0])
    const B = new Float32Array(5)
    r.copy('a', 2, 5, -1, B, new Float32Array(5), 0)
    expect(Array.from(B)).toEqual([2, 1, 0, 0, 0])
    const N = new Float32Array(4)
    r.copy('a', -2, 4, 1, N, new Float32Array(4), 0)
    expect(Array.from(N)).toEqual([0, 0, 0, 1])
  })

  it('knows readiness, and a source with no proxy is never ready', async () => {
    const s = fakeServer({ samples: 5000 })
    const ch = new AudioChunks({ fetch: s.fetch, decode: s.decode })
    const r = chunkReader(ch, (src) => (src === 'none' ? null : 'k'))
    await ch.layout('k')
    expect(r.ready('a', 0, 10)).toBe(false)
    await r.load('a', 1500, 2500)
    expect([r.ready('a', 1500, 2500), r.ready('a', 900, 1100), r.ready('a', 6000, 7000)]).toEqual([true, false, true])
    expect(r.ready('none', 0, 1)).toBe(false)
    const L = new Float32Array(4)
    expect(r.copy('a', 998, 4, 1, L, new Float32Array(4), 0)).toBe(false)   // chunk 0 missing
    expect(Array.from(L)).toEqual([0, 0, 1000, 1001])
  })
})
