// Structural divergence check (instant preview spec §4.1 step 8, §6 R14, §8.2).
//
// After every committed edit the engine has built its own program map from
// the EDL `/dispatch?include=edl` returned. The server's reference map of
// the same `render_hash` (render/frame_map.py, pinned to real ffmpeg by the
// goldens) is fetched from `GET /api/sessions/{sid}/frame_map?h=` and
// compared with it. Any frame where the two disagree is DEMOTED: its range
// is handed to `support.classify` as `demote`, which marks it BAKED, so the
// server's own frames (bake spans) are shown there instead of a frame the
// export would never contain. A mismatch also emits telemetry
// `{render_hash, first_k, client, server}` and, in development, a console
// warning.
//
// Never blocking: `check()` returns a promise nobody has to await; the
// display never waits on it. Latest wins: a newer check aborts the older
// one's fetch and its result is dropped. It never throws.

import type { EdlClip } from '../timeline/framePlan'
import type { SourceInfoJson } from '../timeline/frameMap'
import { KIND_BLEND, KIND_GAP, compareWithRle, rleFrames, toRle, type ProgramMap, type Run } from '../timeline/programMap'

/** The `GET /frame_map` body (render/frame_map.frame_map_json + `sources`). */
export interface FrameMapBody {
  version: number
  render_hash: string
  R: [number, number]
  T: number | null
  total: number
  runs: Run[]
  seams?: unknown[]
  audio?: unknown[]
  audio_total?: number
  sources?: Record<string, SourceInfoJson>
}

export type Range = readonly [number, number]

/** The subset of `fetch` the checker uses (injectable for tests / pages). */
export type FetchLike = (url: string, init?: { signal?: AbortSignal; headers?: Record<string, string> }) => Promise<{
  status: number
  ok: boolean
  headers: { get(name: string): string | null }
  json(): Promise<unknown>
}>

export interface FrameEntry {
  kind: number
  src: string | null
  frame: number
  bSrc?: string | null
  bFrame?: number
  p?: [number, number]
}

export type DivergenceEvent =
  | { type: 'match'; render_hash: string; runs: number; total: number; ms: number }
  | {
    type: 'mismatch'; render_hash: string; first_k: number; frames: number; ranges: Range[]
    client: FrameEntry | null; server: FrameEntry | null; total: { client: number; server: number }
    sources_differ: string[]; ms: number
  }
  | { type: 'stale'; render_hash: string; current: string | null }
  | { type: 'unavailable'; render_hash: string; status: number; reason: string }
  | { type: 'error'; render_hash: string; message: string }

export type CheckOutcome = DivergenceEvent['type'] | 'superseded'

export interface CheckResult {
  outcome: CheckOutcome
  renderHash: string
  /** Output ranges demoted to BAKED by this check (empty unless mismatch). */
  demote: Range[]
}

export interface DivergenceOptions {
  /** `/api/sessions/{sid}/frame_map?h=…` by default. */
  url?: (renderHash: string) => string
  sessionId?: string
  fetch?: FetchLike
  /** Called with the demoted ranges of a hash (feed them to support.classify). */
  onDemote?: (renderHash: string, ranges: Range[]) => void
  /** Telemetry sink (spec §15.4: a local log; the engine decides). */
  telemetry?: (e: DivergenceEvent) => void
  /** Development warning (default: console.warn when running under Vite dev). */
  warn?: ((msg: string, detail?: unknown) => void) | null
  /** The source identity both maps key frames by (default: the clip's src). */
  srcKey?: (c: EdlClip) => string
  /** How often a 202 (session busy / sources probing) is retried. */
  maxPendingRetries?: number
  /** Retry delay when the answer carries no Retry-After (ms). */
  retryMs?: number
  sleep?: (ms: number, signal: AbortSignal) => Promise<void>
  /** §7 engine fallback: mismatch rate above which the engine should fall
   *  back to server mode, and the minimum evidence before it may. */
  fallbackRate?: number
  fallbackMinChecks?: number
  fallbackMinMismatches?: number
}

