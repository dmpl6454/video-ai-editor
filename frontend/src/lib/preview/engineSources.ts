// The engine's PROXY I/O (INSTANT_PREVIEW_SPEC §3.5 visibility, §5.1-5.2,
// §7 degraded tier): the span store, opening the program's proxies, a
// transient open failure reopened after PROXY_REOPEN_MS, prefetch around the
// playhead, and SUSPENSION while the page is hidden.
//
// Hidden (§3.5: "the engine pauses decode, appends and proxy prefetch"):
// laneA starts no append or remove, neither the proxy store nor the bake
// store starts a span fetch (queued requests wait; ones already on the wire
// finish), no prefetch is ranked, and a reopen that comes due is deferred.
// Shown: everything resumes where it stopped and the paused frame is shown
// (a seek made while hidden completes then).

import type { EngineOptions } from './engineOptions'
import type { ProgramFeed } from './engineFeed'
import type { LaneA } from './media/laneA'
import { ProxyStore } from './media/proxyIndex'
import type { Support } from './timeline/support'
import type { Rational } from './timeline/timebase'

/** A proxy that failed to open for a transient reason is tried again after this. */
export const PROXY_REOPEN_MS = 10_000

export interface SourcesHost {
  lane(): LaneA | null
  hasProgram(): boolean
  destroyed(): boolean
  /** The frame the window centres on (presented while playing, else the target). */
  playhead(): number
  rate(): Rational
  support(): Support | null
  bakeStore(): ProxyStore | null
  /** A proxy failed (degraded now, §7): reclassify and emit. */
  failed(): void
  /** A proxy that had failed opened: reclassify, re-feed laneA, re-show. */
  recovered(): void
  /** The page is visible again (after resume): re-show the paused frame. */
  shown(): void
}

const pageHidden = () => typeof document !== 'undefined' && document.visibilityState === 'hidden'

export class EngineSources {
  readonly store: ProxyStore
  /** Set once by the engine (the feed is built over `store`). */
  feed!: ProgramFeed
  private readonly host: SourcesHost
  private reopenTimer: ReturnType<typeof setTimeout> | null = null
  private reopenDue = false
  private suspended = false

  constructor(opts: EngineOptions, host: SourcesHost) {
    this.host = host
    this.store = new ProxyStore({
      baseUrl: opts.proxyBaseUrl ?? '/api/proxies',
      fetch: opts.fetch,
      maxBytes: opts.spanCacheBytes,
      onLoad: () => this.host.lane()?.poke(),
      onError: (key, _msg, permanent) => {
        // degraded (§7) now; a transient failure is opened again later, and
        // a proxy that opens then is readable again (feed.openProxies)
        this.feed.markFailed(key)
        this.host.failed()
        if (!permanent) this.scheduleReopen()
      },
    })
  }

  get isSuspended(): boolean {
    return this.suspended
  }

  /** Open the program's proxies; one that failed transiently and opens now
   *  is readable again. */
  open(): void {
    this.feed.openProxies(() => this.host.lane()?.poke(), () => {
      if (!this.host.destroyed() && this.host.hasProgram()) this.host.recovered()
    })
  }

  private scheduleReopen(): void {
    if (this.reopenTimer || this.host.destroyed()) return
    this.reopenTimer = setTimeout(() => {
      this.reopenTimer = null
      if (this.host.destroyed() || !this.host.hasProgram()) return
      if (this.suspended || pageHidden()) this.reopenDue = true
      else this.open()
    }, PROXY_REOPEN_MS)
  }

  /** Ask for every span the laneA window needs, nearest first (nothing
   *  while the page is hidden). */
  prefetch(): void {
    const lane = this.host.lane()
    if (!lane || this.suspended || pageHidden()) return
    const P = this.host.playhead()
    const R = this.host.rate()
    const back = Math.round((10 * R.num) / R.den)
    this.feed.prefetch(P, back, lane.lookAheadFrames)
    this.feed.prefetchBake(this.host.support(), P, back, lane.lookAheadFrames)
  }

  /** Page hidden (true) or shown (false). */
  suspend(on: boolean): void {
    if (this.suspended === on) return
    this.suspended = on
    this.host.lane()?.suspend(on)
    this.store.suspend(on)
    this.host.bakeStore()?.suspend(on)
    if (on) return
    if (this.reopenDue) {
      this.reopenDue = false
      if (this.host.hasProgram()) this.open()
    }
    this.prefetch()
    this.host.shown()
  }

  destroy(): void {
    if (this.reopenTimer) clearTimeout(this.reopenTimer)
    this.reopenTimer = null
    this.store.dispose()
  }
}
