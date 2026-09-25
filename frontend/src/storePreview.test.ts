// QA-004: store.renderPreview is latest-wins. Runs the REAL store against a
// mocked api module whose preview() answers are released by hand, so the
// arrival order of responses is controlled exactly.
import { beforeEach, describe, expect, it, vi } from 'vitest'

interface Pending {
  sid: string
  signal: AbortSignal | undefined
  resolve: (r: { edl_hash: string }) => void
  reject: (e: unknown) => void
}
const calls: Pending[] = []

vi.mock('./api', () => ({
  api: {
    preview: vi.fn((sid: string, signal?: AbortSignal) => new Promise((resolve, reject) => {
      calls.push({ sid, signal, resolve: resolve as Pending['resolve'], reject })
    })),
    upload: vi.fn(async () => ({})),
    audioUpload: vi.fn(async () => ({})),
    getSession: vi.fn(async () => ({ ops: [], name: 'n', redo_available: false, summary: { edl_hash: 'x' } })),
    getEDL: vi.fn(async () => ({ version: 2, duration: 5, tracks: [], canvas: { w: 1080, h: 1920, fps: 30 } })),
  },
}))

// Node ships a half-initialised global `localStorage` (no backing file), whose
// getItem is not a function; the store reads panel sizes from it at creation.
const mem = new Map<string, string>()
vi.stubGlobal('localStorage', {
  getItem: (k: string) => mem.get(k) ?? null,
  setItem: (k: string, v: string) => { mem.set(k, v) },
  removeItem: (k: string) => { mem.delete(k) },
})

const { useStore } = await import('./store')
const { api } = await import('./api')

const hash = (h: string) => ({ path: '', cached: false, edl_hash: h, url: '' })
const flush = () => new Promise((r) => setTimeout(r, 0))

beforeEach(() => {
  calls.length = 0
  vi.mocked(api.preview).mockClear()
  useStore.setState({ sessionId: 's_1', previewHash: null, previewRendering: false })
})

describe('renderPreview latest-wins', () => {
  it('an older response that arrives LAST never replaces the newer preview', async () => {
    const older = useStore.getState().renderPreview()   // e.g. the edit
    const newer = useStore.getState().renderPreview()   // the undo, 0.7 s later
    expect(calls).toHaveLength(2)
    // The undone state is cached server-side and answers first…
    calls[1].resolve(hash('undone'))
    await newer
    expect(useStore.getState().previewHash).toBe('undone')
    // …then the slow render of the edit lands. It must be ignored.
    calls[0].resolve(hash('edited'))
    await older
    expect(useStore.getState().previewHash).toBe('undone')
  })

  it('aborts the superseded request through the signal fetch actually receives', async () => {
    const first = useStore.getState().renderPreview()
    expect(calls[0].signal).toBeInstanceOf(AbortSignal)
    expect(calls[0].signal!.aborted).toBe(false)
    const second = useStore.getState().renderPreview()
    expect(calls[0].signal!.aborted).toBe(true)
    expect(calls[1].signal!.aborted).toBe(false)
    // fetch rejects an aborted request with AbortError — not an error to report.
    calls[0].reject(new DOMException('aborted', 'AbortError'))
    await expect(first).resolves.toBe('')
    calls[1].resolve(hash('b'))
    await expect(second).resolves.toBe('b')
  })

  it('keeps the Rendering flag up until the NEWEST render settles', async () => {
    const a = useStore.getState().renderPreview()
    const b = useStore.getState().renderPreview()
    expect(useStore.getState().previewRendering).toBe(true)
    calls[0].reject(new DOMException('aborted', 'AbortError'))
    await a
    expect(useStore.getState().previewRendering).toBe(true)
    calls[1].resolve(hash('b'))
    await b
    expect(useStore.getState().previewRendering).toBe(false)
  })

  it("treats the server's 409 preview_superseded as a quiet no-op", async () => {
    useStore.setState({ previewHash: 'shown' })
    const p = useStore.getState().renderPreview()
    calls[0].reject(new Error('409 Conflict: {"error":{"code":"CONFLICT","details":{"error":"preview_superseded"}}}'))
    await expect(p).resolves.toBe('shown')
    expect(useStore.getState().previewHash).toBe('shown')
  })

  it('still reports a real render failure of the newest request', async () => {
    const p = useStore.getState().renderPreview()
    calls[0].reject(new Error('422 Unprocessable: render_failed'))
    await expect(p).rejects.toThrow('render_failed')
    expect(useStore.getState().previewRendering).toBe(false)
  })

  it('drops a response that arrives after the session changed', async () => {
    const p = useStore.getState().renderPreview()
    useStore.setState({ sessionId: 's_2', previewHash: null })
    calls[0].resolve(hash('from-s1'))
    await p
    expect(useStore.getState().previewHash).toBeNull()
  })
})

describe('imports render once', () => {
  it('upload() leaves the render to the Preview effect instead of firing its own', async () => {
    const file = new File([new Uint8Array([1])], 'a.mp4')
    await useStore.getState().upload(file)
    await useStore.getState().uploadAudio(file)
    await flush()
    expect(api.preview).not.toHaveBeenCalled()
  })
})