// ---------------------------------------------------------------- pure parts

/** Half-open output ranges where `pm` disagrees with the server's RLE
 *  (R14): every frame whose source, source frame, blend side or progress
 *  differs, and every frame only one of the two maps has. */
export function mismatchRanges(pm: ProgramMap, runs: Run[], srcKey?: (c: EdlClip) => string): Range[] {
  const bad = compareWithRle(pm, runs, Number.POSITIVE_INFINITY, srcKey)
  const out: Array<[number, number]> = []
  for (const k of bad) {
    const last = out[out.length - 1]
    if (last && last[1] === k) last[1] = k + 1
    else out.push([k, k + 1])
  }
  return out
}

function runEqual(a: Run, b: Run): boolean {
  const keys = new Set([...Object.keys(a), ...Object.keys(b)])
  for (const key of keys) {
    const x = (a as unknown as Record<string, unknown>)[key]
    const y = (b as unknown as Record<string, unknown>)[key]
    if (x === y) continue
    if (x && y && typeof x === 'object' && typeof y === 'object') {
      const sx = x as Record<string, unknown>, sy = y as Record<string, unknown>
      const ks = new Set([...Object.keys(sx), ...Object.keys(sy)])
      for (const k of ks) if (sx[k] !== sy[k]) return false
      continue
    }
    if (x === undefined || y === undefined) {
      // `nested: false` and an absent `nested` say the same thing.
      if ((x ?? false) === false && (y ?? false) === false) continue
    }
    return false
  }
  return true
}

/** O(runs) fast path: identical RLE ⇒ identical frames. */
export function sameRuns(pm: ProgramMap, runs: Run[], srcKey?: (c: EdlClip) => string): boolean {
  const mine = toRle(pm, srcKey)
  if (mine.length !== runs.length) return false
  for (let i = 0; i < mine.length; i++) if (!runEqual(mine[i], runs[i])) return false
  return true
}

function clientEntry(pm: ProgramMap, k: number, srcKey: (c: EdlClip) => string): FrameEntry | null {
  if (k < 0 || k >= pm.total) return null
  const kind = pm.kind[k]
  if (kind === KIND_GAP) return { kind, src: null, frame: -1 }
  const e: FrameEntry = { kind, src: srcKey(pm.clips[pm.clip[k]]), frame: pm.srcFrame[k] }
  if (kind === KIND_BLEND) {
    e.bSrc = srcKey(pm.clips[pm.bClip[k]])
    e.bFrame = pm.bSrcFrame[k]
    e.p = [pm.pNum[k], pm.pDen[k]]
  }
  return e
}

function serverEntry(runs: Run[], k: number): FrameEntry | null {
  for (const run of runs) {
    if (k < run.k0 || k >= run.k0 + run.n) continue
    const f = rleFrames([{ ...run, k0: 0 }])[k - run.k0]
    const e: FrameEntry = { kind: f.kind, src: f.src, frame: f.frame }
    if (f.bFrame !== undefined) Object.assign(e, { bSrc: f.bSrc ?? null, bFrame: f.bFrame, p: f.p })
    return e
  }
  return null
}

function sourcesDiffer(client: Record<string, SourceInfoJson> | undefined,
  server: Record<string, SourceInfoJson> | undefined): string[] {
  if (!client || !server) return []
  const out: string[] = []
  for (const [src, s] of Object.entries(server)) {
    const c = client[src]
    if (!c) { out.push(src); continue }
    if (c.frames !== s.frames || c.rate[0] * s.rate[1] !== s.rate[0] * c.rate[1]
      || c.tb[0] * s.tb[1] !== s.tb[0] * c.tb[1] || (c.start_ticks ?? 0) !== (s.start_ticks ?? 0)) out.push(src)
  }
  return out
}

