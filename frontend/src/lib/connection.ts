// ONE connection state for the whole editor (QA-109).
//
// When the backend died, nothing said so: the editor looked idle and healthy,
// and every gesture then popped its own raw "Failed to fetch" toast (six for
// three gestures) and was dropped. Recovery after a restart was silent too.
//
// Now every request that cannot reach the engine reports it here, the state
// flips to `offline` once, a probe asks `/api/health` until the engine answers
// again, and listeners (the store, the banner) hear each transition exactly
// once. Module state, not React state: api.ts reports into it and cannot import
// the store (the store imports api.ts).

export type EngineState = 'online' | 'offline'
type Listener = (next: EngineState, prev: EngineState) => void

export const ENGINE_OFFLINE_MESSAGE = 'The editor engine is not responding.'
/** How often the probe asks while offline. */
export const PROBE_MS = 1500
/** Under /api so the Vite dev proxy forwards it (a bare /livez would be
 *  answered by Vite's own index.html fallback with a 200). */
export const PROBE_PATH = '/api/health'

/** The error every engine-unreachable request throws: callers test for it
 *  (`isEngineOffline`) instead of toasting the browser's "Failed to fetch". */
export class EngineOfflineError extends Error {
  readonly offline = true
  constructor() { super(ENGINE_OFFLINE_MESSAGE) }
}

export function isEngineOffline(e: unknown): boolean {
  return e instanceof EngineOfflineError || (!!e && typeof e === 'object' && (e as { offline?: unknown }).offline === true)
}

/** A request the browser aborted on purpose (a superseded preview) is not a
 *  sign the engine is gone. */
export function isAbort(e: unknown): boolean {
  return !!e && typeof e === 'object' && (e as { name?: unknown }).name === 'AbortError'
}

/**
 * A response that came from something IN FRONT of the engine rather than from
 * it: 502/503/504, or the Vite dev proxy's bare 500 (empty body) when nothing
 * listens behind it. The engine's own errors always carry the hardening
 * envelope, so a 500 WITH a body is a real server error, not an outage.
 */
export function isGatewayFailure(status: number, body: string): boolean {
  if (status === 502 || status === 504) return true
  // A 503 carrying the engine's own envelope is the engine ANSWERING — e.g.
  // `ffmpeg_missing` (QA-108): the app is up, a dependency is not. Treating it
  // as an outage would bury the install instruction under "disconnected".
  if (status === 503) return !isEngineEnvelope(body)
  return status === 500 && body.trim() === ''
}

function isEngineEnvelope(body: string): boolean {
  try {
    const parsed = JSON.parse(body) as { error?: { code?: unknown } }
    return typeof parsed?.error?.code === 'string'
  } catch {
    return false
  }
}

// ------------------------------------------------------------ page leaving
//
// A reload (⌘R, location.reload) or a navigation makes WebKit fail EVERY
// request in flight with a plain TypeError ("Load failed") a few ms BEFORE
// 'pagehide' — measured in WKWebView (tests/wk/test_wk_robustness.py): the
// fetch rejected at 170 ms, pagehide at 174 ms, and no 'beforeunload' at all
// for a scripted reload. Reported at once, that flashed "The editor engine is
// not responding." (banner, panel errors) in the dying page, in server mode
// as much as in client mode. So a network failure is judged only after
// LEAVE_GRACE_MS: a page that meanwhile says pagehide was leaving, not
// offline (and a page that is gone never runs that timer at all).

/** How long a network failure waits for 'pagehide' before it counts. */
export const LEAVE_GRACE_MS = 250
let leaving = false
if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
  window.addEventListener('pagehide', () => { leaving = true })
  window.addEventListener('pageshow', () => { leaving = false })
}

/** The page is being unloaded (pagehide seen, not shown again). */
export function isPageLeaving(): boolean {
  return leaving
}

/** After a network failure: resolves true when the page turns out to be
 *  leaving within `ms` (the failure was the unload, not the engine). */
export function pageLeavingSoon(ms = LEAVE_GRACE_MS): Promise<boolean> {
  if (leaving) return Promise.resolve(true)
  return new Promise((resolve) => { setTimeout(() => resolve(leaving), ms) })
}

/** The error a request gets when it failed only because the page is leaving:
 *  an AbortError, so every caller stays silent (`isAbort`). */
export function leavingError(): Error {
  const e = new Error('The page is being unloaded.')
  e.name = 'AbortError'
  return e
}

let state: EngineState = 'online'
const listeners = new Set<Listener>()
let probeTimer: ReturnType<typeof setTimeout> | null = null
let probing = false

export const engineState = (): EngineState => state

/** Subscribe to transitions; returns the unsubscribe. */
export function onEngineState(fn: Listener): () => void {
  listeners.add(fn)
  return () => { listeners.delete(fn) }
}

function setState(next: EngineState): void {
  if (next === state) return
  const prev = state
  state = next
  if (next === 'offline') scheduleProbe()
  else clearProbe()
  for (const l of [...listeners]) {
    try { l(next, prev) } catch (e) { console.warn('[connection] listener failed:', e) }
  }
}

/** A request could not reach the engine (ignored while the page is leaving). */
export function reportEngineUnreachable(): void { if (!leaving) setState('offline') }
/** Something the engine itself answered arrived (any status). */
export function reportEngineReachable(): void { setState('online') }

function clearProbe(): void {
  if (probeTimer !== null) clearTimeout(probeTimer)
  probeTimer = null
}

function scheduleProbe(): void {
  clearProbe()
  probeTimer = setTimeout(() => { probeTimer = null; void probeEngineNow() }, PROBE_MS)
}

/**
 * Ask the engine whether it is there. Resolves true when it answered; while
 * offline a failed probe schedules the next one. Also the banner's "Retry now".
 */
export async function probeEngineNow(): Promise<boolean> {
  if (probing) return state === 'online'
  probing = true
  try {
    const res = await fetch(PROBE_PATH, { cache: 'no-store' })
    const body = res.ok ? '' : await res.text().catch(() => '')
    if (res.ok || !isGatewayFailure(res.status, body)) {
      reportEngineReachable()
      return true
    }
  } catch {
    // unreachable — fall through (unless the page is just leaving)
    if (await pageLeavingSoon()) { probing = false; return state === 'online' }
  } finally {
    probing = false
  }
  if (state === 'online') setState('offline')
  else scheduleProbe()
  return false
}

/** Tests only: back to a clean `online` with no timers or listeners. */
export function _resetConnectionForTests(): void {
  clearProbe()
  leaving = false
  listeners.clear()
  state = 'online'
  probing = false
}
