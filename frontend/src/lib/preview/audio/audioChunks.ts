// Decoded proxy SOUND (instant preview spec §5.1 "audio sidecar", §9.1): the
// FLAC chunks `ingest/proxy.py` cuts from ffmpeg's decode of each master —
// 48 kHz stereo, exactly `chunk_samples` per chunk, 24-bit, a chunk whose
// peak was over full scale stored divided by a power of two (`X-Audio-Gain` /
// index.json `audio.chunk_gain` multiplies it back, exactly) — fetched,
// decoded with `decodeAudioData` (FLAC in WebKit and Chromium; never the
// master's AAC, whose priming/edit list the export's decode already handled),
// and kept as plain Float32Arrays in an LRU capped at 96 MB.
//
// The decoded samples are the samples the export mixes: WebKit's FLAC decode
// equals ffmpeg's decode of the same chunk bit for bit, and is within the
// 24-bit step of ffmpeg's float decode of the master (tests/wk/test_wk_audio.py).

export const AUDIO_RATE = 48000
export const DEFAULT_CAP_BYTES = 96 * 1024 * 1024
/** Retry cadence of a `202 Retry-After` answer (on-demand audio encode). */
const DEFAULT_RETRY_S = 0.2
const DEFAULT_MAX_WAIT_S = 30
/** Network errors (not HTTP statuses) retried before a chunk load fails. */
const NETWORK_RETRIES = 3
/** Re-reads (ms apart) of an index whose sound is still being built: its
 *  recorded peaks come with the built sound (K2, 0.8.0 QA). ~40 s in all. */
const LAYOUT_RECHECK_MS = [250, 500, 1000, 1000, 2000, 2000, 4000, 4000, 8000, 8000, 8000]

/** The index was read while the sound was still being built (no sample
 *  count, no recorded peaks): it is re-read until it is. */
const building = (l: AudioLayout) => !l.silent && l.samples === null

export interface AudioLayout {
  rate: number
  chunkSamples: number
  /** Total source samples (null while the sidecar is still being built). */
  samples: number | null
  chunks: number | null
  /** The master has no sound: every sample is silence. */
  silent: boolean
  /** Linear gain per chunk ("n" → factor; absent = 1). */
  chunkGain: Record<string, number>
  /** Each chunk's peak (max |x| of the float decode, rounded up; index.json
   *  `audio.chunk_peak`), null when the index has none (gate RX). */
  chunkPeak: number[] | null
}

export interface Pcm {
  L: Float32Array
  R: Float32Array
}

export class AudioChunkError extends Error {
  readonly status: number
  constructor(message: string, status: number) {
    super(message)
    this.status = status
  }
}

export interface AudioChunksOptions {
  /** Route prefix: `<base>/<key>/index.json`, `<base>/<key>/a/NNNN.flac`. */
  base?: string
  fetch?: (url: string) => Promise<Response>
  /** FLAC bytes → AudioBuffer at 48 kHz (default: an OfflineAudioContext). */
  decode?: (bytes: ArrayBuffer) => Promise<AudioBuffer>
  capBytes?: number
  /** Give up on a chunk still answering 202 after this long. */
  maxWaitS?: number
  sleep?: (ms: number) => Promise<void>
}

interface Entry { pcm: Pcm; bytes: number }

const pad4 = (n: number) => String(n).padStart(4, '0')
const realSleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms))

let sharedDecodeCtx: OfflineAudioContext | null = null

/** decodeAudioData on a 48 kHz OfflineAudioContext: the buffer comes back at
 *  the chunk's own rate (no device-rate resampling, whatever the live
 *  AudioContext runs at). */
export function defaultDecode(bytes: ArrayBuffer): Promise<AudioBuffer> {
  if (!sharedDecodeCtx) sharedDecodeCtx = new OfflineAudioContext(2, 1, AUDIO_RATE)
  return sharedDecodeCtx.decodeAudioData(bytes)
}

