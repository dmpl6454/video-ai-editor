// lib/captionRun: the captions run lifted out of the top bar's CaptionsButton
// (LEFT_RAIL_SPEC §6.2, risk 5). Its behaviours are ported here as cases
// against the real run with injected edges (dispatch, the job API, timers),
// so the panel and the activity chip cannot drift from what the button did.
import { beforeEach, describe, expect, it, vi } from 'vitest'

// store.ts (imported for the app's singleton) reads storage at import time;
// Node's `localStorage` is a stub without getItem (see TopBar.test).
vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {}, removeItem: () => {} })
const {
  captionArgs, createCaptionRun, doneMessage, etaSeconds, formatEta, hasCaptionFootage, loadSpeed, loadTarget,
  speedDownload, SPEED_KEY, TARGET_KEY, TURBO_MODEL,
} = await import('./captionRun')
type Deps = Parameters<typeof createCaptionRun>[0]

const REPORT = {
  'captions:large-v3': { what: 'the accurate caption model', bytes: 3.1e9, cached: true },
  'captions:large-v3-turbo': { what: 'the fast caption model', bytes: 1.6e9, cached: false },
}

/** A controllable auto_caption job: progress() and finish() drive it. */
function harness(over: Partial<Deps> = {}) {
  const kv = new Map<string, string>()
  let resolveJob: ((v: { result: unknown } | null) => void) | null = null
  let onProgress: ((p: { jobId: string; progress: number }) => void) | null = null
  let clock = 10_000
  let tick: (() => void) | null = null
  const calls = {
    dispatch: [] as { tool: string; args: Record<string, unknown> }[],
    cancel: [] as string[],
    toastOk: [] as string[],
    toastErr: [] as string[],
    published: [] as unknown[],
  }
  const deps: Deps = {
    dispatch: (tool, args, opts) => {
      calls.dispatch.push({ tool, args })
      onProgress = opts.onProgress
      return new Promise((r) => { resolveJob = r })
    },
    getDownloads: async () => REPORT,
    cancelJob: async (id) => { calls.cancel.push(id) },
    hasFootage: () => true,
    toast: { success: (m) => calls.toastOk.push(m), error: (m) => calls.toastErr.push(m) },
    storage: { getItem: (k) => kv.get(k) ?? null, setItem: (k, v) => { kv.set(k, v) } },
    now: () => clock,
    every: (fn) => { tick = fn; return () => { tick = null } },
    publish: (c, outcome) => { calls.published.push(c ? { ...c, cancel: 'fn' } : { end: outcome }) },
    ...over,
  }
  const run = createCaptionRun(deps)
  const flush = () => new Promise((r) => setTimeout(r, 0))
  /** Starts a run without awaiting the job (it resolves only on finish()). */
  const begin = async () => { void run.getState().run(); await flush(); await flush() }
  const beginStart = async () => { void run.getState().start(); await flush() }
  return {
    run, kv, calls, flush, begin, beginStart,
    progress: (p: number, jobId = 'job-1') => onProgress?.({ jobId, progress: p }),
    finish: async (v: { result: unknown } | null) => { resolveJob?.(v); await flush(); await flush() },
    advance: (ms: number) => { clock += ms; tick?.() },
    ticking: () => tick !== null,
  }
}

