// Engine options, the silent default sink, and the capability check
// (INSTANT_PREVIEW_SPEC §7 engine-level fallback). Re-exported by engine.ts.

import type { AudioSink } from './engine'
import type { FetchLike } from './media/proxyIndex'
import type { Size } from './render/geometry'

/** The sink used until the audio lane lands: silent, no clock. */
export class NullAudioSink implements AudioSink {
  prepare(): void {}
  start(): void {}
  stop(): void {}
  reschedule(): void {}
  setParams(): void {}
  ctxTimeAt(): number | null { return null }
  dispose(): void {}
}

/** Engine construction options (all optional in the app). */
export interface EngineOptions {
  /** `/api/proxies` in the app. */
  proxyBaseUrl?: string
  /** `/api/sessions/{sid}/bake` in the app: enables the bake splice (§5.3). */
  bakeBaseUrl?: string
  fetch?: FetchLike
  audioSink?: AudioSink
  /** A fixed backing size for the canvas (tests); else host box × DPR. */
  canvasSize?: Size
  /** Short edge cap of the backing canvas (§3.4: 1080). */
  maxShortEdge?: number
  spanCacheBytes?: number
  /** Resume picture + sound when the page is visible again after an
   *  external pause, if the user never paused (default true). */
  resumeOnVisible?: boolean
  /** Trilinear sampling for minified clips (default true). */
  mipmaps?: boolean
  /** Stop playback as soon as the page is hidden (default true, §3.5). With
   *  false the engine relies on noticing WebKit's own pause (tests). */
  pauseOnHidden?: boolean
}

/** Content id of proxy `slot`'s frame `frame` (laneA `want`/`have`). */
/** Why the client engine cannot run here at all (§7), or null. */
export function engineUnsupportedReason(): string | null {
  if (typeof window === 'undefined') return 'no-window'
  if (!('MediaSource' in window || 'ManagedMediaSource' in window)) return 'no-mse'
  try {
    const c = document.createElement('canvas')
    if (!c.getContext('webgl2')) return 'no-webgl2'
  } catch {
    return 'no-webgl2'
  }
  return null
}

/** A tiny typed event emitter; a throwing listener never stops the others. */
export class Emitter<M> {
  private readonly sets = new Map<keyof M, Set<(e: never) => void>>()

  on<E extends keyof M>(event: E, cb: (e: M[E]) => void): () => void {
    let set = this.sets.get(event)
    if (!set) this.sets.set(event, (set = new Set()))
    set.add(cb as (e: never) => void)
    return () => { set.delete(cb as (e: never) => void) }
  }

  emit<E extends keyof M>(event: E, payload: M[E]): void {
    for (const cb of this.sets.get(event) ?? []) {
      try {
        (cb as (e: M[E]) => void)(payload)
      } catch (e) {
        console.error('[preview engine] listener failed', e)
      }
    }
  }

  clear(): void {
    this.sets.clear()
  }
}