/** Parse index.json's `audio` object. */
export function parseLayout(index: unknown): AudioLayout {
  const a = ((index as { audio?: Record<string, unknown> } | null)?.audio ?? {}) as Record<string, unknown>
  const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)
  const gains: Record<string, number> = {}
  const cg = a.chunk_gain
  if (cg && typeof cg === 'object') {
    for (const [k, v] of Object.entries(cg as Record<string, unknown>)) {
      const g = num(v)
      if (g !== null && g > 0) gains[k] = g
    }
  }
  const cp = Array.isArray(a.chunk_peak) ? (a.chunk_peak as unknown[]).map(num) : null
  return {
    rate: num(a.rate) ?? AUDIO_RATE,
    chunkSamples: num(a.chunk_samples) ?? 240000,
    samples: num(a.samples),
    chunks: num(a.chunks),
    silent: a.silent === true,
    chunkGain: gains,
    chunkPeak: cp && cp.every((v): v is number => v !== null && v >= 0) ? cp : null,
  }
}

/** Fetch + decode + LRU of proxy FLAC chunks, keyed by proxy key. */
export class AudioChunks {
  readonly base: string
  readonly capBytes: number
  private readonly fetchFn: (url: string) => Promise<Response>
  private readonly decode: (bytes: ArrayBuffer) => Promise<AudioBuffer>
  private readonly maxWaitS: number
  private readonly sleep: (ms: number) => Promise<void>
  private readonly lru = new Map<string, Entry>()
  private readonly inflight = new Map<string, Promise<Pcm>>()
  private readonly layouts = new Map<string, Promise<AudioLayout>>()
  private readonly known = new Map<string, AudioLayout>()
  private readonly rechecking = new Set<string>()
  private gen = 0
  private held = 0
  /** A layout learned again changed (the sound was built after the first
   *  read): its peaks and sample count are known now. */
  onLayoutChange: ((key: string) => void) | null = null
  /** Counters for telemetry and tests. */
  readonly stats = { fetched: 0, decoded: 0, hits: 0, evicted: 0, retries: 0 }

  constructor(opts: AudioChunksOptions = {}) {
    this.base = (opts.base ?? '/api/proxies').replace(/\/$/, '')
    this.fetchFn = opts.fetch ?? ((u) => fetch(u))
    this.decode = opts.decode ?? defaultDecode
    this.capBytes = opts.capBytes ?? DEFAULT_CAP_BYTES
    this.maxWaitS = opts.maxWaitS ?? DEFAULT_MAX_WAIT_S
    this.sleep = opts.sleep ?? realSleep
  }

  get bytes(): number {
    return this.held
  }

  get size(): number {
    return this.lru.size
  }

  /** The source's audio layout (index.json), fetched once per key. */
  layout(key: string): Promise<AudioLayout> {
    let p = this.layouts.get(key)
    if (!p) {
      p = this.get(`${this.base}/${key}/index.json`).then(async (r) => {
        const l = parseLayout(await r.json())
        this.known.set(key, l)
        if (building(l)) this.recheck(key)
        return l
      })
      p.catch(() => this.layouts.delete(key))
      this.layouts.set(key, p)
    }
    return p
  }

  /** Re-read `key`'s index until its sound is built (K2, 0.8.0 QA: a layout
   *  read while it was being built stayed peak-less for the page's life, and
   *  every fresh upload showed "≈ Limiter on loud sound"). */
  private recheck(key: string): void {
    if (this.rechecking.has(key)) return
    this.rechecking.add(key)
    const gen = this.gen
    void (async () => {
      try {
        for (const ms of LAYOUT_RECHECK_MS) {
          await this.sleep(ms)
          if (gen !== this.gen) return
          let l: AudioLayout
          try {
            l = parseLayout(await (await this.get(`${this.base}/${key}/index.json`)).json())
          } catch {
            continue
          }
          if (gen !== this.gen) return
          if (building(l)) continue
          this.known.set(key, l)
          this.layouts.set(key, Promise.resolve(l))
          this.onLayoutChange?.(key)
          return
        }
      } finally {
        this.rechecking.delete(key)
      }
    })()
  }

  /** The layout if it has already been fetched. */
  layoutNow(key: string): AudioLayout | null {
    return this.known.get(key) ?? null
  }

