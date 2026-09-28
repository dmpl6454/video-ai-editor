// Proxy data for the engine (INSTANT_PREVIEW_SPEC §5.1-5.2, §9.1): each
// proxy's `index.json` and `init.mp4`, and its span packs `v/NNNN.bin` held
// in a byte-bounded LRU (128 MB of encoded samples, §11.3).
//
// * `202 Accepted` + `Retry-After` means the server is encoding that span on
//   demand: the request is retried after the advertised delay — back in the
//   queue, not waiting in its fetch slot, so a READY span behind it still
//   gets the next free slot (review RD3).
// * A span that fails transiently (5xx, a network error, a response that
//   never completes within `spanTimeoutMs`) is retried by the store itself,
//   backing off 250 ms → 1 s: a paused engine has nothing else that would
//   ask again (review RD3).
// * A COLD span needed right now (a paused seek into a span not yet fetched)
//   is read with two HTTP Range requests — the pack's header, then just the
//   one sample — so the first frame does not wait for the whole ~1.3 MB pack;
//   the full span is fetched right after, at normal priority.
// * Fetches run a few at a time, most urgent first (the engine re-ranks them
//   by distance from the playhead).
//
// Pure fetch + bytes: no DOM. laneA asks `sample(key, frame)` synchronously
// and gets bytes or null (not here yet: `request` it and wait for `onLoad`).

import { parseInitSegment, type FrameSample, type TrackFormat } from './fmp4Writer'
import { parseSpanPack, SpanPackError } from './spanPack'

export const DEFAULT_SPAN_CACHE_BYTES = 128 * 1024 * 1024
const DEFAULT_CONCURRENCY = 3
const DEFAULT_RETRY_MS = 200
/** Give up on a span after this long of 202s (then the frames stay PENDING
 *  and a later request starts over). */
const MAX_PENDING_MS = 30_000
/** A transient failure opening a proxy (5xx, 429, a network error, an init
 *  still pending) is retried this many times, backing off from
 *  OPEN_BACKOFF_MS (doubling, capped at 4 s: ≈ 7.75 s in all) before the
 *  engine hears of it. Only 410 or `index.failed` are permanent (§5.1, §7). */
const OPEN_RETRIES = 5
const OPEN_BACKOFF_MS = 250
const OPEN_BACKOFF_MAX_MS = 4000
/** A transient span failure is retried after this, doubling up to the max
 *  (1 s: a paused frame whose span failed 8 times shows within ~6 s, and a
 *  span that keeps failing costs one request a second). */
const SPAN_RETRY_MS = 250
const SPAN_RETRY_MAX_MS = 1000
/** Transient failures of one span in a row before the engine hears of it —
 *  the open path's rule applied to spans (Final QA): a span route that keeps
 *  answering 5xx left paused frames on the previous clip under a spinner and
 *  playback buffering for good, never reaching the degraded tier (§7). The
 *  report repeats every this-many further failures while the streak lasts, so
 *  a reopen that cleared the degraded mark (EngineSources) is re-marked. */
export const SPAN_DEGRADE_AFTER = 5
/** A span request (a Range read or the pack) still unanswered after this is
 *  abandoned and retried: a hung response must not hold a slot for good. */
const DEFAULT_SPAN_TIMEOUT_MS = 15_000

/** `index.json` fields the engine reads (ingest/proxy.py `static_index` +
 *  `live_index`). */
export interface ProxyIndexJson {
  key?: string
  frames: number
  w: number
  h: number
  span_frames: number
  /** Span count (ingest/proxy.py) or per-span states (test fixtures). */
  spans: number | readonly string[]
  src_rate: { num: number; den: number }
  codec?: string
  init_key?: string
  span_ready?: readonly boolean[]
  state?: string
  failed?: boolean
}

export interface ProxyHandle {
  readonly key: string
  readonly index: ProxyIndexJson
  readonly format: TrackFormat
  readonly spanFrames: number
  readonly spanCount: number
}

export type FetchLike = (url: string, init?: { headers?: Record<string, string>; signal?: AbortSignal }) => Promise<{
  status: number
  ok: boolean
  headers: { get(name: string): string | null }
  arrayBuffer(): Promise<ArrayBuffer>
  json(): Promise<unknown>
}>