describe('the pure pieces (verbatim from CaptionsButton)', () => {
  it('etaSeconds is null until there is honest signal, then extrapolates', () => {
    expect(etaSeconds(2, 0.5)).toBeNull()        // under 3 s
    expect(etaSeconds(10, 0.02)).toBeNull()      // under 2 %
    expect(etaSeconds(20, 0.4)).toBe(30)
    expect(etaSeconds(99, 0.995)).toBeNull()     // ≤ 1 s left
  })
  it('formatEta', () => {
    expect(formatEta(29.2)).toBe('30s left')
    expect(formatEta(95)).toBe('~2 min left')
    expect(formatEta(70)).toBe('~1 min left')
  })
  it('captionArgs sends nothing for the defaults (the backend\'s own defaults)', () => {
    expect(captionArgs('as-spoken', 'quality')).toEqual({})
    expect(captionArgs('hinglish', 'quality')).toEqual({ target: 'hinglish' })
    expect(captionArgs('as-spoken', 'fast')).toEqual({ model: TURBO_MODEL })
    expect(captionArgs('en', 'fast')).toEqual({ target: 'en', model: 'large-v3-turbo' })
  })
  it('loads the remembered choices, falling back on junk or a throwing storage', () => {
    const kv = (m: Record<string, string>) => ({ getItem: (k: string) => m[k] ?? null, setItem() {} })
    expect(loadTarget(kv({ [TARGET_KEY]: 'hi' }))).toBe('hi')
    expect(loadTarget(kv({ [TARGET_KEY]: 'klingon' }))).toBe('as-spoken')
    expect(loadSpeed(kv({ [SPEED_KEY]: 'fast' }))).toBe('fast')
    expect(loadSpeed({ getItem: () => { throw new Error('denied') }, setItem() {} })).toBe('quality')
    expect(loadTarget(null)).toBe('as-spoken')
  })
  it('doneMessage names the language, the source and a model fall-back', () => {
    expect(doneMessage({ cues: 18, language: 'en' }, 'quality')).toBe('Captions added — 18 cues (en)')
    expect(doneMessage({ cues: 4, language: 'hi-Latn', spoken: 'hi' }, 'quality')).toBe('Captions added — 4 cues (Hinglish from hi)')
    expect(doneMessage({ cues: 7, language: 'en', spoken: 'zh', model: 'large-v3' }, 'fast'))
      .toBe("Captions added — 7 cues (en from zh) · used large-v3 (turbo can't translate)")
    expect(doneMessage({ cues: 7, language: 'es', model: 'large-v3-turbo' }, 'fast')).toBe('Captions added — 7 cues (Spanish)')
    expect(doneMessage(null, 'quality')).toBe('Captions added')
  })
  it('speedDownload: only an uncached model wears a badge', () => {
    expect(speedDownload(REPORT, 'fast')).toEqual(REPORT['captions:large-v3-turbo'])
    expect(speedDownload(REPORT, 'quality')).toBeNull()
    expect(speedDownload(null, 'fast')).toBeNull()
  })
  it('hasCaptionFootage: a media clip on v1, nothing else', () => {
    const edl = (clips: unknown[]) => ({ tracks: [{ id: 'v1', clips }] }) as never
    expect(hasCaptionFootage(null)).toBe(false)
    expect(hasCaptionFootage(edl([]))).toBe(false)
    expect(hasCaptionFootage(edl([{ id: 'c', src: '/a.mp4', in: 0, out: 1, start: 0 }]))).toBe(true)
  })
})