  /** Chunk `n` of `key`, decoded and multiplied back by its headroom gain. */
  chunk(key: string, n: number): Promise<Pcm> {
    const id = `${key}/${n}`
    const hit = this.lru.get(id)
    if (hit) {
      this.stats.hits++
      this.lru.delete(id)
      this.lru.set(id, hit)
      return Promise.resolve(hit.pcm)
    }
    let p = this.inflight.get(id)
    if (!p) {
      p = this.load(key, n).finally(() => this.inflight.delete(id))
      this.inflight.set(id, p)
    }
    return p
  }

  /** A chunk already in memory (no fetch), refreshing its LRU slot. */
  peek(key: string, n: number): Pcm | null {
    const id = `${key}/${n}`
    const hit = this.lru.get(id)
    if (!hit) return null
    this.lru.delete(id)
    this.lru.set(id, hit)
    return hit.pcm
  }

  /** Whether chunk `n` of `key` is in memory (no LRU refresh). */
  has(key: string, n: number): boolean {
    return this.lru.has(`${key}/${n}`)
  }

  /** Load every chunk covering source samples [a, b). */
  async ensure(key: string, a: number, b: number): Promise<void> {
    const l = await this.layout(key)
    if (l.silent || b <= a) return
    const last = l.samples !== null ? Math.min(b, l.samples) : b
    const n0 = Math.max(0, Math.floor(a / l.chunkSamples))
    const n1 = Math.ceil(Math.max(a + 1, last) / l.chunkSamples)
    const jobs: Array<Promise<Pcm>> = []
    for (let n = n0; n < n1; n++) if (l.chunks === null || n < l.chunks) jobs.push(this.chunk(key, n))
    await Promise.all(jobs)
  }

  /** Drop everything (dispose, tests). */
  clear(): void {
    this.lru.clear()
    this.held = 0
    this.gen++
  }

  private async get(url: string): Promise<Response> {
    const t0 = Date.now()
    let failures = 0
    for (;;) {
      let r: Response
      try {
        r = await this.fetchFn(url)
      } catch (e) {
        // A dropped loopback connection (a busy server): retry a few times
        // with backoff before giving up.
        if (++failures > NETWORK_RETRIES) throw e
        this.stats.retries++
        await this.sleep(DEFAULT_RETRY_S * 1000 * failures)
        continue
      }
      if (r.status === 202) {
        const ra = Number(r.headers.get('Retry-After'))
        if ((Date.now() - t0) / 1000 > this.maxWaitS) throw new AudioChunkError(`${url}: still pending`, 202)
        this.stats.retries++
        await this.sleep(1000 * (Number.isFinite(ra) && ra > 0 ? ra : DEFAULT_RETRY_S))
        continue
      }
      if (!r.ok) throw new AudioChunkError(`${url}: HTTP ${r.status}`, r.status)
      return r
    }
  }

  private async load(key: string, n: number): Promise<Pcm> {
    const layout = await this.layout(key)
    const r = await this.get(`${this.base}/${key}/a/${pad4(n)}.flac`)
    this.stats.fetched++
    const header = Number(r.headers.get('X-Audio-Gain'))
    const gain = Number.isFinite(header) && header > 0 ? header : (layout.chunkGain[String(n)] ?? 1)
    const buf = await this.decode(await r.arrayBuffer())
    this.stats.decoded++
    if (buf.sampleRate !== AUDIO_RATE) {
      throw new AudioChunkError(`${key}/${n}: decoded at ${buf.sampleRate} Hz, not ${AUDIO_RATE}`, 0)
    }
    const L = buf.getChannelData(0).slice()
    const R = buf.numberOfChannels > 1 ? buf.getChannelData(1).slice() : L.slice()
    if (gain !== 1) {
      for (let i = 0; i < L.length; i++) { L[i] *= gain; R[i] *= gain }
    }
    const pcm: Pcm = { L, R }
    this.insert(`${key}/${n}`, { pcm, bytes: (L.byteLength + R.byteLength) })
    return pcm
  }

  private insert(id: string, e: Entry): void {
    const old = this.lru.get(id)
    if (old) this.held -= old.bytes
    this.lru.set(id, e)
    this.held += e.bytes
    while (this.held > this.capBytes && this.lru.size > 1) {
      const oldest = this.lru.keys().next().value as string
      if (oldest === id) break
      this.held -= this.lru.get(oldest)!.bytes
      this.lru.delete(oldest)
      this.stats.evicted++
    }
  }
}

