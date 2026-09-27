// The engine's side of the DEGRADED SOURCE tier (INSTANT_PREVIEW_SPEC §7,
// §6 R7): a paused frame whose source's proxy is failed is shown from one
// paused <video> on the master (media/degradedSource.ts) — sought with the
// +1 ms bias, confirmed by rVFC — and uploaded into the SAME compositor as
// laneA's frames. Its content id is the frame's own (proxy index = master
// index, R5), so a texture holding it is reused like any other. While
// playing those ranges are BAKED (support.ts); nothing here runs then.
//
// The element is created on first need, under the canvas like laneA's: the
// engine adds at most 2 media elements (§3.2, P1-E1).

import type { ProgramFeed } from './engineFeed'
import { DegradedSource, type DegradedRequest, type DegradedVideo } from './media/degradedSource'
import type { Compositor } from './render/compositor'

export interface DegradedHost {
  /** Where the element goes (the engine's root), null before attach(). */
  root(): HTMLElement | null
  compositor(): Compositor | null
  /** Frame k with content `id` is still what a paused engine wants shown. */
  wanted(k: number, id: number): boolean
  /** Draw frame k from the texture just uploaded (paused). */
  draw(k: number): void
  /** A media element was created (P1-E1 budget). */
  created(): void
  /** The frame could not be shown (it stays held; the spinner stays up). */
  failed(k: number, why: string): void
}

export class DegradedTier {
  private src: DegradedSource | null = null
  private readonly host: DegradedHost
  private readonly feed: ProgramFeed

  constructor(host: DegradedHost, feed: ProgramFeed) {
    this.host = host
    this.feed = feed
  }

  /** k is drawn by this tier (paused): its request, else null. */
  request(k: number): DegradedRequest | null {
    return this.feed.degradedAt(k)
  }

  /** Output frame a show is on its way to (−1: none). */
  get pending(): number {
    return this.src?.pending ?? -1
  }

  get element(): DegradedVideo | null {
    return this.src?.element ?? null
  }

  get stats(): DegradedSource['stats'] | null {
    return this.src?.stats ?? null
  }

  /** Show `req` as paused output frame k: at once when the texture already
   *  holds it, else seek the master and upload when confirmed. */
  show(k: number, req: DegradedRequest): void {
    const comp = this.host.compositor()
    if (comp && !comp.lost && comp.textureContent === req.contentId) {
      this.src?.cancel()
      this.host.draw(k)
      return
    }
    this.source().show(k, req)
  }

  cancel(): void {
    this.src?.cancel()
  }

  private source(): DegradedSource {
    if (this.src) return this.src
    this.src = new DegradedSource({
      createVideo: () => {
        const root = this.host.root()
        const v = document.createElement('video')
        v.playsInline = true
        v.disableRemotePlayback = true
        v.setAttribute('aria-hidden', 'true')
        // under the opaque canvas, in the layout (WebKit may not decode or
        // present a detached or display:none element), never seen
        v.style.cssText = 'position:absolute;left:0;top:0;width:100%;height:100%;object-fit:contain;pointer-events:none;'
        if (root) root.insertBefore(v, root.firstChild)
        this.host.created()
        return v as unknown as DegradedVideo
      },
      onReady: (k, id) => {
        const comp = this.host.compositor()
        const el = this.src?.element as unknown as HTMLVideoElement | null
        if (!comp || !el || !this.host.wanted(k, id)) return
        if (comp.upload(el, id, { w: el.videoWidth, h: el.videoHeight })) this.host.draw(k)
      },
      onFailed: (k, why) => this.host.failed(k, why),
    })
    return this.src
  }

  destroy(): void {
    this.src?.destroy()
    const el = this.src?.element as unknown as HTMLElement | null
    el?.remove?.()
    this.src = null
  }
}
