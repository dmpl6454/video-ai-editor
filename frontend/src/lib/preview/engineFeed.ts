// The engine's PROGRAM FEED (INSTANT_PREVIEW_SPEC §4.1 steps 4-5): turns a
// committed EDL into the program map, gives every proxy frame a stable
// content id, and hands laneA the per-frame `want` list plus access to the
// bytes (ProxyStore). Content ids survive edits — one slot per proxy key — so
// laneA can tell which output frames really changed.

import type { EdlClip, EdlLike } from './timeline/framePlan'
import { defaultTimeBase, type SourceInfo } from './timeline/frameMap'
import {
  KIND_GAP, audioPlacements, buildProgramMap, diffPrograms, type AudioPlacement, type ProgramDiff, type ProgramMap,
} from './timeline/programMap'
import { MODE_BAKED, classify, type ProxyState, type Support } from './timeline/support'
import type { Rational } from './timeline/timebase'
import { GAP, type LaneProgram } from './media/laneA'
import type { ProxyHandle, ProxyStore } from './media/proxyIndex'
import type { DegradedRequest } from './media/degradedSource'
import type { Size } from './render/geometry'
import type { EngineSourceLookup } from './engine'

/** Frames per content-id slot: id = slot · CONTENT_SPAN + proxy frame. */
export const CONTENT_SPAN = 2 ** 22
const PENDING = 'pending:'
/** Slot key prefix of a bake (the server's frames of one render hash, §5.3):
 *  `bake:<hash>`, read from the bake store, bake frame k = output frame k. */
export const BAKE = 'bake:'

export class ProgramFeed {
  pm: ProgramMap | null = null
  edl: EdlLike | null = null
  want = new Float64Array(0)
  R: Rational = { num: 30, den: 1 }
  canvas: Size = { w: 1080, h: 1920 }
  lookup: EngineSourceLookup = () => null
  private readonly store: ProxyStore
  /** The bake store (`/api/sessions/{sid}/bake/<hash>/…`), when the engine
   *  splices bakes. */
  bakeStore: ProxyStore | null = null
  /** The hash whose bake frames may replace BAKED frames (§4.1 step 8). */
  private bakeHash: string | null = null
  /** Output ranges the structural check demoted to BAKED (R14). */
  demote: ReadonlyArray<readonly [number, number]> = []
  /** slot → proxy key (or `pending:<src>` for a source with no proxy). */
  private readonly slots: string[] = []
  private readonly slotOf = new Map<string, number>()
  private readonly failed = new Set<string>()

  constructor(store: ProxyStore) {
    this.store = store
  }

  /** The master's frame facts; an unknown source gets a CFR stand-in, so the
   *  map stays whole while its frames are PENDING. */
  sourceInfo(src: string): SourceInfo {
    const s = this.lookup(src)
    if (s?.info) return s.info
    return { rate: this.R, tb: defaultTimeBase(this.R), frames: 1 << 21, startTicks: 0, w: this.canvas.w, h: this.canvas.h }
  }

  /** A new program; throws only if the map cannot be built at all. */
  build(edl: EdlLike, lookup: EngineSourceLookup, R: Rational, canvas: Size): ProgramDiff {
    this.lookup = lookup
    this.R = R
    this.canvas = canvas
    const pm = buildProgramMap(edl, (src) => this.sourceInfo(src))
    const diff = diffPrograms(this.pm, pm)
    this.pm = pm
    this.edl = edl
    const slotOfSrc = pm.sources.map((src) => this.slotFor(lookup(src)?.proxy?.key ?? `${PENDING}${src}`))
    const want = new Float64Array(pm.total)
    for (let k = 0; k < pm.total; k++) {
      want[k] = pm.kind[k] === KIND_GAP ? GAP : slotOfSrc[pm.srcKey[k]] * CONTENT_SPAN + pm.srcFrame[k]
    }
    this.want = want
    return diff
  }

  /** Forget the map (a new frame rate: nothing carries over). */
  reset(): void {
    this.pm = null
  }