/** What the mix reads its source sound through (by `src`, the EDL's path). */
export interface PcmReader {
  /** Load the chunks covering source samples [a, b) of `src`. */
  load(src: string, a: number, b: number): Promise<void>
  /** Copy `count` source samples starting at `first` going `dir` into
   *  `L`/`R` at `off`. Samples outside the source are silence. Returns false
   *  when a needed chunk is not in memory (its samples are left silent). */
  copy(src: string, first: number, count: number, dir: 1 | -1, L: Float32Array, R: Float32Array, off: number): boolean
  /** Whether every chunk covering source samples [a, b) is in memory (a
   *  silent source always is; a source with no proxy key never is). */
  ready(src: string, a: number, b: number): boolean
  /** Whether the source has no sound at all. */
  silent(src: string): boolean
  /** The recorded peak of source samples [a, b) (chunk resolution): 0 for a
   *  silent source or one with no proxy, null while not known. */
  peak?(src: string, a: number, b: number): number | null
}

/** A PcmReader over AudioChunks; `keyOf` maps an EDL `src` to its proxy key
 *  (null: no proxy — silence). */
export function chunkReader(chunks: AudioChunks, keyOf: (src: string) => string | null): PcmReader {
  return {
    async load(src, a, b) {
      const key = keyOf(src)
      if (key) await chunks.ensure(key, Math.max(0, a), b)
    },
    silent(src) {
      const key = keyOf(src)
      return !key || (chunks.layoutNow(key)?.silent ?? false)
    },
    peak(src, a, b) {
      const key = keyOf(src)
      if (!key) return 0
      const l = chunks.layoutNow(key)
      if (!l) return null
      if (l.silent) return 0
      if (!l.chunkPeak) return null
      const n0 = Math.max(0, Math.floor(a / l.chunkSamples))
      const n1 = Math.min(l.chunkPeak.length, Math.ceil(b / l.chunkSamples))
      let p = 0
      for (let n = n0; n < n1; n++) p = Math.max(p, l.chunkPeak[n])
      return p
    },
    ready(src, a, b) {
      const key = keyOf(src)
      if (!key) return false
      const l = chunks.layoutNow(key)
      if (!l) return false
      if (l.silent || b <= a) return true
      const end = l.samples ?? Number.MAX_SAFE_INTEGER
      const lo = Math.max(0, a)
      const hi = Math.min(b, end)
      for (let n = Math.floor(lo / l.chunkSamples); n * l.chunkSamples < hi; n++) {
        if (!chunks.has(key, n)) return false
      }
      return true
    },
    copy(src, first, count, dir, L, R, off) {
      const key = keyOf(src)
      if (!key || count <= 0) return !!key || count <= 0
      const l = chunks.layoutNow(key)
      if (!l) return false
      if (l.silent) return true
      const cs = l.chunkSamples
      const end = l.samples ?? Number.MAX_SAFE_INTEGER
      let ok = true
      let i = 0
      while (i < count) {
        const s = first + dir * i
        if (s < 0 || s >= end) {
          // Outside the source: silence (the rest of the way, going that way).
          const skip = dir > 0 ? (s < 0 ? Math.min(count - i, -s) : count - i) : (s >= end ? Math.min(count - i, s - end + 1) : count - i)
          i += skip
          continue
        }
        const n = Math.floor(s / cs)
        const pos = s - n * cs
        // Samples of this chunk reachable going `dir`, and still in the source.
        const avail = dir > 0 ? Math.min(cs - pos, end - s) : pos + 1
        const take = Math.min(count - i, avail)
        const pcm = chunks.peek(key, n)
        if (!pcm) {
          ok = false
        } else if (dir > 0) {
          L.set(pcm.L.subarray(pos, pos + take), off + i)
          R.set(pcm.R.subarray(pos, pos + take), off + i)
        } else {
          for (let j = 0; j < take; j++) {
            L[off + i + j] = pcm.L[pos - j]
            R[off + i + j] = pcm.R[pos - j]
          }
        }
        i += take
      }
      return ok
    },
  }
}
