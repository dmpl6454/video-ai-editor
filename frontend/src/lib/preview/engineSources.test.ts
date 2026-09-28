// EngineSources (spec §3.5 visibility, §7): while the page is hidden laneA,
// the proxy store and the bake store are suspended, prefetch asks nothing,
// and a proxy reopen that comes due waits; shown, everything resumes.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { EngineSources, PROXY_REOPEN_MS, type SourcesHost } from './engineSources'
import { ProxyStore } from './media/proxyIndex'
import type { LaneA } from './media/laneA'
import type { ProgramFeed } from './engineFeed'

function rig() {
  const log: string[] = []
  const lane = { suspend: (on: boolean) => log.push(`lane:${on}`), poke: () => undefined, lookAheadFrames: 900 } as unknown as LaneA
  const bake = new ProxyStore({ baseUrl: '/bake', fetch: async () => { throw new Error('no') } })
  let visibility: DocumentVisibilityState = 'visible'
  vi.stubGlobal('document', { get visibilityState() { return visibility } })
  const host: SourcesHost = {
    lane: () => lane,
    hasProgram: () => true,
    destroyed: () => false,
    playhead: () => 50,
    rate: () => ({ num: 30, den: 1 }),
    support: () => null,
    bakeStore: () => bake,
    failed: () => log.push('failed'),
    recovered: () => log.push('recovered'),
    shown: () => log.push('shown'),
  }
  const fails = { open: 0 }
  const src = new EngineSources({ fetch: async () => {
    fails.open++
    return { status: 500, ok: false, headers: { get: () => null }, arrayBuffer: async () => new ArrayBuffer(0), json: async () => ({}) }
  } }, host)
  const feed = {
    prefetch: vi.fn(), prefetchBake: vi.fn(), markFailed: (k: string) => log.push(`markFailed:${k}`),
    openProxies: vi.fn(),
  }
  src.feed = feed as unknown as ProgramFeed
  return { src, log, feed, bake, setVisibility: (v: DocumentVisibilityState) => { visibility = v }, fails }
}

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

describe('EngineSources', () => {
  it('hidden: laneA, the proxy store and the bake store are suspended; prefetch asks nothing', () => {
    const r = rig()
    r.src.suspend(true)
    expect(r.log).toEqual(['lane:true'])
    expect(r.src.store.isSuspended).toBe(true)
    expect(r.bake.isSuspended).toBe(true)
    r.src.prefetch()
    expect(r.feed.prefetch).not.toHaveBeenCalled()
    r.src.suspend(false)
    expect(r.src.store.isSuspended).toBe(false)
    expect(r.bake.isSuspended).toBe(false)
    expect(r.feed.prefetch).toHaveBeenCalledTimes(1)
    expect(r.log).toEqual(['lane:true', 'lane:false', 'shown'])
  })

  it('a page reported hidden without a visibility event still prefetches nothing, until the event brings it back', () => {
    const r = rig()
    r.setVisibility('hidden')
    r.src.prefetch()
    expect(r.feed.prefetch).not.toHaveBeenCalled()
    r.setVisibility('visible')
    r.src.suspend(true)          // the queued 'hidden' event lands late
    r.src.prefetch()
    expect(r.feed.prefetch).not.toHaveBeenCalled()
    r.src.suspend(false)         // 'visible': resumes and prefetches
    expect(r.feed.prefetch).toHaveBeenCalledTimes(1)
    r.src.prefetch()
    expect(r.feed.prefetch).toHaveBeenCalledTimes(2)
  })

  it('hiddenNow(): a page reported hidden before its event suspends laneA and both stores at once; the events resume them', () => {
    const r = rig()
    expect(r.src.hiddenNow()).toBe(false)
    expect(r.log).toEqual([])
    r.setVisibility('hidden')
    expect(r.src.hiddenNow()).toBe(true)
    expect(r.log).toEqual(['lane:true'])
    expect(r.src.store.isSuspended).toBe(true)
    expect(r.bake.isSuspended).toBe(true)
    // the queued 'hidden' event, then WebKit flips back before 'visible'
    r.src.suspend(true)
    r.setVisibility('visible')
    expect(r.src.hiddenNow()).toBe(true)          // latched until the event says visible
    r.src.suspend(false)
    expect(r.src.hiddenNow()).toBe(false)
    expect(r.src.store.isSuspended).toBe(false)
    expect(r.bake.isSuspended).toBe(false)
    expect(r.log).toEqual(['lane:true', 'lane:false', 'shown'])
  })

  it('resumeIfVisible(): hidden seen before the engine listened, visible since, resumes; still hidden stays', () => {
    const r = rig()
    r.setVisibility('hidden')
    r.src.hiddenNow()
    r.src.resumeIfVisible()
    expect(r.src.isSuspended).toBe(true)
    r.setVisibility('visible')
    r.src.resumeIfVisible()
    expect(r.src.isSuspended).toBe(false)
    expect(r.log).toEqual(['lane:true', 'lane:false', 'shown'])
  })

  it('the proxy store asks hiddenNow() before it starts a fetch', () => {
    const r = rig()
    r.setVisibility('hidden')
    r.src.store.request('K', 0)
    expect(r.src.isSuspended).toBe(true)
    expect(r.log).toEqual(['lane:true'])
  })

  it('a transient open failure reopens after 10 s — deferred to the page coming back when hidden', async () => {
    const r = rig()
    // the store reports a transient failure (its 5 retries exhausted)
    ;(r.src.store as unknown as { onError: (k: string, m: string, p: boolean) => void }).onError('K', 'HTTP 500', false)
    expect(r.log).toEqual(['markFailed:K', 'failed'])
    r.src.suspend(true)
    vi.advanceTimersByTime(PROXY_REOPEN_MS + 1)
    expect(r.feed.openProxies).not.toHaveBeenCalled()
    r.src.suspend(false)
    expect(r.feed.openProxies).toHaveBeenCalledTimes(1)
  })

  it('a span landing after a span-streak report re-opens at once, not 10 s later (Final QA r2)', () => {
    const r = rig()
    const store = r.src.store as unknown as {
      onError: (k: string, m: string, p: boolean) => void; onRecovered: (k: string) => void }
    store.onError('K', 'HTTP 500', false)        // 5 span failures in a row: degraded
    expect(r.log).toEqual(['markFailed:K', 'failed'])
    store.onRecovered('K')                       // …then one of its spans landed
    expect(r.feed.openProxies).toHaveBeenCalledTimes(1)
    // while hidden, the reopen waits for the page to come back
    const h = rig()
    const hs = h.src.store as unknown as { onError: (k: string, m: string, p: boolean) => void; onRecovered: (k: string) => void }
    hs.onError('K', 'HTTP 500', false)
    h.src.suspend(true)
    hs.onRecovered('K')
    expect(h.feed.openProxies).not.toHaveBeenCalled()
    h.src.suspend(false)
    expect(h.feed.openProxies).toHaveBeenCalledTimes(1)
  })

  it('a permanent failure (410) is never reopened', () => {
    const r = rig()
    ;(r.src.store as unknown as { onError: (k: string, m: string, p: boolean) => void }).onError('K', '410', true)
    vi.advanceTimersByTime(PROXY_REOPEN_MS * 3)
    expect(r.feed.openProxies).not.toHaveBeenCalled()
  })
})
