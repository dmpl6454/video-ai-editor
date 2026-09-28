// The store in CLIENT preview mode (INSTANT_PREVIEW_SPEC §3.5, §4.1 steps
// 1-3, §7, §9.2) against the server-mode contract it must leave untouched.
// The REAL store runs against a mocked api and a recording stand-in for the
// engine's controller (lib/preview/previewController).
import { beforeEach, describe, expect, it, vi } from 'vitest'

const EDL0 = { version: 3, duration: 4, tracks: [{ id: 'v1', type: 'video', clips: [{ id: 'c1', src: '/m/a.mp4', in: 0, out: 4, start: 0 }] }], canvas: { w: 1080, h: 1920, fps: 30 } }
const EDL1 = { ...EDL0, duration: 2, tracks: [{ id: 'v1', type: 'video', clips: [{ id: 'c1', src: '/m/a.mp4', in: 0, out: 2, start: 0 }] }] }

const calls: Array<[string, ...unknown[]]> = []

vi.mock('./api', () => ({
  api: {
    dispatch: vi.fn(async () => ({ result: {}, edl_hash: 'srv1', op: null })),
    dispatchWithEdl: vi.fn(async () => ({ result: {}, edl_hash: 'cli1', op: null, render_hash: 'aaaaaaaaaaaaaaaa', edl: EDL1 })),
    getSession: vi.fn(async () => ({ ops: [{ tool: 'x' }], name: 'n', redo_available: true, summary: { edl_hash: 'cli1' } })),
    getEDL: vi.fn(async () => EDL0),
    preview: vi.fn(async () => ({ path: '', cached: false, edl_hash: 'p', url: '' })),
    previewLow: vi.fn(async () => ({ path: '', cached: false, edl_hash: 'p', url: '' })),
    previewSettings: vi.fn(async () => ({ engine: 'server', source: 'default' })),
    setPreviewEngine: vi.fn(async (engine: string) => ({ engine, source: 'settings' })),
  },
  clientFetch: vi.fn(),
}))

// a recording controller
vi.mock('./lib/preview/previewController', () => ({
  PreviewController: class {
    sessionId: string
    appliedEdl: unknown = null
    opts: Record<string, (...a: unknown[]) => void>
    constructor(opts: { sessionId: string }) {
      this.sessionId = opts.sessionId
      this.opts = opts as unknown as Record<string, (...a: unknown[]) => void>
      calls.push(['new', opts.sessionId])
      ;(globalThis as Record<string, unknown>).__ctl = this
    }
    applyTimeline(edl: unknown, h: string | null) { this.appliedEdl = edl; calls.push(['applyTimeline', h, edl]) }
    onPreviewLanded(h: string | null) { calls.push(['onPreviewLanded', h]) }
    play(t: number) { calls.push(['play', t]); return true }
    pause() { calls.push(['pause']) }
    dispose() { calls.push(['dispose']) }
  },
}))

// A window that CAN run the engine (MediaSource, WebGL2, AudioContext), so
// the resolution below is decided by the setting, the rate and fallbacks.
vi.stubGlobal('window', {
  MediaSource: class {}, AudioContext: class {},
  document: { createElement: () => ({ getContext: (k: string) => (k === 'webgl2' ? {} : null) }) },
})

const mem = new Map<string, string>()
vi.stubGlobal('localStorage', {
  getItem: (k: string) => mem.get(k) ?? null,
  setItem: (k: string, v: string) => { mem.set(k, v) },
  removeItem: (k: string) => { mem.delete(k) },
})

const { useStore, previewController } = await import('./store')
const { useToasts } = await import('./toast')
const { api } = await import('./api')
const flush = (ms = 0) => new Promise((r) => setTimeout(r, ms))
const ctl = () => (globalThis as Record<string, unknown>).__ctl as { opts: Record<string, (...a: unknown[]) => void> }

function toMode(mode: 'server' | 'client') {
  useStore.setState({ previewEngine: mode, previewEngineReason: null })
}

beforeEach(() => {
  calls.length = 0
  for (const f of Object.values(api)) (f as ReturnType<typeof vi.fn>).mockClear?.()
  useStore.setState({ previewEngine: 'server', sessionId: null, edl: null, edlHash: null, isPlaying: false,
                      playhead: 0, playbackRate: 1, pendingOps: 0, previewHash: null })
  useStore.setState({ sessionId: 's_1', edl: EDL0 as never, edlHash: 'h0' })
})

