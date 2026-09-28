// The bake splice (INSTANT_PREVIEW_SPEC §4.1 step 8, §5.3, R13, R14): when
// the preview render of the CURRENT program lands, its BAKED ranges take the
// server's composited frames (bake spans), appended over the RAW client
// frames in the same SourceBuffer as they arrive (no <video> src swap). The
// structural check's demotions (R14) are kept per render hash.

import type { EngineOptions } from './engineOptions'
import { ProgramFeed } from './engineFeed'
import { ProxyStore } from './media/proxyIndex'
import { MODE_BAKED, type Support } from './timeline/support'

type Ranges = ReadonlyArray<readonly [number, number]>

/** At most this many render hashes keep their demotions. */
const DEMOTED_KEPT = 16

/** What the splice needs from the engine. */
export interface BakeHost {
  readonly feed: ProgramFeed
  renderHash(): string
  hasProgram(): boolean
  support(): Support | null
  /** Not destroyed, not in server mode. */
  usable(): boolean
  /** Reclassify (§7) without emitting. */
  reclassify(): void
  /** Re-point BAKED frames at landed bake samples, hand laneA the result,
   *  prefetch, and re-show the paused frame. */
  refreshWant(): void
  prefetch(): void
  emitStatus(): void
  /** The page is hidden now, event or not (EngineSources.hiddenNow). */
  hiddenNow?(): boolean
}

export class BakeSplice {
  /** Bake spans of the current render hash (null: no `bakeBaseUrl`). */
  readonly store: ProxyStore | null
  /** `ranges=` of each bake's index.json (the BAKED ranges it must encode). */
  private readonly rangesText = new Map<string, string>()
  private readonly demoted = new Map<string, Ranges>()
  private readonly host: BakeHost

  constructor(host: BakeHost, opts: EngineOptions) {
    this.host = host
    this.store = opts.bakeBaseUrl ? this.makeStore(opts.bakeBaseUrl, opts) : null
  }

  private makeStore(baseUrl: string, opts: EngineOptions): ProxyStore {
    const f = opts.fetch ?? ((url: string, init?: Parameters<NonNullable<EngineOptions['fetch']>>[1]) => fetch(url, init))
    return new ProxyStore({
      baseUrl,
      // index.json?ranges=k0-k1,… queues exactly the spans the BAKED ranges
      // touch (§5.2); every other bake request is the proxy shape
      fetch: (url, init) => {
        const m = /\/([0-9a-f]+)\/index\.json$/.exec(url)
        const ranges = m ? this.rangesText.get(m[1]) : undefined
        return f(ranges ? `${url}?ranges=${ranges}` : url, init)
      },
      hiddenNow: () => this.host.hiddenNow?.() ?? false,
      // a bake span that keeps failing retries by itself; onError here would
      // drop the whole bake (proxyIndex SPAN_DEGRADE_AFTER is for sources)
      reportSpanFailures: false,
      onLoad: () => this.onLoaded(),
      onError: (key) => {
        if (this.host.feed.bakeKey === key) this.host.feed.setBake(null)
        this.host.emitStatus()
      },
    })
  }

  /** The render of `renderHash` landed: start splicing its bake into the
   *  BAKED ranges. A hash that is not the current program's is ignored.
   *  Returns whether a splice is running. */
  splice(renderHash: string): boolean {
    const h = this.host
    const bs = this.store
    if (!bs || !h.usable() || !h.hasProgram() || renderHash !== h.renderHash()) return false
    const ranges = ProgramFeed.bakedRanges(h.support())
    if (!ranges.length) return false
    if (h.feed.bakeKey === renderHash) return true
    this.rangesText.set(renderHash, ranges.map(([a, b]) => `${a}-${b}`).join(','))
    h.feed.setBake(renderHash)
    void bs.open(renderHash).then(() => {
      if (h.feed.bakeKey !== renderHash) return
      h.prefetch()
      this.onLoaded()
    }, () => undefined)
    h.emitStatus()
    return true
  }

  /** Output ranges the structural check (R14) found in disagreement for
   *  `renderHash`: they become BAKED (and take the bake when it lands). */
  setDemoted(renderHash: string, ranges: Ranges): void {
    const h = this.host
    this.demoted.set(renderHash, ranges)
    while (this.demoted.size > DEMOTED_KEPT) this.demoted.delete(this.demoted.keys().next().value as string)
    if (renderHash !== h.renderHash() || !h.hasProgram()) return
    h.feed.demote = ranges
    h.reclassify()
    h.refreshWant()
    h.emitStatus()
  }

  demotedFor(renderHash: string): Ranges {
    return this.demoted.get(renderHash) ?? []
  }

  /** BAKED frames: how many show the bake, how many still show RAW frames. */
  state(): { hash: string | null; baked: number; waiting: number } {
    const feed = this.host.feed
    let baked = 0
    let waiting = 0
    const mode = this.host.support()?.mode
    if (mode) {
      for (let k = 0; k < mode.length; k++) {
        if (mode[k] !== MODE_BAKED) continue
        if (feed.isBaked(k)) baked++
        else waiting++
      }
    }
    return { hash: feed.bakeKey, baked, waiting }
  }

  private onLoaded(): void {
    const h = this.host
    if (!h.hasProgram() || h.feed.bakeKey !== h.renderHash()) return
    h.refreshWant()
    h.emitStatus()
  }

  dispose(): void {
    this.store?.dispose()
  }
}