const defaultSleep = (ms: number, signal: AbortSignal) => new Promise<void>((resolve, reject) => {
  if (signal.aborted) { reject(new DOMException('aborted', 'AbortError')); return }
  const t = setTimeout(() => { signal.removeEventListener('abort', onAbort); resolve() }, ms)
  const onAbort = () => { clearTimeout(t); reject(new DOMException('aborted', 'AbortError')) }
  signal.addEventListener('abort', onAbort, { once: true })
})

function devWarn(msg: string, detail?: unknown): void {
  const env = (import.meta as unknown as { env?: { DEV?: boolean } }).env
  if (env?.DEV) console.warn(msg, detail)
}

const isAbort = (e: unknown) => !!e && typeof e === 'object' && (e as { name?: string }).name === 'AbortError'

// ---------------------------------------------------------------- the checker

const KEEP_HASHES = 16
const RECENT_EVENTS = 64

export class DivergenceChecker {
  private readonly opts: Required<Pick<DivergenceOptions, 'maxPendingRetries' | 'retryMs' | 'fallbackRate'
    | 'fallbackMinChecks' | 'fallbackMinMismatches'>> & DivergenceOptions
  private controller: AbortController | null = null
  private gen = 0
  private demoted = new Map<string, Range[]>()
  private recentEvents: DivergenceEvent[] = []
  private counts = { checked: 0, mismatched: 0 }

  constructor(opts: DivergenceOptions = {}) {
    this.opts = {
      maxPendingRetries: 10, retryMs: 200, fallbackRate: 0.05, fallbackMinChecks: 20,
      fallbackMinMismatches: 2, ...opts,
    }
  }

  /** Compare `pm` (the client map of `renderHash`) with the server's.
   *  Resolves once the outcome is known; never rejects. A newer call
   *  supersedes this one (its outcome is then `superseded`). */
  async check(renderHash: string, pm: ProgramMap,
    clientSources?: Record<string, SourceInfoJson>): Promise<CheckResult> {
    this.controller?.abort()
    const controller = new AbortController()
    this.controller = controller
    const gen = ++this.gen
    const superseded = (): CheckResult => ({ outcome: 'superseded', renderHash, demote: [] })
    try {
      const got = await this.fetchMap(renderHash, controller.signal)
      if (gen !== this.gen) return superseded()
      if ('event' in got) return this.finish(got.event, [])
      return this.compare(renderHash, pm, got.body, clientSources)
    } catch (e) {
      if (gen !== this.gen || isAbort(e)) return superseded()
      return this.finish({ type: 'error', render_hash: renderHash, message: String((e as Error)?.message ?? e) }, [])
    } finally {
      if (this.controller === controller) this.controller = null
    }
  }

  /** The `demote` input of `support.classify` for this hash. */
  demotedFor(renderHash: string): readonly Range[] {
    return this.demoted.get(renderHash) ?? []
  }

  /** Checks that reached a verdict, and how many disagreed (§7). */
  stats(): { checked: number; mismatched: number; rate: number } {
    const { checked, mismatched } = this.counts
    return { checked, mismatched, rate: checked ? mismatched / checked : 0 }
  }

  /** §7: fall back to server mode when more than 5% of the session's edits
   *  disagreed (with a minimum of evidence, so one early outlier does not). */
  shouldFallback(): boolean {
    const s = this.stats()
    return s.checked >= this.opts.fallbackMinChecks && s.mismatched >= this.opts.fallbackMinMismatches
      && s.rate > this.opts.fallbackRate
  }

  recent(): readonly DivergenceEvent[] {
    return this.recentEvents
  }

  /** Abort any check in flight (engine dispose / session switch). */
  cancel(): void {
    this.gen++
    this.controller?.abort()
    this.controller = null
  }

  // ---- internals ----

  private url(renderHash: string): string {
    if (this.opts.url) return this.opts.url(renderHash)
    const sid = encodeURIComponent(this.opts.sessionId ?? '')
    return `/api/sessions/${sid}/frame_map?h=${encodeURIComponent(renderHash)}`
  }