export interface ProxyStoreOptions {
  /** `/api/proxies` in the app; a fixture mount in tests. */
  baseUrl?: string
  fetch?: FetchLike
  maxBytes?: number
  concurrency?: number
  /** A span (or a Range-read sample) landed: its frames are readable now. */
  onLoad?: (key: string, first: number, count: number) => void
  /** A proxy could not be opened (or one of its spans failed
   *  SPAN_DEGRADE_AFTER times in a row). `permanent`: 410 or `index.failed` (the
   *  store will not ask again); otherwise the open failed OPEN_RETRIES times
   *  in a row and a later `open(key)` tries afresh. */
  onError?: (key: string, error: string, permanent: boolean) => void
  /** A span of `key` landed after `key` had been reported through onError
   *  for a span streak (SPAN_DEGRADE_AFTER): its proxy is readable again, so
   *  the owner can undo the degraded tier NOW instead of at its next reopen
   *  (Final QA r2: a source stayed degraded ~10 s after a short hiccup, and
   *  Play waited on it). Once per report. */
  onRecovered?: (key: string) => void
  now?: () => number
  sleep?: (ms: number) => Promise<void>
  /** Abandon a span request after this many ms (default 15 s). */
  spanTimeoutMs?: number
  /** Report a span failing SPAN_DEGRADE_AFTER times in a row through
   *  onError(…, false) (default true: the source's degraded tier). The bake
   *  store opts out — its onError drops the whole bake. */
  reportSpanFailures?: boolean
  /** The page is hidden NOW, its 'visibilitychange' dispatched or not
   *  (WebKit flips visibilityState a task before the event, §3.5): no fetch
   *  or open starts; the owner suspends now and resumes (suspend(false))
   *  when the page is back. */
  hiddenNow?: () => boolean
}

interface SpanEntry {
  bytes: number
  /** Full pack: first frame + samples. */
  first: number
  samples: readonly Uint8Array[] | null
  /** Range-read samples of a pack not yet fetched whole. */
  partial: Map<number, Uint8Array> | null
}

interface Job {
  key: string
  span: number
  priority: number
  /** Range-read just this frame first. */
  frame: number | null
  /** Not before this time (`now()`): a 202's Retry-After or a back-off. */
  notBefore?: number
  /** When the span first answered 202 (MAX_PENDING_MS). */
  pendingSince?: number
}

/** A span answered 202: try again after `afterMs`, out of the slot. */
class SpanPending extends Error {
  readonly afterMs: number
  constructor(afterMs: number) {
    super('span pending')
    this.afterMs = afterMs
  }
}

export class ProxyError extends Error {
  /** 410 or `index.failed`: the proxy will never be served. */
  readonly permanent: boolean
  constructor(message: string, permanent = false) {
    super(message)
    this.name = 'ProxyError'
    this.permanent = permanent
  }
}

const isPermanent = (e: unknown): boolean => e instanceof ProxyError && e.permanent

const pad4 = (n: number) => String(n).padStart(4, '0')

function retryAfterMs(h: { get(name: string): string | null }): number {
  const v = Number(h.get('Retry-After'))
  return Number.isFinite(v) && v >= 0 ? Math.max(20, v * 1000) : DEFAULT_RETRY_MS
}

export class ProxyStore {
  readonly baseUrl: string
  readonly maxBytes: number
  private readonly fetchFn: FetchLike
  private readonly concurrency: number
  private readonly onLoad: ProxyStoreOptions['onLoad']
  private readonly onError: ProxyStoreOptions['onError']
  private readonly onRecovered: ProxyStoreOptions['onRecovered']
  /** Keys reported through onError for a span streak, not landed since. */
  private readonly spanDegraded = new Set<string>()
  private readonly now: () => number
  private readonly sleep: (ms: number) => Promise<void>
  private readonly spanTimeoutMs: number
  private readonly reportSpanFailures: boolean
  private readonly hiddenNow: () => boolean
  /** Transient failures in a row, per span (the back-off). */
  private readonly spanFails = new Map<string, number>()
  /** The earliest wake-up already scheduled for a waiting job. */
  private wakeAt = Infinity

