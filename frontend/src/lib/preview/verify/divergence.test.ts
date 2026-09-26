// The structural divergence check (spec §4.1 step 8, R14, §8.2) against
// fixture RLEs: the server side of every comparison is a `model.runs` that
// render/frame_map.py emitted for a golden case (tests/goldens/frame_map),
// served through a fake fetch with the real route's status codes.
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it, vi } from 'vitest'
import type { EdlLike } from '../timeline/framePlan'
import type { SourceInfoJson } from '../timeline/frameMap'
import { KIND_BLEND, buildProgramMap, lookupFromJson, type Run } from '../timeline/programMap'
import { MODE_BAKED, MODE_EXACT, classify } from '../timeline/support'
import {
  DivergenceChecker, mismatchRanges, sameRuns, type DivergenceEvent, type FetchLike, type FrameMapBody,
} from './divergence'

interface Case {
  name: string
  edl: EdlLike
  sources: Record<string, SourceInfoJson>
  model: { R: [number, number]; T: number | null; total: number; runs: Run[] }
}

const load = (group: string): Case[] => JSON.parse(readFileSync(fileURLToPath(
  new URL(`../../../../../tests/goldens/frame_map/${group}.json`, import.meta.url)), 'utf8')).cases
const byName = (group: string, name: string) => load(group).find((c) => c.name === name)!

const H = '0123456789abcdef'
const H2 = 'fedcba9876543210'

function body(c: Case, runs = c.model.runs, over: Partial<FrameMapBody> = {}): FrameMapBody {
  return { version: 1, render_hash: H, R: c.model.R, T: c.model.T, total: c.model.total, runs, sources: c.sources, ...over }
}

interface Reply { status: number; body?: unknown; headers?: Record<string, string> }

/** A fetch that answers a scripted sequence and records the URLs. */
function scripted(replies: Array<Reply | (() => Promise<Reply>) | Error>): FetchLike & { urls: string[] } {
  const urls: string[] = []
  const f = (async (url: string, init?: { signal?: AbortSignal }) => {
    urls.push(url)
    let next = replies.length > 1 ? replies.shift()! : replies[0]
    if (next instanceof Error) throw next
    if (typeof next === 'function') {
      const signal = init?.signal
      next = await Promise.race([next(), new Promise<never>((_, rej) => {
        signal?.addEventListener('abort', () => rej(new DOMException('aborted', 'AbortError')))
      })])
    }
    const r = next as Reply
    return {
      status: r.status, ok: r.status >= 200 && r.status < 300,
      headers: { get: (n: string) => r.headers?.[n] ?? r.headers?.[n.toLowerCase()] ?? null },
      json: async () => r.body,
    }
  }) as FetchLike & { urls: string[] }
  f.urls = urls
  return f
}

const noSleep = vi.fn(async () => {})

function setup(c: Case, replies: Parameters<typeof scripted>[0], extra: Record<string, unknown> = {}) {
  const events: DivergenceEvent[] = []
  const demotes: Array<[string, ReadonlyArray<readonly [number, number]>]> = []
  const warn = vi.fn()
  const fetch = scripted(replies)
  const checker = new DivergenceChecker({
    sessionId: 's_abc123', fetch, telemetry: (e) => events.push(e), warn, sleep: noSleep,
    onDemote: (h, r) => demotes.push([h, r]), ...extra,
  })
  const pm = buildProgramMap(c.edl, lookupFromJson(c.sources))
  return { checker, pm, events, demotes, warn, fetch }
}

/** A copy of the runs with run `i`'s first source frame moved by `d`. */
function shifted(runs: Run[], i: number, d = 1): Run[] {
  const out = structuredClone(runs)
  out[i].a = { ...out[i].a!, f0: out[i].a!.f0 + d }
  return out
}

describe('divergence: agreement', () => {
  // Frame-map parity over EVERY golden is programMap.test.ts's job; here the
  // structural and transition goldens (no retiming) are enough to prove the
  // checker says "match" whenever the two maps agree.
  it('golden model RLEs match the client map built from the same EDL', async () => {
    let checked = 0
    for (const group of ['structure', 'transitions']) {
      for (const c of load(group)) {
        const { checker, pm, events, demotes } = setup(c, [{ status: 200, body: body(c) }])
        const res = await checker.check(H, pm)
        expect(res, c.name).toEqual({ outcome: 'match', renderHash: H, demote: [] })
        expect(events[0].type).toBe('match')
        expect(demotes).toEqual([])
        expect(checker.demotedFor(H)).toEqual([])
        checked++
      }
    }
    expect(checked).toBeGreaterThan(30)
  })

  it('asks the frame_map route of its session for exactly the hash it checks', async () => {
    const c = byName('structure', 'gaps_p30')
    const { checker, pm, fetch } = setup(c, [{ status: 200, body: body(c) }], { sessionId: 's_a/b' })
    await checker.check(H, pm)
    expect(fetch.urls).toEqual([`/api/sessions/s_a%2Fb/frame_map?h=${H}`])
  })

  it('same frames under different clip ids (a split) is not a disagreement', async () => {
    const c = byName('structure', 'gaps_p30')
    const runs = structuredClone(c.model.runs).map((r) => (r.clip_id ? { ...r, clip_id: `${r.clip_id}_x` } : r))
    const { checker, pm } = setup(c, [{ status: 200, body: body(c, runs) }])
    expect(sameRuns(pm, runs)).toBe(false)
    expect(mismatchRanges(pm, runs)).toEqual([])
    expect((await checker.check(H, pm)).outcome).toBe('match')
  })
})