describe('server mode (the default) is what it always was', () => {
  it('dispatches without include=edl and refreshes the EDL; no controller exists', async () => {
    const res = await useStore.getState().dispatch('trim_clip', { clip_id: 'c1', out: 2 })
    expect(res?.edl_hash).toBe('srv1')
    expect(api.dispatch).toHaveBeenCalledWith('s_1', 'trim_clip', { clip_id: 'c1', out: 2 }, 'h0')
    expect(api.dispatchWithEdl).not.toHaveBeenCalled()
    expect(useStore.getState().edl).toBe(EDL0)         // not from the answer
    await flush(200)
    expect(api.getEDL).toHaveBeenCalledTimes(1)
    expect(previewController()).toBeNull()
    expect(calls).toEqual([])
  })

  it('play/pause only flips the flag (the <video> path plays)', () => {
    useStore.getState().setPlaying(true)
    expect(useStore.getState().isPlaying).toBe(true)
    expect(calls).toEqual([])
  })

  it('renderPreview() renders at normal priority', async () => {
    await useStore.getState().renderPreview()
    expect(api.preview).toHaveBeenCalledTimes(1)
    expect(api.previewLow).not.toHaveBeenCalled()
  })
})

describe('client mode', () => {
  beforeEach(() => {
    toMode('client')
    calls.length = 0
    ;(api.getEDL as ReturnType<typeof vi.fn>).mockClear()
  })

  it('creates one controller per project and feeds it the EDL on screen', () => {
    toMode('server')
    expect(calls).toEqual([['dispose']])
    toMode('client')
    expect(calls[1]).toEqual(['new', 's_1'])
    expect(calls[2][0]).toBe('applyTimeline')
    expect(calls[2][1]).toBeNull()                      // no hash yet: the controller asks
    expect(previewController()?.sessionId).toBe('s_1')
  })

  it('applies the EDL of the dispatch answer IMMEDIATELY, then refreshes only the session fields', async () => {
    const res = await useStore.getState().dispatch('trim_clip', { clip_id: 'c1', out: 2 })
    expect(api.dispatchWithEdl).toHaveBeenCalledWith('s_1', 'trim_clip', { clip_id: 'c1', out: 2 }, 'h0')
    expect(api.dispatch).not.toHaveBeenCalled()
    expect(res?.edl_hash).toBe('cli1')
    // before any refresh: the store and the engine have the post-op EDL
    expect(useStore.getState().edl).toEqual(EDL1)
    expect(useStore.getState().edlHash).toBe('cli1')
    const applied = calls.filter((c) => c[0] === 'applyTimeline')
    expect(applied).toHaveLength(1)
    expect(applied[0][1]).toBe('aaaaaaaaaaaaaaaa')
    await flush(200)
    expect(api.getSession).toHaveBeenCalled()
    expect(api.getEDL).not.toHaveBeenCalled()
    expect(useStore.getState().redoAvailable).toBe(true)
    // the store's own EDL change did not come back as an "EDL from elsewhere"
    expect(calls.filter((c) => c[0] === 'applyTimeline')).toHaveLength(1)
  })

  it('undo: the EDL of the answer, then an immediate session-only refresh', async () => {
    await useStore.getState().dispatch('undo')
    expect(api.dispatchWithEdl).toHaveBeenCalledTimes(1)
    expect(api.getSession).toHaveBeenCalledTimes(1)
    expect(api.getEDL).not.toHaveBeenCalled()
    expect(useStore.getState().edl).toEqual(EDL1)
  })

  it('Space / click / L: setPlaying(true) starts the engine synchronously from the playhead', () => {
    useStore.setState({ playhead: 1.5 })
    useStore.getState().setPlaying(true)
    // same call stack: nothing awaited between the gesture and the engine
    expect(calls.at(-1)).toEqual(['play', 1.5])
    expect(useStore.getState().isPlaying).toBe(true)
    useStore.getState().setPlaying(false)
    expect(calls.at(-1)).toEqual(['pause'])
    expect(useStore.getState().isPlaying).toBe(false)
  })

  it('reverse shuttle does not start (Phase 4); the engine owns the play flag after that', () => {
    useStore.setState({ playbackRate: -1 })
    useStore.getState().setPlaying(true)
    expect(useStore.getState().isPlaying).toBe(false)
    expect(calls.some((c) => c[0] === 'play')).toBe(false)
    useStore.setState({ playbackRate: 1 })
    ctl().opts.onPlaying(true)          // e.g. resumed after the window came back
    expect(useStore.getState().isPlaying).toBe(true)
    ctl().opts.onPlaying(false)         // e.g. WebKit paused it: no pause() call back into the engine
    expect(useStore.getState().isPlaying).toBe(false)
    expect(calls.some((c) => c[0] === 'pause')).toBe(false)
  })

  it('J while playing forward stops the engine (reverse is Phase 4), never leaves it running under a reverse rate', () => {
    useStore.getState().setPlaying(true)
    expect(calls.at(-1)?.[0]).toBe('play')
    // shuttleReverse: setPlaybackRate(-1) then setPlaying(true), same handler
    useStore.getState().setPlaybackRate(-1)
    useStore.getState().setPlaying(true)
    expect(calls.at(-1)).toEqual(['pause'])
    expect(useStore.getState().isPlaying).toBe(false)
  })

  it('tells the controller when a preview render lands (bakes)', () => {
    useStore.setState({ previewHash: 'bbbbbbbbbbbbbbbb' })
    expect(calls.at(-1)).toEqual(['onPreviewLanded', 'bbbbbbbbbbbbbbbb'])
  })

  it('renders the background preview niced when asked', async () => {
    await useStore.getState().renderPreview({ priority: 'low' })
    expect(api.previewLow).toHaveBeenCalledTimes(1)
    expect(api.preview).not.toHaveBeenCalled()
  })

  it('an engine that cannot run falls the project back to the server preview', () => {
    useStore.setState({ previewSettings: { engine: 'client', source: 'settings', eagerProxies: false } })
    ctl().opts.onFallback('webgl-lost')
    expect(useStore.getState().previewEngine).toBe('server')
    expect(useStore.getState().previewEngineReason).toBe('webgl-lost')
    expect(calls.at(-1)).toEqual(['dispose'])
    expect(previewController()).toBeNull()
  })
})