  private readonly handles = new Map<string, ProxyHandle>()
  private readonly opening = new Map<string, Promise<ProxyHandle>>()
  private readonly failed = new Map<string, string>()
  /** LRU: insertion order = recency (re-inserted on use). */
  private readonly spans = new Map<string, SpanEntry>()
  private readonly queue: Job[] = []
  private readonly inFlight = new Set<string>()
  private active = 0
  private disposed = false
  /** The page is hidden (§3.5): no new fetch starts until it is back. */
  private suspended = false
  bytes = 0
  readonly stats = { spanFetches: 0, rangeReads: 0, retries202: 0, evictions: 0, errors: 0, openRetries: 0,
    spanRetries: 0, timeouts: 0 }

  constructor(opts: ProxyStoreOptions = {}) {
    this.baseUrl = (opts.baseUrl ?? '/api/proxies').replace(/\/$/, '')
    this.fetchFn = opts.fetch ?? ((url, init) => fetch(url, init))
    this.maxBytes = opts.maxBytes ?? DEFAULT_SPAN_CACHE_BYTES
    this.concurrency = opts.concurrency ?? DEFAULT_CONCURRENCY
    this.onLoad = opts.onLoad
    this.onError = opts.onError
    this.onRecovered = opts.onRecovered
    this.now = opts.now ?? (() => performance.now())
    this.sleep = opts.sleep ?? ((ms) => new Promise((r) => setTimeout(r, ms)))
    this.spanTimeoutMs = opts.spanTimeoutMs ?? DEFAULT_SPAN_TIMEOUT_MS
    this.reportSpanFailures = opts.reportSpanFailures ?? true
    this.hiddenNow = opts.hiddenNow ?? (() => false)
  }

  private url(key: string, path: string): string {
    return `${this.baseUrl}/${key}/${path}`
  }

  /** GET with 202 retries until `deadline`; 410 → ProxyError. A span
   *  request (`signal`) yields its slot on a 202 instead (SpanPending). */
  private async getOk(url: string, headers?: Record<string, string>, signal?: AbortSignal): Promise<Awaited<ReturnType<FetchLike>>> {
    const start = this.now()
    for (;;) {
      if (this.disposed) throw new ProxyError('disposed')
      const init = headers || signal ? { ...(headers ? { headers } : {}), ...(signal ? { signal } : {}) } : undefined
      const r = await this.fetchFn(url, init)
      if (r.status === 202) {
        this.stats.retries202++
        if (signal) throw new SpanPending(retryAfterMs(r.headers))
        if (this.now() - start > MAX_PENDING_MS) throw new ProxyError(`${url}: still pending after ${MAX_PENDING_MS} ms`)
        await this.sleep(retryAfterMs(r.headers))
        continue
      }
      if (r.status === 410) throw new ProxyError(`${url}: proxy failed (410)`, true)
      if (!r.ok) throw new ProxyError(`${url}: HTTP ${r.status}`)
      return r
    }
  }

  // ------------------------------------------------------------ index/init

  handle(key: string): ProxyHandle | null {
    return this.handles.get(key) ?? null
  }

  failure(key: string): string | null {
    return this.failed.get(key) ?? null
  }

  /** Fetches (once) and parses `index.json` + `init.mp4`. */
  open(key: string): Promise<ProxyHandle> {
    const known = this.handles.get(key)
    if (known) return Promise.resolve(known)
    let p = this.opening.get(key)
    if (!p) {
      p = this.loadRetrying(key).then((h) => {
        this.handles.set(key, h)
        this.opening.delete(key)
        return h
      }, (e: unknown) => {
        this.opening.delete(key)
        const msg = String((e as Error)?.message ?? e)
        const permanent = isPermanent(e)
        // only a proxy the server says is gone is failed for good; anything
        // else is forgotten, so the next request()/open() starts over
        if (permanent) this.failed.set(key, msg)
        if (!this.disposed) this.onError?.(key, msg, permanent)
        throw e
      })
      this.opening.set(key, p)
    }
    return p
  }