describe('divergence: disagreement demotes to BAKED', () => {
  it('a one-frame shift in one run demotes exactly that run and reports it', async () => {
    const c = byName('structure', 'gaps_p30')
    const i = c.model.runs.findIndex((r) => r.kind === 0 && r.n > 3)
    const run = c.model.runs[i]
    const { checker, pm, events, demotes, warn } = setup(c, [{ status: 200, body: body(c, shifted(c.model.runs, i)) }])
    const res = await checker.check(H, pm)
    expect(res.outcome).toBe('mismatch')
    expect(res.demote).toEqual([[run.k0, run.k0 + run.n]])
    expect(demotes).toEqual([[H, [[run.k0, run.k0 + run.n]]]])
    expect(checker.demotedFor(H)).toEqual([[run.k0, run.k0 + run.n]])
    const e = events[0] as Extract<DivergenceEvent, { type: 'mismatch' }>
    expect(e).toMatchObject({ type: 'mismatch', render_hash: H, first_k: run.k0, frames: run.n })
    expect(e.client).toEqual({ kind: 0, src: run.src, frame: run.a!.f0 })
    expect(e.server).toEqual({ kind: 0, src: run.src, frame: run.a!.f0 + 1 })
    expect(warn).toHaveBeenCalledTimes(1)

    // The demotion is the `demote` input of support.classify: BAKED there only.
    const s = classify(pm, c.edl, { phase: 1, demote: checker.demotedFor(H) })
    for (let k = 0; k < pm.total; k++) {
      const inside = k >= run.k0 && k < run.k0 + run.n
      expect(s.mode[k], `k=${k}`).toBe(inside ? MODE_BAKED : MODE_EXACT)
    }
    expect(s.ranges.find((r) => r.mode === MODE_BAKED)?.reasons).toEqual(['structure:mismatch'])
  })

  it('a different source at a frame is a mismatch', async () => {
    const c = byName('structure', 'gaps_p30')
    const runs = structuredClone(c.model.runs)
    const i = runs.findIndex((r) => r.kind === 0)
    runs[i].src = '/elsewhere.mp4'
    const { checker, pm } = setup(c, [{ status: 200, body: body(c, runs) }])
    expect((await checker.check(H, pm)).demote).toEqual([[runs[i].k0, runs[i].k0 + runs[i].n]])
  })

  it('a gap where the client has a clip (and the reverse) is a mismatch', async () => {
    const c = byName('structure', 'gaps_p30')
    const runs = structuredClone(c.model.runs)
    const gi = runs.findIndex((r) => r.kind === 1)
    const ci = runs.findIndex((r) => r.kind === 0)
    runs[gi] = { k0: runs[gi].k0, n: runs[gi].n, kind: 0, clip_id: 'c', src: runs[ci].src, a: { f0: 0, step: 1 } }
    const { checker, pm } = setup(c, [{ status: 200, body: body(c, runs) }])
    expect((await checker.check(H, pm)).demote).toEqual([[runs[gi].k0, runs[gi].k0 + runs[gi].n]])
  })

  it('a server map longer than the client map demotes the tail', async () => {
    const c = byName('structure', 'gaps_p30')
    const runs = [...structuredClone(c.model.runs), { k0: c.model.total, n: 5, kind: 1 }]
    const { checker, pm, events } = setup(c, [{ status: 200, body: body(c, runs, { total: c.model.total + 5 }) }])
    const res = await checker.check(H, pm)
    expect(res.demote).toEqual([[c.model.total, c.model.total + 5]])
    expect(events[0]).toMatchObject({ total: { client: c.model.total, server: c.model.total + 5 }, client: null })
  })

  it('blend progress or incoming frame disagreeing inside a seam demotes those frames', async () => {
    const c = byName('transitions', 'xfade_p30')
    const runs = structuredClone(c.model.runs)
    const i = runs.findIndex((r) => r.kind === KIND_BLEND)
    expect(i).toBeGreaterThan(-1)
    runs[i] = { ...runs[i], b: { ...runs[i].b!, f0: runs[i].b!.f0 + 2 } }
    const { checker, pm, events } = setup(c, [{ status: 200, body: body(c, runs) }])
    const res = await checker.check(H, pm)
    expect(res.demote).toEqual([[runs[i].k0, runs[i].k0 + runs[i].n]])
    const e = events[0] as Extract<DivergenceEvent, { type: 'mismatch' }>
    expect(e.server?.bFrame).toBe((e.client?.bFrame ?? 0) + 2)

    const runs2 = structuredClone(c.model.runs)
    runs2[i] = { ...runs2[i], p_j0: (runs2[i].p_j0 ?? 0) + 1 }
    const b = setup(c, [{ status: 200, body: body(c, runs2) }])
    expect((await b.checker.check(H, b.pm)).demote).toEqual([[runs2[i].k0, runs2[i].k0 + runs2[i].n]])
  })

  it('reports which sources the two sides described differently', async () => {
    const c = byName('structure', 'gaps_p30')
    const src = Object.keys(c.sources)[0]
    const client = structuredClone(c.sources)
    client[src] = { ...client[src], frames: client[src].frames - 1 }
    const { checker, pm, events } = setup(c, [{ status: 200, body: body(c, shifted(c.model.runs, 1)) }])
    await checker.check(H, pm, client)
    expect(events[0]).toMatchObject({ type: 'mismatch', sources_differ: [src] })
  })

  it('mismatchRanges groups consecutive frames into half-open ranges', () => {
    const c = byName('structure', 'gaps_p30')
    const pm = buildProgramMap(c.edl, lookupFromJson(c.sources))
    const clips = c.model.runs.map((r, i) => [r, i] as const).filter(([r]) => r.kind === 0 && r.n > 1)
    const [[r1, i1], [r2, i2]] = [clips[0], clips[2]]
    const runs = shifted(shifted(c.model.runs, i1), i2)
    expect(mismatchRanges(pm, runs)).toEqual([[r1.k0, r1.k0 + r1.n], [r2.k0, r2.k0 + r2.n]])
    expect(mismatchRanges(pm, c.model.runs)).toEqual([])
  })
})