describe('the setting', () => {
  it('Off / Auto / Always resolve against capabilities and the project rate', async () => {
    const { setPreviewEngineSetting } = useStore.getState()
    await setPreviewEngineSetting('server')
    expect(useStore.getState().previewEngine).toBe('server')
    await setPreviewEngineSetting('client')
    expect(api.setPreviewEngine).toHaveBeenLastCalledWith('client')
    expect(useStore.getState().previewEngine).toBe('client')
    await setPreviewEngineSetting('auto')
    expect(useStore.getState().previewEngine).toBe('client')
    // a rate MSE cannot hold on its 240 kHz grid (R1): the server preview
    useStore.setState({ edl: { ...EDL0, canvas: { ...EDL0.canvas, fps: 29.5 } } as never })
    expect(useStore.getState().previewEngine).toBe('server')
    expect(useStore.getState().previewEngineReason).toBe('rate')
    useStore.setState({ edl: EDL0 as never })
    expect(useStore.getState().previewEngine).toBe('client')
    await setPreviewEngineSetting('server')
    expect(useStore.getState().previewEngine).toBe('server')
    expect(previewController()).toBeNull()
  })
})

// Final QA: with Instant preview on the shuttle's rates are Phase 4 — the
// client engine plays at 1× and never in reverse. The store used to CLAIM 2×
// (and StickerLayer animated at it) while the picture ran at 1×, and J did
// nothing at all, with no word to the user.
describe('client mode: the shuttle says what it cannot do yet', () => {
  beforeEach(() => {
    toMode('client')
    calls.length = 0
    useToasts.setState({ toasts: [] })
  })
  const shuttleToasts = () => useToasts.getState().toasts.filter((t) => /Instant preview/.test(t.message))

  it('L L keeps the rate at 1× (what actually plays) and says why', () => {
    useStore.getState().setPlaybackRate(1)
    useStore.getState().setPlaying(true)
    useStore.getState().setPlaybackRate(2)
    expect(useStore.getState().playbackRate).toBe(1)
    expect(useStore.getState().isPlaying).toBe(true)
    expect(shuttleToasts()).toHaveLength(1)
  })

  it('J says why it does not play backwards; repeated presses do not stack toasts', () => {
    useStore.getState().setPlaybackRate(-1)
    useStore.getState().setPlaying(true)
    expect(useStore.getState().isPlaying).toBe(false)
    useStore.getState().setPlaybackRate(-2)
    expect(shuttleToasts()).toHaveLength(1)
  })

  it('server mode honours the rate with no toast', () => {
    toMode('server')
    useStore.getState().setPlaybackRate(2)
    expect(useStore.getState().playbackRate).toBe(2)
    expect(shuttleToasts()).toHaveLength(0)
  })
})

// Final QA: switching Instant preview ON while the server preview plays left
// the store saying Playing over an engine that never started (frozen picture,
// and the next Space only "paused").
describe('switching engines while playing', () => {
  it('server → client while playing: the transport agrees with the engine (stopped)', () => {
    useStore.getState().setPlaying(true)
    expect(useStore.getState().isPlaying).toBe(true)
    toMode('client')
    expect(calls.some((c) => c[0] === 'play')).toBe(false)
    expect(useStore.getState().isPlaying).toBe(false)
    // and the next Space plays
    useStore.getState().setPlaying(true)
    expect(calls.at(-1)?.[0]).toBe('play')
    expect(useStore.getState().isPlaying).toBe(true)
  })

  it('J in server mode, then Instant preview on: the Play button plays (Final QA r2)', () => {
    // shuttleReverse in server mode: the server preview really plays backwards
    useStore.getState().setPlaybackRate(-1)
    useStore.getState().setPlaying(true)
    expect(useStore.getState().playbackRate).toBe(-1)
    toMode('client')
    // The leftover -1 made setPlaying refuse every Play click and the first
    // Space, silently — and a voice-over take would start over a stopped
    // timeline (VoRecorder.beginTake calls setPlaying only).
    expect(useStore.getState().playbackRate).toBe(1)
    useStore.getState().setPlaying(true)                // the timeline's Play button
    expect(calls.at(-1)?.[0]).toBe('play')
    expect(useStore.getState().isPlaying).toBe(true)
  })
})