  /** `load`, retried with back-off while the failure is transient. */
  private async loadRetrying(key: string): Promise<ProxyHandle> {
    let wait = OPEN_BACKOFF_MS
    for (let attempt = 0; ; attempt++) {
      try {
        return await this.load(key)
      } catch (e) {
        if (isPermanent(e) || this.disposed || attempt >= OPEN_RETRIES - 1) throw e
        this.stats.openRetries++
        await this.sleep(wait)
        wait = Math.min(OPEN_BACKOFF_MAX_MS, wait * 2)
      }
    }
  }

  private async load(key: string): Promise<ProxyHandle> {
    const index = (await (await this.getOk(this.url(key, 'index.json'))).json()) as ProxyIndexJson
    if (index.failed) throw new ProxyError(`${key}: proxy failed`, true)
    const init = new Uint8Array(await (await this.getOk(this.url(key, 'init.mp4'))).arrayBuffer())
    const format = parseInitSegment(init)
    if (index.init_key !== undefined && index.init_key !== format.initKey) {
      throw new ProxyError(`${key}: index init_key does not match init.mp4's avcC`)
    }
    const spanFrames = Math.max(1, index.span_frames)
    const spanCount = typeof index.spans === 'number' ? index.spans : index.spans.length
    this.failed.delete(key)
    return { key, index, format, spanFrames, spanCount }
  }

  // ------------------------------------------------------------ samples

  private spanId(key: string, n: number): string {
    return `${key}/${n}`
  }

  spanOf(key: string, frame: number): number {
    const h = this.handles.get(key)
    return h ? Math.floor(frame / h.spanFrames) : -1
  }

  /** The frame's bytes if they are here (a full span or a Range read). */
  sample(key: string, frame: number): FrameSample | null {
    const h = this.handles.get(key)
    if (!h || frame < 0 || frame >= h.index.frames) return null
    const id = this.spanId(key, Math.floor(frame / h.spanFrames))
    const e = this.spans.get(id)
    if (!e) return null
    // LRU touch
    this.spans.delete(id)
    this.spans.set(id, e)
    let bytes: Uint8Array | undefined
    if (e.samples) bytes = e.samples[frame - e.first]
    else bytes = e.partial?.get(frame)
    return bytes ? { format: h.format, bytes } : null
  }

  has(key: string, frame: number): boolean {
    return this.sample(key, frame) !== null
  }

  /** Ask for the span holding `frame`. `urgent` Range-reads the single frame
   *  first when its span is cold. Lower `priority` runs first. */
  request(key: string, frame: number, priority = 0, urgent = false): void {
    if (this.disposed) return
    const h = this.handles.get(key)
    if (!h) {
      // hidden: the engine asks again when the page is back
      if (!this.failed.has(key) && !this.paused()) void this.open(key).then(() => this.request(key, frame, priority, urgent), () => undefined)
      return
    }
    if (frame < 0 || frame >= h.index.frames) return
    const n = Math.floor(frame / h.spanFrames)
    const e = this.spans.get(this.spanId(key, n))
    if (e?.samples) return
    if (urgent && e?.partial?.has(frame)) urgent = false
    const id = this.spanId(key, n)
    const queued = this.queue.find((j) => j.key === key && j.span === n)
    if (queued) {
      queued.priority = Math.min(queued.priority, priority)
      if (urgent && queued.frame === null) queued.frame = frame
    } else if (!this.inFlight.has(id)) {
      this.queue.push({ key, span: n, priority, frame: urgent ? frame : null })
    } else if (urgent) {
      // the whole span is already on its way: nothing faster to do
    }
    this.drain()
  }

  /** Re-rank everything queued (e.g. by distance from a new playhead). */
  reprioritize(rank: (key: string, span: number) => number): void {
    for (const j of this.queue) j.priority = rank(j.key, j.span)
  }

  /** Drop queued (not in-flight) requests the engine no longer needs. */
  cancelWhere(pred: (key: string, span: number) => boolean): void {
    for (let i = this.queue.length - 1; i >= 0; i--) if (pred(this.queue[i].key, this.queue[i].span)) this.queue.splice(i, 1)
  }