  private async fetchMap(renderHash: string, signal: AbortSignal):
    Promise<{ body: FrameMapBody } | { event: DivergenceEvent }> {
    const f: FetchLike = this.opts.fetch ?? ((u, i) => fetch(u, i))
    const sleep = this.opts.sleep ?? defaultSleep
    for (let attempt = 0; ; attempt++) {
      const r = await f(this.url(renderHash), { signal, headers: { Accept: 'application/json' } })
      if (r.status === 202) {
        if (attempt >= this.opts.maxPendingRetries) {
          return { event: { type: 'unavailable', render_hash: renderHash, status: 202, reason: 'pending' } }
        }
        const ra = Number.parseFloat(r.headers.get('Retry-After') ?? '')
        await sleep(Number.isFinite(ra) && ra >= 0 ? Math.min(5000, ra * 1000) : this.opts.retryMs, signal)
        continue
      }
      if (r.status === 409) {
        const body = await r.json().catch(() => null) as { error?: { details?: { render_hash?: string } } } | null
        return { event: { type: 'stale', render_hash: renderHash, current: body?.error?.details?.render_hash ?? null } }
      }
      if (!r.ok) {
        const body = await r.json().catch(() => null) as { error?: { details?: { code?: string } } } | null
        return {
          event: {
            type: 'unavailable', render_hash: renderHash, status: r.status,
            reason: body?.error?.details?.code ?? `http_${r.status}`,
          },
        }
      }
      const body = await r.json() as FrameMapBody
      if (!body || !Array.isArray(body.runs) || typeof body.total !== 'number') {
        return { event: { type: 'error', render_hash: renderHash, message: 'malformed frame_map body' } }
      }
      if (body.render_hash !== renderHash) {
        return { event: { type: 'stale', render_hash: renderHash, current: body.render_hash ?? null } }
      }
      return { body }
    }
  }

  private compare(renderHash: string, pm: ProgramMap, body: FrameMapBody,
    clientSources?: Record<string, SourceInfoJson>): CheckResult {
    const t0 = performance.now()
    const srcKey = this.opts.srcKey ?? ((c: EdlClip) => c.src)
    this.counts.checked++
    if (body.total === pm.total && sameRuns(pm, body.runs, srcKey)) {
      return this.finish({
        type: 'match', render_hash: renderHash, runs: body.runs.length, total: pm.total,
        ms: performance.now() - t0,
      }, [])
    }
    const ranges = mismatchRanges(pm, body.runs, srcKey)
    if (!ranges.length) {
      // Same frames, different run boundaries (clip ids differ, say): not a
      // structural disagreement.
      return this.finish({
        type: 'match', render_hash: renderHash, runs: body.runs.length, total: pm.total,
        ms: performance.now() - t0,
      }, [])
    }
    this.counts.mismatched++
    const first = ranges[0][0]
    const frames = ranges.reduce((n, [a, b]) => n + (b - a), 0)
    const event: DivergenceEvent = {
      type: 'mismatch', render_hash: renderHash, first_k: first, frames, ranges,
      client: clientEntry(pm, first, srcKey), server: serverEntry(body.runs, first),
      total: { client: pm.total, server: body.total },
      sources_differ: sourcesDiffer(clientSources, body.sources), ms: performance.now() - t0,
    }
    this.demoted.set(renderHash, ranges)
    while (this.demoted.size > KEEP_HASHES) this.demoted.delete(this.demoted.keys().next().value as string)
    try { this.opts.onDemote?.(renderHash, ranges) } catch { /* a sink must not break the check */ }
    const warn = this.opts.warn === undefined ? devWarn : this.opts.warn
    warn?.(`[preview] frame map disagrees with the server at k=${first} (${frames} frames demoted to BAKED)`, event)
    return this.finish(event, ranges)
  }

  private finish(event: DivergenceEvent, demote: Range[]): CheckResult {
    this.recentEvents.push(event)
    if (this.recentEvents.length > RECENT_EVENTS) this.recentEvents.shift()
    try { this.opts.telemetry?.(event) } catch { /* never let telemetry break the engine */ }
    return { outcome: event.type, renderHash: event.render_hash, demote }
  }
}
