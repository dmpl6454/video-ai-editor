// A reload is not an outage (wave D3): WebKit fails every request in flight
// with a TypeError ("Load failed") a few ms BEFORE 'pagehide' when the page
// reloads (measured in WKWebView: rejected at 170 ms, pagehide at 174 ms).
// Reported at once, the dying page flashed "The editor engine is not
// responding." — so a network failure only counts after LEAVE_GRACE_MS
// without a pagehide. Runs the REAL api.ts against a stubbed fetch.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// the unit environment is node: a window that can say pagehide/pageshow,
// there before the modules load (connection.ts listens at import)
const win = new EventTarget()
vi.stubGlobal('window', win)
const conn = await import('./connection')
const { api } = await import('../api')

beforeEach(() => {
  conn._resetConnectionForTests()
  vi.useFakeTimers()
  vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new TypeError('Load failed'))))
})
afterEach(() => {
  vi.useRealTimers()
  win.dispatchEvent(new Event('pageshow'))
})

describe('a request failing because the page is leaving', () => {
  it('pagehide inside the grace: an AbortError (callers stay silent), and the engine stays online', async () => {
    const heard: string[] = []
    conn.onEngineState((next) => heard.push(next))
    const p = api.getEDL('s1').then(() => 'resolved', (e: Error) => e)
    await vi.advanceTimersByTimeAsync(4)
    win.dispatchEvent(new Event('pagehide'))
    await vi.advanceTimersByTimeAsync(conn.LEAVE_GRACE_MS)
    const e = await p
    expect(conn.isAbort(e)).toBe(true)
    expect(conn.isEngineOffline(e)).toBe(false)
    expect(conn.engineState()).toBe('online')
    expect(heard).toEqual([])
    // and nothing flips it while the page is going
    conn.reportEngineUnreachable()
    expect(conn.engineState()).toBe('online')
  })

  it('no pagehide: after the grace it IS an outage (typed error, offline once)', async () => {
    const p = api.getEDL('s1').then(() => 'resolved', (e: Error) => e)
    await vi.advanceTimersByTimeAsync(conn.LEAVE_GRACE_MS - 1)
    expect(conn.engineState()).toBe('online')
    await vi.advanceTimersByTimeAsync(2)
    const e = await p
    expect(conn.isEngineOffline(e)).toBe(true)
    expect(conn.engineState()).toBe('offline')
  })

  it('a page shown again from the back/forward cache judges failures again', async () => {
    win.dispatchEvent(new Event('pagehide'))
    expect(conn.isPageLeaving()).toBe(true)
    win.dispatchEvent(new Event('pageshow'))
    expect(conn.isPageLeaving()).toBe(false)
    const p = api.getEDL('s1').then(() => 'resolved', (e: Error) => e)
    await vi.advanceTimersByTimeAsync(conn.LEAVE_GRACE_MS + 1)
    expect(conn.isEngineOffline(await p)).toBe(true)
  })
})