  get pending(): number {
    return this.queue.length + this.active
  }

  /** Page hidden (true): queued requests wait and no new fetch starts
   *  (those already on the wire finish). false: the queue drains again. */
  suspend(on: boolean): void {
    if (this.suspended === on) return
    this.suspended = on
    if (!on) this.drain()
  }

  get isSuspended(): boolean {
    return this.suspended
  }

  /** Requests waiting to start (not on the wire). */
  get queued(): number {
    return this.queue.length
  }

  /** The most urgent job that is due now (index), or -1. */
  private nextDue(t: number): number {
    let best = -1
    for (let i = 0; i < this.queue.length; i++) {
      if ((this.queue[i].notBefore ?? 0) > t) continue
      if (best < 0 || this.queue[i].priority < this.queue[best].priority) best = i
    }
    return best
  }

  /** Suspended, or the page is hidden before its event said so. */
  private paused(): boolean {
    return this.suspended || this.hiddenNow()
  }

  private drain(): void {
    if (this.disposed || this.paused()) return
    const t = this.now()
    while (this.active < this.concurrency) {
      const i = this.nextDue(t)
      if (i < 0) break
      const [job] = this.queue.splice(i, 1)
      const id = this.spanId(job.key, job.span)
      if (this.inFlight.has(id) || this.spans.get(id)?.samples) continue
      this.inFlight.add(id)
      this.active++
      void this.run(job).catch((e: unknown) => this.jobFailed(job, e)).finally(() => {
        this.inFlight.delete(id)
        this.active--
        this.drain()
      })
    }
    this.wake()
  }

  /** Drain again when the earliest waiting job is due. */
  private wake(): void {
    let due = Infinity
    for (const j of this.queue) if ((j.notBefore ?? 0) > this.now()) due = Math.min(due, j.notBefore!)
    if (due === Infinity || due >= this.wakeAt) return
    this.wakeAt = due
    void this.sleep(Math.max(0, due - this.now())).then(() => {
      if (this.wakeAt === due) this.wakeAt = Infinity
      // the wait is over (an injected sleep need not follow `now()`)
      for (const j of this.queue) if ((j.notBefore ?? 0) <= due) j.notBefore = 0
      this.drain()
    })
  }

  /** Back in the queue after `ms` (merged with a request made meanwhile). */
  private requeue(job: Job, ms: number): void {
    job.notBefore = this.now() + ms
    const same = this.queue.find((j) => j.key === job.key && j.span === job.span)
    if (same) {
      same.priority = Math.min(same.priority, job.priority)
      same.frame ??= job.frame
      same.notBefore = Math.max(same.notBefore ?? 0, job.notBefore)
      same.pendingSince ??= job.pendingSince
    } else {
      this.queue.push(job)
    }
  }

  private jobFailed(job: Job, e: unknown): void {
    if (this.disposed) return
    if (e instanceof SpanPending) {
      // an on-demand encode: out of the slot, back after Retry-After; after
      // MAX_PENDING_MS the frames stay PENDING and a later request starts over
      job.pendingSince ??= this.now()
      if (this.now() - job.pendingSince <= MAX_PENDING_MS) this.requeue(job, e.afterMs)
      return
    }
    this.stats.errors++
    const msg = String((e as Error)?.message ?? e)
    if (isPermanent(e)) {
      this.failed.set(job.key, msg)
      this.onError?.(job.key, msg, true)
      return
    }
    const id = this.spanId(job.key, job.span)
    const n = (this.spanFails.get(id) ?? 0) + 1
    this.spanFails.set(id, n)
    this.stats.spanRetries++
    // Degraded now (not failed for good): the span keeps retrying with its
    // back-off, and one that lands clears the streak (put()).
    if (this.reportSpanFailures && n % SPAN_DEGRADE_AFTER === 0) {
      this.spanDegraded.add(job.key)
      this.onError?.(job.key, msg, false)
    }
    this.requeue(job, Math.min(SPAN_RETRY_MAX_MS, SPAN_RETRY_MS * 2 ** (n - 1)))
  }