describe('the run', () => {
  let h: ReturnType<typeof harness>
  beforeEach(() => { h = harness() })

  it('remembers the language and speed picks', () => {
    h.run.getState().pickTarget('es')
    h.run.getState().pickSpeed('fast')
    expect(h.kv.get(TARGET_KEY)).toBe('es')
    expect(h.kv.get(SPEED_KEY)).toBe('fast')
    expect(harness({ storage: { getItem: (k) => h.kv.get(k) ?? null, setItem() {} } }).run.getState())
      .toMatchObject({ target: 'es', speed: 'fast' })
  })

  it('runs auto_caption with the chosen args, reports progress, toasts the result', async () => {
    h.run.getState().pickTarget('en')
    await h.begin()
    expect(h.calls.dispatch).toEqual([{ tool: 'auto_caption', args: { target: 'en' } }])
    expect(h.run.getState()).toMatchObject({ busy: true, progress: 0 })
    h.progress(0)
    h.advance(20_000)
    h.progress(0.4)
    expect(h.run.getState()).toMatchObject({ jobId: 'job-1', progress: 0.4, elapsed: 20 })
    // The chip's mirror: % and an honest ETA.
    expect(h.calls.published.at(-1)).toMatchObject({ progress: 0.4, etaS: 30, elapsedS: 20, cancelling: false })
    await h.finish({ result: { cues: 12, language: 'en' } })
    expect(h.calls.toastOk).toEqual(['Captions added — 12 cues (en)'])
    expect(h.run.getState()).toMatchObject({ busy: false, progress: 0, jobId: null, cancelling: false })
    expect(h.calls.published.at(-1)).toEqual({ end: 'done' })
    expect(h.ticking()).toBe(false)
  })

  it('cancel → "Stopping…" until the job reports cancelled (then no success toast)', async () => {
    await h.begin()
    h.progress(0.1)
    await h.run.getState().cancel()
    expect(h.calls.cancel).toEqual(['job-1'])
    expect(h.run.getState().cancelling).toBe(true)
    // The decoder stops only between segments: the job keeps running, the
    // elapsed counter keeps counting and the state stays "Stopping…".
    h.advance(30_000)
    h.progress(0.12)
    expect(h.run.getState()).toMatchObject({ busy: true, cancelling: true, elapsed: 30 })
    expect(h.calls.published.at(-1)).toMatchObject({ cancelling: true, etaS: null })
    await h.finish(null)   // store.dispatch resolves null: "Auto captions was cancelled" is its toast
    expect(h.calls.toastOk).toEqual([])
    expect(h.run.getState()).toMatchObject({ busy: false, cancelling: false })
    expect(h.calls.published.at(-1)).toEqual({ end: 'cancelled' })
  })

  it('a cancel the server refuses returns to running and says so', async () => {
    h = harness({ cancelJob: async () => { throw new Error('404') } })
    await h.begin()
    h.progress(0.2)
    await h.run.getState().cancel()
    expect(h.run.getState()).toMatchObject({ busy: true, cancelling: false })
    expect(h.calls.toastErr).toEqual(['Could not cancel — it may have already finished'])
  })

  it('cancel before the job id is known does nothing (no job to cancel yet)', async () => {
    await h.begin()
    await h.run.getState().cancel()
    expect(h.calls.cancel).toEqual([])
    expect(h.run.getState().cancelling).toBe(false)
  })

  it('a failed run is announced as failed, not done', async () => {
    await h.begin()
    await h.finish(null)
    expect(h.calls.published.at(-1)).toEqual({ end: 'failed' })
  })

  it('Fastest with its model missing asks first and sends nothing; the yes starts it', async () => {
    h.run.getState().pickSpeed('fast')
    await h.begin()
    expect(h.run.getState().consent).toEqual(REPORT['captions:large-v3-turbo'])
    expect(h.calls.dispatch).toEqual([])
    expect(h.run.getState().busy).toBe(false)
    h.run.getState().dismissConsent()
    expect(h.run.getState().consent).toBeNull()
    expect(h.calls.dispatch).toEqual([])
    await h.begin()
    h.run.getState().acceptConsent()
    await h.flush()
    expect(h.run.getState().consent).toBeNull()
    expect(h.calls.dispatch).toEqual([{ tool: 'auto_caption', args: { model: 'large-v3-turbo' } }])
  })

  it('Best quality with its model cached starts at once', async () => {
    await h.begin()
    expect(h.run.getState().consent).toBeNull()
    expect(h.calls.dispatch).toHaveLength(1)
  })

  it('no footage on v1 → no run at all; a second run while busy is ignored', async () => {
    const none = harness({ hasFootage: () => false })
    await none.begin()
    await none.beginStart()
    expect(none.calls.dispatch).toEqual([])
    await h.begin()
    await h.begin()
    await h.beginStart()
    expect(h.calls.dispatch).toHaveLength(1)
  })

  it('re-reads the download report after a run (a model just fetched loses its badge)', async () => {
    let reads = 0
    h = harness({ getDownloads: async () => { reads++; return REPORT } })
    await h.begin()
    expect(reads).toBe(1)
    await h.finish({ result: { cues: 1 } })
    expect(reads).toBe(2)
    expect(h.run.getState().downloads).toEqual(REPORT)
  })

  it('an unreachable download report does not block the run', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    h = harness({ getDownloads: async () => { throw new Error('offline') } })
    await h.begin()
    expect(h.calls.dispatch).toHaveLength(1)
    expect(warn).toHaveBeenCalled()
    warn.mockRestore()
  })
})