  /** Open every proxy of the program; `onOpen` when each is readable.
   *  A key marked failed earlier (a transient open failure) that opens now
   *  is readable again: `onRecovered` lets the engine reclassify (§7). */
  openProxies(onOpen: () => void, onRecovered?: () => void): void {
    if (!this.pm) return
    for (const src of this.pm.sources) {
      const p = this.lookup(src)?.proxy
      if (!p?.key || p.state === 'failed') continue
      const key = p.key
      void this.store.open(key).then(() => {
        if (this.failed.delete(key)) onRecovered?.()
        onOpen()
      }, () => undefined)
    }
  }

  private keyOf(id: number): string | null {
    const key = this.slots[Math.floor(id / CONTENT_SPAN)]
    return key && !key.startsWith(PENDING) && !this.failed.has(key) ? key : null
  }

  private slotFor(key: string): number {
    let slot = this.slotOf.get(key)
    if (slot === undefined) {
      slot = this.slots.length
      this.slots.push(key)
      this.slotOf.set(key, slot)
    }
    return slot
  }

  /** (store, key in that store) of a slot key: bakes live in the bake store. */
  private route(key: string): [ProxyStore, string] | null {
    if (!key.startsWith(BAKE)) return [this.store, key]
    return this.bakeStore ? [this.bakeStore, key.slice(BAKE.length)] : null
  }

  /** The proxy (or bake) behind output frame k (null for a gap or a pending source). */
  handleAt(k: number): ProxyHandle | null {
    const id = this.want[k]
    if (id === undefined || id < 0) return null
    const key = this.keyOf(id)
    const r = key ? this.route(key) : null
    return r ? r[0].handle(r[1]) : null
  }

  /** Output frame k shows a bake frame (drawn full-canvas: the bake is the
   *  composited picture, §5.3). */
  isBaked(k: number): boolean {
    const id = this.want[k]
    if (id === undefined || id < 0) return false
    return !!this.slots[Math.floor(id / CONTENT_SPAN)]?.startsWith(BAKE)
  }

  laneProgram(): LaneProgram {
    const want = this.want
    return {
      total: this.pm?.total ?? 0, want,
      sample: (id) => {
        const key = this.keyOf(id)
        const r = key ? this.route(key) : null
        return r ? r[0].sample(r[1], id % CONTENT_SPAN) : null
      },
      request: (id, priority, urgent) => {
        const key = this.keyOf(id)
        const r = key ? this.route(key) : null
        if (r) r[0].request(r[1], id % CONTENT_SPAN, priority, urgent)
      },
    }
  }

  // ------------------------------------------------------- bake splice

  /** Bake frames of `hash` may now replace the BAKED frames (the preview
   *  render of the current program landed); null forgets the bake. */
  setBake(hash: string | null): void {
    this.bakeHash = hash
  }

  get bakeKey(): string | null {
    return this.bakeHash
  }

  /** BAKED output ranges of `support` (merged, half-open). */
  static bakedRanges(support: Support | null): Array<[number, number]> {
    const out: Array<[number, number]> = []
    for (const r of support?.ranges ?? []) {
      if (r.mode !== MODE_BAKED) continue
      const last = out[out.length - 1]
      if (last && last[1] === r.k0) last[1] = r.k1
      else out.push([r.k0, r.k1])
    }
    return out
  }

  /** Point every BAKED frame whose bake sample HAS LANDED at it (RAW client
   *  frames stay until then: switching early would cut them out of laneA's
   *  interval and stall). Returns how many frames changed. */
  applyBake(support: Support | null): number {
    const h = this.bakeHash
    const bs = this.bakeStore
    if (!h || !bs || !support || !this.pm) return 0
    const slot = this.slotFor(BAKE + h)
    let changed = 0
    const n = Math.min(this.want.length, support.mode.length)
    for (let k = 0; k < n; k++) {
      if (support.mode[k] !== MODE_BAKED) continue
      const id = slot * CONTENT_SPAN + k
      if (this.want[k] === id || !bs.has(h, k)) continue
      this.want[k] = id
      changed++
    }
    return changed
  }