  private async run(job: Job): Promise<void> {
    const ac = new AbortController()
    const timer = setTimeout(() => { this.stats.timeouts++; ac.abort() }, this.spanTimeoutMs)
    try {
      await this.runSpan(job, ac.signal)
    } finally {
      clearTimeout(timer)
    }
  }

  private async runSpan(job: Job, signal: AbortSignal): Promise<void> {
    const h = this.handles.get(job.key)!
    const url = this.url(job.key, `v/${pad4(job.span)}.bin`)
    if (job.frame !== null) {
      const got = await this.rangeRead(h, job.span, job.frame, url, signal)
      if (got) this.onLoad?.(job.key, job.frame, 1)
    }
    if (this.spans.get(this.spanId(job.key, job.span))?.samples) return
    this.stats.spanFetches++
    const buf = new Uint8Array(await (await this.getOk(url, undefined, signal)).arrayBuffer())
    let pack
    try {
      pack = parseSpanPack(buf)
    } catch (e) {
      throw new ProxyError(`${url}: ${(e as SpanPackError).message}`)
    }
    this.put(job.key, job.span, { bytes: buf.byteLength, first: pack.first, samples: pack.samples, partial: null })
    this.onLoad?.(job.key, pack.first, pack.samples.length)
  }

  /** Header, then one sample, by HTTP Range. A server that ignores Range
   *  (200) hands over the whole pack, which is kept as the full span. */
  private async rangeRead(h: ProxyHandle, span: number, frame: number, url: string, signal: AbortSignal): Promise<boolean> {
    this.stats.rangeReads++
    const headLen = 8 + 4 * h.spanFrames
    const r1 = await this.getOk(url, { Range: `bytes=0-${headLen - 1}` }, signal)
    const head = new Uint8Array(await r1.arrayBuffer())
    if (r1.status === 200) {
      const pack = parseSpanPack(head)
      this.put(h.key, span, { bytes: head.byteLength, first: pack.first, samples: pack.samples, partial: null })
      return true
    }
    if (head.byteLength < 8) return false
    const dv = new DataView(head.buffer, head.byteOffset, head.byteLength)
    const first = dv.getUint32(0)
    const count = dv.getUint32(4)
    const i = frame - first
    if (i < 0 || i >= count || head.byteLength < 8 + 4 * count) return false
    let off = 8 + 4 * count
    for (let j = 0; j < i; j++) off += dv.getUint32(8 + 4 * j)
    const size = dv.getUint32(8 + 4 * i)
    const r2 = await this.getOk(url, { Range: `bytes=${off}-${off + size - 1}` }, signal)
    const body = new Uint8Array(await r2.arrayBuffer())
    if (r2.status === 200) {
      const pack = parseSpanPack(body)
      this.put(h.key, span, { bytes: body.byteLength, first: pack.first, samples: pack.samples, partial: null })
      return true
    }
    if (body.byteLength !== size) return false
    const id = this.spanId(h.key, span)
    const e = this.spans.get(id)
    if (e?.samples) return true
    const partial = e?.partial ?? new Map<number, Uint8Array>()
    partial.set(frame, body)
    this.put(h.key, span, { bytes: (e?.bytes ?? 0) + size, first, samples: null, partial })
    return true
  }

  private put(key: string, span: number, entry: SpanEntry): void {
    const id = this.spanId(key, span)
    this.spanFails.delete(id)
    // A landed span undoes a span-streak report of its key (onRecovered).
    if (this.spanDegraded.delete(key) && !this.disposed) this.onRecovered?.(key)
    const old = this.spans.get(id)
    if (old) {
      this.bytes -= old.bytes
      this.spans.delete(id)
    }
    this.spans.set(id, entry)
    this.bytes += entry.bytes
    for (const [k, e] of this.spans) {
      if (this.bytes <= this.maxBytes || k === id) break
      this.spans.delete(k)
      this.bytes -= e.bytes
      this.stats.evictions++
    }
  }

  /** Cached span count (tests, telemetry). */
  get cachedSpans(): number {
    return this.spans.size
  }

  dispose(): void {
    this.disposed = true
    this.queue.length = 0
    this.spanFails.clear()
    this.spanDegraded.clear()
    this.spans.clear()
    this.bytes = 0
  }
}
