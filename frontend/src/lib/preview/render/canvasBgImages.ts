// The cover-fitted pictures of IMAGE canvas backgrounds (wave E, lane F2),
// fetched once per URL from the canvas-bg route — the export's own file
// (render/canvas_bg.image_file) — and handed to the compositor as a texture
// source once decoded. A picture that is still loading draws black bars;
// its `ready` callback asks the engine to redraw the paused frame.
//
// Review RE: a picture that failed ONCE used to be cached as failed for
// good (black bars until a reload, classed EXACT). A failure is retried
// with a backoff (1 s, 2 s, 4 s … 30 s) and at once when the engine
// connection comes back; until a picture is ready its frames are PENDING
// (`canvasBgImageState`, support.ts), so the spinner says so.

import { onEngineState } from '../../connection'

interface Entry {
  img: HTMLImageElement
  state: 'loading' | 'ready' | 'error'
  waiters: Set<() => void>
  /** Failures in a row, and when the next attempt may start (ms, performance clock). */
  fails: number
  retryAt: number
}

const CACHE = new Map<string, Entry>()
/** Pictures kept (a timeline rarely has more than a handful). */
const MAX = 16
const BACKOFF_MS = 1000
const BACKOFF_MAX_MS = 30000

const now = () => (typeof performance !== 'undefined' ? performance.now() : Date.now())

function load(url: string, e: Entry): void {
  if (typeof Image === 'undefined') return
  const img = new Image()
  img.decoding = 'async'
  e.img = img
  e.state = 'loading'
  img.onload = () => {
    e.state = 'ready'
    e.fails = 0
    for (const w of e.waiters) w()
    e.waiters.clear()
  }
  img.onerror = () => {
    e.state = 'error'
    e.fails += 1
    e.retryAt = now() + Math.min(BACKOFF_MAX_MS, BACKOFF_MS * 2 ** (e.fails - 1))
    // the waiters stay: a later successful retry still redraws them
  }
  img.src = url
}

/** The decoded picture at `url`, or null while it loads (then `ready` is
 *  called once it has) or after it failed (retried with a backoff). */
export function canvasBgImage(url: string, ready?: () => void): HTMLImageElement | null {
  let e = CACHE.get(url)
  if (!e) {
    if (typeof Image === 'undefined') return null
    e = { img: null as unknown as HTMLImageElement, state: 'loading', waiters: new Set(), fails: 0, retryAt: 0 }
    CACHE.set(url, e)
    load(url, e)
    while (CACHE.size > MAX) {
      const first = CACHE.keys().next().value as string
      CACHE.delete(first)
    }
  } else if (e.state === 'error' && now() >= e.retryAt) {
    load(url, e)
  }
  if (e.state === 'ready') return e.img
  if (ready) e.waiters.add(ready)
  return null
}

/** Where the picture at `url` stands: `none` (never asked for). */
export function canvasBgImageState(url: string): 'none' | 'loading' | 'ready' | 'error' {
  return CACHE.get(url)?.state ?? 'none'
}

/** Retry every failed picture now (the engine is reachable again). */
export function retryFailedCanvasBgImages(): void {
  for (const [url, e] of CACHE) if (e.state === 'error') { e.retryAt = 0; load(url, e) }
}

if (typeof window !== 'undefined') onEngineState((next) => { if (next === 'online') retryFailedCanvasBgImages() })

/** Tests: forget every picture. */
export function resetCanvasBgImages(): void {
  CACHE.clear()
}