  /** Ask the bake store for the BAKED frames in [P − back, P + ahead),
   *  nearest first (span by span). */
  prefetchBake(support: Support | null, P: number, back: number, ahead: number): void {
    const h = this.bakeHash
    const bs = this.bakeStore
    if (!h || !bs || !support) return
    const n = support.mode.length
    const a = Math.max(0, P - back)
    const b = Math.min(n, P + ahead)
    const seen = new Set<number>()
    const order: number[] = []
    for (let k = P; k < b; k++) order.push(k)
    for (let k = P - 1; k >= a; k--) order.push(k)
    for (const k of order) {
      if (support.mode[k] !== MODE_BAKED || this.isBaked(k)) continue
      const span = bs.spanOf(h, k)
      if (span >= 0 && seen.has(span)) continue
      seen.add(span)
      bs.request(h, k, Math.abs(k - P), false)
    }
  }

  /** Ask for every span output frames [P − back, P + ahead) need, nearest
   *  first, and re-rank what is already queued by the same distance. */
  prefetch(P: number, back: number, ahead: number): void {
    const pm = this.pm
    if (!pm) return
    const a = Math.max(0, P - back)
    const b = Math.min(pm.total, P + ahead)
    const order: number[] = []
    for (let k = P; k < b; k++) order.push(k)
    for (let k = P - 1; k >= a; k--) order.push(k)
    const seen = new Set<string>()
    const nearest = new Map<string, number>()
    for (const k of order) {
      const id = this.want[k]
      if (id < 0) continue
      const key = this.keyOf(id)
      const r = key ? this.route(key) : null
      if (!key || !r) continue
      const [store, skey] = r
      const frame = id % CONTENT_SPAN
      const span = store.spanOf(skey, frame)
      const tag = `${key}/${span}`
      if (!nearest.has(tag)) nearest.set(tag, Math.abs(k - P))
      if (span >= 0 && seen.has(tag)) continue
      seen.add(tag)
      store.request(skey, frame, Math.abs(k - P), false)
    }
    this.store.reprioritize((key, span) => nearest.get(`${key}/${span}`) ?? 1e9)
  }

  /** Where every v1 clip's sound lands (programMap.audioPlacements), or
   *  none if they cannot be computed (the sink then stays silent). */
  audioPlacements(): AudioPlacement[] {
    if (!this.pm) return []
    try {
      return audioPlacements(this.pm, (src) => this.sourceInfo(src))
    } catch (e) {
      console.error('[preview engine] audio placements failed', e)
      return []
    }
  }

  markFailed(key: string): void {
    this.failed.add(key)
  }

  /** Output frame k comes from the DEGRADED tier while paused (§7): its
   *  source's proxy is failed, it is not a landed bake frame, and the
   *  source's master can be played (`EngineSource.media`). Null otherwise. */
  degradedAt(k: number): DegradedRequest | null {
    const pm = this.pm
    const id = this.want[k]
    if (!pm || id === undefined || id < 0 || k >= pm.total || this.isBaked(k)) return null
    const src = pm.sources[pm.srcKey[k]]
    if (this.proxyState(src) !== 'failed') return null
    const url = this.lookup(src)?.media
    if (!url) return null
    return { url, info: this.sourceInfo(src), frame: pm.srcFrame[k], contentId: id }
  }

  /** Set by the engine: whether a clip's IMAGE canvas background picture is
   *  not decoded yet (or failed) — its frames are PENDING (review RE). */
  canvasImagePending: ((clip: EdlClip) => boolean) | null = null

  proxyState(src: string): ProxyState {
    const s = this.lookup(src)
    const key = s?.proxy?.key
    if (!key) return 'pending'
    if (s?.proxy?.state === 'failed' || this.failed.has(key)) return 'failed'
    return 'ready'
  }

  /** Fidelity classes of the program (§7), Phase 1 capabilities. */
  classify(limiting?: ReadonlyArray<readonly [number, number]>): Support | null {
    if (!this.pm || !this.edl) return null
    try {
      return classify(this.pm, this.edl, {
        phase: 1, proxyState: (src) => this.proxyState(src), demote: this.demote,
        canvasImagePending: this.canvasImagePending ?? undefined, limiting,
      })
    } catch (e) {
      console.error('[preview engine] classify failed', e)
      return null
    }
  }
}