describe('divergence: route answers', () => {
  const c = byName('structure', 'gaps_p30')

  it('409 stale: no demotion, the current hash is reported', async () => {
    const { checker, pm, events, demotes } = setup(c, [{
      status: 409, body: { error: { code: 'HTTP_409', details: { code: 'stale_render_hash', render_hash: H2 } } },
    }])
    const res = await checker.check(H, pm)
    expect(res).toEqual({ outcome: 'stale', renderHash: H, demote: [] })
    expect(events).toEqual([{ type: 'stale', render_hash: H, current: H2 }])
    expect(demotes).toEqual([])
    expect(checker.stats().checked).toBe(0)
  })

  it('a body for another hash is stale too', async () => {
    const { checker, pm } = setup(c, [{ status: 200, body: body(c, c.model.runs, { render_hash: H2 }) }])
    expect((await checker.check(H, pm)).outcome).toBe('stale')
  })

  it('202 is retried after Retry-After, then compared', async () => {
    const sleep = vi.fn(async () => {})
    const { checker, pm, fetch } = setup(c, [
      { status: 202, headers: { 'Retry-After': '0.2' }, body: { status: 'pending' } },
      { status: 202, body: { status: 'pending' } },
      { status: 200, body: body(c) },
    ], { sleep })
    expect((await checker.check(H, pm)).outcome).toBe('match')
    expect(fetch.urls).toHaveLength(3)
    expect(sleep.mock.calls.map((a) => (a as unknown[])[0])).toEqual([200, 200])
  })

  it('202 forever gives up as unavailable without demoting', async () => {
    const { checker, pm, fetch, demotes } = setup(c, [{ status: 202, body: {} }], { maxPendingRetries: 3 })
    const res = await checker.check(H, pm)
    expect(res.outcome).toBe('unavailable')
    expect(fetch.urls).toHaveLength(4)
    expect(demotes).toEqual([])
  })

  it('422 source_unavailable and a network error are reported, never thrown', async () => {
    const a = setup(c, [{ status: 422, body: { error: { details: { code: 'source_unavailable' } } } }])
    const ra = await a.checker.check(H, a.pm)
    expect(ra.outcome).toBe('unavailable')
    expect(a.events[0]).toEqual({ type: 'unavailable', render_hash: H, status: 422, reason: 'source_unavailable' })
    const b = setup(c, [new TypeError('Failed to fetch')])
    const rb = await b.checker.check(H, b.pm)
    expect(rb.outcome).toBe('error')
    expect(b.events[0]).toMatchObject({ type: 'error', message: 'Failed to fetch' })
    const m = setup(c, [{ status: 200, body: { nope: true } }])
    expect((await m.checker.check(H, m.pm)).outcome).toBe('error')
  })

  it('a throwing telemetry or demote sink does not break the check', async () => {
    const { checker, pm } = setup(c, [{ status: 200, body: body(c, shifted(c.model.runs, 1)) }], {
      telemetry: () => { throw new Error('sink down') }, onDemote: () => { throw new Error('nope') },
    })
    const res = await checker.check(H, pm)
    expect(res.outcome).toBe('mismatch')
    expect(checker.demotedFor(H).length).toBe(1)
  })
})

