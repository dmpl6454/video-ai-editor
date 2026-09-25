// QA-109: when the engine dies the editor must SAY so once, stop sending
// gestures into the void (no raw "Failed to fetch" toast per gesture), and
// recover — with a message and a fresh timeline — when the engine answers
// again. Runs the REAL store and the REAL api.ts against a stubbed fetch.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mem = new Map<string, string>()
vi.stubGlobal('localStorage', {
  getItem: (k: string) => mem.get(k) ?? null,
  setItem: (k: string, v: string) => { mem.set(k, v) },
  removeItem: (k: string) => { mem.delete(k) },
})

const { useStore } = await import('./store')
const { useToasts } = await import('./toast')
const conn = await import('./lib/connection')
const { api } = await import('./api')

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
const edl = { version: 2, duration: 4, canvas: { w: 1920, h: 1080, fps: 30 },
              tracks: [{ id: 'v1', type: 'video', clips: [{ id: 'c1', src: '/u/a.mp4', start: 0, in: 0, out: 4 }] }] }

let engineUp = true
const calls: string[] = []
function server(url: string) {
  calls.push(url)
  if (!engineUp) return Promise.reject(new TypeError('Failed to fetch'))
  if (url === '/api/health') return Promise.resolve(json({ ok: true }))
  if (url.endsWith('/edl')) return Promise.resolve(json(edl))
  if (url.endsWith('/sessions/s1')) return Promise.resolve(json({ id: 's1', name: 'Reel', ops: [], summary: { edl_hash: 'h1' } }))
  if (url.endsWith('/dispatch')) return Promise.resolve(json({ result: {}, edl_hash: 'h2', op: null }))
  return Promise.resolve(json({}))
}

const toasts = () => useToasts.getState().toasts.map((t) => `${t.kind}:${t.message}`)

beforeEach(() => {
  conn._resetConnectionForTests()
  engineUp = true
  calls.length = 0
  vi.stubGlobal('fetch', vi.fn((url: string) => server(String(url))))
  useToasts.setState({ toasts: [] })
  useStore.setState({ sessionId: 's1', edl: edl as never, edlHash: 'h1', pendingOps: 0, engine: 'online', uploading: false })
})
afterEach(() => { vi.useRealTimers() })

// The store subscribes to connection changes at import; re-attach after the
// per-test reset (which clears listeners).
async function withStoreListener() {
  vi.resetModules()
}

describe('engine connection (QA-109)', () => {
  it('a failed request marks the engine offline and throws a typed error, not "Failed to fetch"', async () => {
    engineUp = false
    await expect(api.getEDL('s1')).rejects.toSatisfy((e: unknown) => conn.isEngineOffline(e))
    expect(conn.engineState()).toBe('offline')
  })

  it('the dev proxy\'s bare 500 counts as unreachable; the engine\'s own 500 does not', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('', { status: 500 })))
    await expect(api.getEDL('s1')).rejects.toSatisfy((e: unknown) => conn.isEngineOffline(e))
    conn._resetConnectionForTests()
    vi.stubGlobal('fetch', vi.fn(async () => json({ error: { code: 'INTERNAL', message: 'boom' } }, 500)))
    await expect(api.getEDL('s1')).rejects.toThrow(/^500/)
    expect(conn.engineState()).toBe('online')
  })

  it('probing brings it back online once the engine answers', async () => {
    engineUp = false
    await api.getEDL('s1').catch(() => undefined)
    expect(await conn.probeEngineNow()).toBe(false)
    engineUp = true
    expect(await conn.probeEngineNow()).toBe(true)
    expect(conn.engineState()).toBe('online')
  })
})

describe('the store while the engine is gone (QA-109)', () => {
  it('refuses gestures with ONE notice, sends nothing more, and recovers with a refresh', async () => {
    await withStoreListener()
    // Fresh modules so the store's onEngineState listener is attached to the
    // connection module the api uses.
    const { useStore: store } = await import('./store')
    const { useToasts: toastsStore } = await import('./toast')
    const c = await import('./lib/connection')
    toastsStore.setState({ toasts: [] })
    store.setState({ sessionId: 's1', edl: edl as never, edlHash: 'h1', pendingOps: 0, engine: 'online', uploading: false })
    engineUp = false
    // Three gestures, like the report (M, S, ⌘Z).
    expect(await store.getState().dispatch('set_clip_muted', { clip_id: 'c1' })).toBeNull()
    expect(store.getState().engine).toBe('offline')
    const sent = calls.length
    expect(await store.getState().dispatch('split_at', { time: 1 })).toBeNull()
    expect(await store.getState().dispatch('undo')).toBeNull()
    expect(calls.length).toBe(sent)                       // refused locally
    const msgs = toastsStore.getState().toasts.map((t) => `${t.kind}:${t.message}`)
    expect(msgs).toHaveLength(1)
    expect(msgs[0]).toMatch(/^info:Not applied — the editor engine is not responding/)
    expect(msgs.join(' ')).not.toMatch(/Failed to fetch/)
    // The engine comes back: one "reconnected", a refresh, gestures flow again.
    engineUp = true
    expect(await c.probeEngineNow()).toBe(true)
    await new Promise((r) => setTimeout(r, 0))
    expect(store.getState().engine).toBe('online')
    expect(toastsStore.getState().toasts.map((t) => t.message)).toContain('Reconnected to the editor engine.')
    expect(calls.some((u) => u.endsWith('/sessions/s1/edl'))).toBe(true)
    expect(await store.getState().dispatch('split_at', { time: 1 })).not.toBeNull()
  })
})

describe('project rename (QA-099)', () => {
  it('renames through PATCH and shows the server\'s name', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
      calls.push(`${init?.method} ${url} ${init?.body}`)
      return json({ id: 's1', name: 'My Reel' })
    }))
    expect(await useStore.getState().renameSession('  My   Reel ')).toBe(true)
    expect(calls.at(-1)).toBe('PATCH /api/sessions/s1 {"name":"My Reel"}')
    expect(useStore.getState().sessionName).toBe('My Reel')
  })

  it('keeps the old name and says why when the server refuses', async () => {
    useStore.setState({ sessionName: 'Old' })
    vi.stubGlobal('fetch', vi.fn(async () => json({ error: { code: 'BAD_REQUEST', message: 'request failed', details: { code: 'invalid_name', message: "A project name can't be empty." } } }, 400)))
    expect(await useStore.getState().renameSession('x')).toBe(false)
    expect(useStore.getState().sessionName).toBe('Old')
    expect(toasts()).toEqual(["error:Couldn't rename the project: A project name can't be empty."])
  })
})