describe('divergence: never blocking, latest wins', () => {
  const c = byName('structure', 'gaps_p30')

  it('check() returns before the answer arrives; a newer check supersedes it', async () => {
    let release!: (r: Reply) => void
    const slow = () => new Promise<Reply>((res) => { release = res })
    const { checker, pm, events, demotes } = setup(c, [slow, { status: 200, body: body(c, c.model.runs, { render_hash: H2 }) }])
    let settled = false
    const first = checker.check(H, pm).then((r) => { settled = true; return r })
    await Promise.resolve()
    expect(settled).toBe(false)                         // nothing waits on the network
    const second = checker.check(H2, pm)
    release({ status: 200, body: body(c, shifted(c.model.runs, 1)) })
    expect(await first).toEqual({ outcome: 'superseded', renderHash: H, demote: [] })
    expect((await second).outcome).toBe('match')
    expect(demotes).toEqual([])                          // the stale mismatch was never applied
    expect(events.map((e) => e.type)).toEqual(['match'])
  })

  it('cancel() drops a check in flight', async () => {
    const slow = () => new Promise<Reply>(() => {})
    const { checker, pm } = setup(c, [slow])
    const p = checker.check(H, pm)
    checker.cancel()
    expect((await p).outcome).toBe('superseded')
  })

  it('the comparison of a 300-clip, 12-minute map stays far under a frame', async () => {
    // One source, 300 cuts of 72 frames each (21,600 frames at 30 fps).
    const src = '/s.mp4'
    const sources = { [src]: { rate: [30, 1], tb: [1, 15360], frames: 30000, start_ticks: 0, w: 1280, h: 720 } as SourceInfoJson }
    const clips = Array.from({ length: 300 }, (_, i) => ({
      id: `c${i}`, src, start: i * 2.4, in: (i * 37) % 900 / 30, out: (i * 37) % 900 / 30 + 2.4, speed: 1,
    }))
    const edl = { canvas: { w: 1280, h: 720, fps: 30 }, tracks: [{ id: 'v1', type: 'video', clips }] } as unknown as EdlLike
    const pm = buildProgramMap(edl, lookupFromJson(sources))
    expect(pm.total).toBe(21600)
    const { toRle } = await import('../timeline/programMap')
    const runs = toRle(pm)
    const fake: Case = { name: 'big', edl, sources, model: { R: [30, 1], T: 8000, total: pm.total, runs } }
    const { checker, events } = setup(fake, [{ status: 200, body: body(fake) }])
    const t0 = performance.now()
    expect((await checker.check(H, pm)).outcome).toBe('match')
    const ms = performance.now() - t0
    console.info(`divergence compare, 300 clips / 21600 frames: ${(events[0] as { ms: number }).ms.toFixed(2)} ms`)
    expect((events[0] as { ms: number }).ms).toBeLessThan(50)   // §8.2 target < 5 ms (reported)
    expect(ms).toBeLessThan(200)
  })
})

describe('divergence: the §7 engine fallback rate', () => {
  const c = byName('structure', 'gaps_p30')

  async function run(outcomes: Array<'match' | 'mismatch'>) {
    const replies = outcomes.map((o) => ({ status: 200, body: body(c, o === 'match' ? c.model.runs : shifted(c.model.runs, 1)) }))
    const { checker, pm } = setup(c, replies)
    for (let i = 0; i < outcomes.length; i++) await checker.check(H, pm)
    return checker
  }

  it('falls back only above 5% of edits, with enough evidence', async () => {
    const two = await run([...Array(18).fill('match'), 'mismatch', 'mismatch'])
    expect(two.stats()).toEqual({ checked: 20, mismatched: 2, rate: 0.1 })
    expect(two.shouldFallback()).toBe(true)
    expect((await run([...Array(19).fill('match'), 'mismatch'])).shouldFallback()).toBe(false)
    expect((await run(['mismatch', 'mismatch', 'match'])).shouldFallback()).toBe(false)
    expect((await run([...Array(38).fill('match'), 'mismatch', 'mismatch'])).shouldFallback()).toBe(false)
  })
})
