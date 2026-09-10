// The store's side effects around the pure reducer: history, the `op` →
// refresh + preview hook, clarify → answer, cancel-is-a-POST, a dropped
// stream reconnecting to the run it lost, and a 409 attaching to the run
// that holds the lock. Every stream is a real `Response` built from the
// recorded fixture, so the reader path is the production one.
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const FIXTURE = readFileSync(fileURLToPath(new URL('./__fixtures__/prompt_stream.txt', import.meta.url)), 'utf-8')
function turn(name: string): string {
  const parts = FIXTURE.split(/^: turn /m).filter(Boolean)
  const hit = parts.find((p) => p.startsWith(name))
  if (!hit) throw new Error(`no turn ${name}`)
  return hit.slice(hit.indexOf('\n') + 1)
}
const sseResponse = (text: string) => new Response(text, { headers: { 'content-type': 'text/event-stream' } })

// --- module mocks -----------------------------------------------------------

const refresh = vi.fn(async () => undefined)
const renderPreview = vi.fn(async () => 'hash')
vi.mock('../store', () => ({
  useStore: {
    getState: () => ({ sessionId: 's_test', selection: 'c_1', multiSelection: [], playhead: 4.5, refresh, renderPreview }),
  },
  errorMessage: (e: unknown) => (e instanceof Error ? e.message : String(e)),
}))

const toastCalls: string[] = []
vi.mock('../toast', () => ({
  toast: {
    info: (m: string) => toastCalls.push(`info:${m}`),
    error: (m: string) => toastCalls.push(`error:${m}`),
    success: (m: string) => toastCalls.push(`success:${m}`),
    action: (m: string) => toastCalls.push(`action:${m}`),
  },
}))

const apiMock = {
  promptStream: vi.fn<(sid: string, body: unknown) => Promise<Response>>(),
  promptAnswer: vi.fn<(sid: string, token: string, answers: unknown) => Promise<Response>>(),
  promptCancel: vi.fn(async () => ({ cancelled: true })),
  promptRun: vi.fn<(sid: string) => Promise<unknown>>(),
  promptPending: vi.fn<(sid: string) => Promise<unknown>>(),
  promptBrains: vi.fn(async () => ({ brains: [{ id: 'recipes', available: true, detail: 'grammar' }] })),
}
vi.mock('../api', () => ({ api: apiMock }))

// A localStorage stand-in — the suite runs in `environment: 'node'`.
const storage = new Map<string, string>()
;(globalThis as { localStorage?: unknown }).localStorage = {
  getItem: (k: string) => storage.get(k) ?? null,
  setItem: (k: string, v: string) => { storage.set(k, v) },
  removeItem: (k: string) => { storage.delete(k) },
}

const { firePromptRunning } = await import('./promptEvents')
const { usePromptStore, pushHistory, isBusy, HISTORY_MAX, HISTORY_KEY, _detachPromptStream } = await import('./promptStore')

const flush = () => new Promise((r) => setTimeout(r, 0))

beforeEach(() => {
  refresh.mockClear(); renderPreview.mockClear(); toastCalls.length = 0
  for (const fn of Object.values(apiMock)) fn.mockClear()
  apiMock.promptRun.mockResolvedValue({ run: null, live: false })
  apiMock.promptPending.mockResolvedValue({ pending: null })
  _detachPromptStream()
  usePromptStore.setState({
    status: 'idle', brain: null, attempts: [], plan: null, steps: [], verify: null, reply: '', clarify: null,
    lastError: null, opSeen: false, unknownEvents: 0, sid: null, runId: null, prompt: '', history: [],
    chatBusy: false, logOpen: false, connectionDropped: false, reconnecting: false,
  })
})
afterEach(() => { storage.clear() })

describe('history', () => {
  it('is newest-first, de-duplicated and capped at HISTORY_MAX', () => {
    let h: string[] = []
    for (let i = 0; i < HISTORY_MAX + 5; i++) h = pushHistory(h, `prompt ${i}`)
    expect(h).toHaveLength(HISTORY_MAX)
    expect(h[0]).toBe(`prompt ${HISTORY_MAX + 4}`)
    expect(pushHistory(['a', 'b'], 'b')).toEqual(['b', 'a'])
    expect(pushHistory(['a'], '   ')).toEqual(['a'])
  })
  it('run() records the prompt before the request and persists it', async () => {
    apiMock.promptStream.mockResolvedValue(sseResponse(turn('tighten_and_captions')))
    await usePromptStore.getState().run('  remove the ums and add captions ')
    expect(usePromptStore.getState().history).toEqual(['remove the ums and add captions'])
    expect(JSON.parse(storage.get(HISTORY_KEY)!)).toEqual(['remove the ums and add captions'])
  })
})

describe('run()', () => {
  it('posts the editor UI state with the prompt, folds the stream, and refreshes on op', async () => {
    apiMock.promptStream.mockResolvedValue(sseResponse(turn('tighten_and_captions')))
    await usePromptStore.getState().run('remove the ums and add captions')
    expect(apiMock.promptStream).toHaveBeenCalledWith('s_test', {
      message: 'remove the ums and add captions', selection: 'c_1', multi_selection: [], playhead: 4.5,
    })
    const s = usePromptStore.getState()
    expect(s.status).toBe('done')
    expect(s.sid).toBe('s_test')
    expect(s.runId).toBe('p_3f9a1c2e')
    expect(s.brain?.brain).toBe('local_model')
    expect(s.verify?.passed).toBe(4)
    expect(s.logOpen).toBe(true)
    expect(s.connectionDropped).toBe(false)
    await flush()
    expect(refresh).toHaveBeenCalledTimes(1)
    expect(renderPreview).toHaveBeenCalledTimes(1)
  })

  it('refuses to start while busy or while chat is streaming, and with an empty prompt', async () => {
    usePromptStore.setState({ status: 'running' })
    await usePromptStore.getState().run('anything')
    usePromptStore.setState({ status: 'idle', chatBusy: true })
    await usePromptStore.getState().run('anything')
    usePromptStore.setState({ chatBusy: false })
    await usePromptStore.getState().run('   ')
    expect(apiMock.promptStream).not.toHaveBeenCalled()
    expect(isBusy('verifying')).toBe(true)
    expect(isBusy('clarify')).toBe(false)
  })

  it('a non-2xx on open ends in `error` with the server message', async () => {
    apiMock.promptStream.mockRejectedValue(new Error('422 Unprocessable Entity: {"error":{"message":"no clip on v1"}}'))
    await usePromptStore.getState().run('add captions')
    expect(usePromptStore.getState().status).toBe('error')
    expect(usePromptStore.getState().lastError).toMatch(/no clip on v1/)
  })

  it('a 409 prompt_running attaches to the run that holds the lock instead of failing', async () => {
    apiMock.promptStream
      .mockRejectedValueOnce(new Error('409 Conflict: {"error":{"code":"CONFLICT","message":"request failed","details":{"code":"prompt_running","run_id":"p_3f9a1c2e"}}}'))
      .mockResolvedValueOnce(sseResponse(turn('tighten_and_captions')))
    apiMock.promptRun.mockResolvedValue({ run: { run_id: 'p_3f9a1c2e', status: 'running', prompt: 'tighten it', steps: [] }, live: true })
    await usePromptStore.getState().run('add music')
    expect(toastCalls).toContain('info:A prompt is already running — showing it')
    expect(apiMock.promptStream).toHaveBeenLastCalledWith('s_test', { message: '', resume_run: 'p_3f9a1c2e' })
    const s = usePromptStore.getState()
    expect(s.status).toBe('done')
    expect(s.prompt).toBe('tighten it')
  })
})

describe('clarify → answer', () => {
  it('pauses on the question, then posts the coerced answers and consumes the resumed run', async () => {
    apiMock.promptStream.mockResolvedValue(sseResponse(turn('clarify_downloads')))
    await usePromptStore.getState().run('add hindi captions')
    let s = usePromptStore.getState()
    expect(s.status).toBe('clarify')
    expect(s.clarify?.token).toBe('clr_5e6f7a8b')
    expect(s.plan?.downloads_needed?.[0].what).toBe('MADLAD translation model')

    apiMock.promptAnswer.mockResolvedValue(sseResponse(turn('tighten_and_captions')))
    await usePromptStore.getState().answer({ downloads: 'Skip' })
    expect(apiMock.promptAnswer).toHaveBeenCalledWith('s_test', 'clr_5e6f7a8b', { downloads: 'no' })
    s = usePromptStore.getState()
    expect(s.status).toBe('done')
    expect(s.clarify).toBeNull()
    expect(s.prompt).toBe('add hindi captions')      // same turn, answered
  })

  it('refuses an incomplete answer without touching the network', async () => {
    apiMock.promptStream.mockResolvedValue(sseResponse(turn('clarify_downloads')))
    await usePromptStore.getState().run('add hindi captions')
    await usePromptStore.getState().answer({})
    expect(apiMock.promptAnswer).not.toHaveBeenCalled()
    expect(usePromptStore.getState().status).toBe('clarify')
    expect(usePromptStore.getState().lastError).toMatch(/unanswered: downloads/)
  })

  it('dropClarify cancels the pending token and returns to idle', async () => {
    apiMock.promptStream.mockResolvedValue(sseResponse(turn('clarify_downloads')))
    await usePromptStore.getState().run('add hindi captions')
    await usePromptStore.getState().dropClarify()
    expect(apiMock.promptCancel).toHaveBeenCalledWith('s_test', 'clr_5e6f7a8b')
    expect(usePromptStore.getState().status).toBe('idle')
    expect(usePromptStore.getState().clarify).toBeNull()
  })
})

describe('cancel and disconnect are different things', () => {
  it('cancel() is a POST; the outcome arrives on the stream as the server sentence', async () => {
    const failing = turn('step_failure').replace(
      'Step 1/2 auto_reframe failed: cv2 not importable. Timeline unchanged; transcript restored.',
      'Cancelled — timeline unchanged.')
    let release: (r: Response) => void = () => undefined
    apiMock.promptStream.mockReturnValue(new Promise<Response>((r) => { release = r }))
    const running = usePromptStore.getState().run('make it vertical')
    await flush()
    expect(usePromptStore.getState().status).toBe('planning')
    await usePromptStore.getState().cancel()
    expect(apiMock.promptCancel).toHaveBeenCalledWith('s_test')
    release(sseResponse(failing))
    await running
    expect(usePromptStore.getState().status).toBe('error')
    expect(usePromptStore.getState().lastError).toBe('Cancelled — timeline unchanged.')
  })

  it('a stream that ends before `done` is a drop: reconnect and replay, never a verdict', async () => {
    const full = turn('tighten_and_captions')
    const cut = full.slice(0, full.indexOf('data: {"type": "step", "index": 2'))
    apiMock.promptStream
      .mockResolvedValueOnce(sseResponse(cut))
      .mockResolvedValueOnce(sseResponse(full))
    apiMock.promptRun.mockResolvedValue({ run: { run_id: 'p_3f9a1c2e', status: 'running', prompt: 'remove the ums and add captions' }, live: true })
    await usePromptStore.getState().run('remove the ums and add captions')
    expect(apiMock.promptRun).toHaveBeenCalledWith('s_test')
    expect(apiMock.promptStream).toHaveBeenCalledTimes(2)
    expect(apiMock.promptStream.mock.calls[1][1]).toEqual({ message: '', resume_run: 'p_3f9a1c2e' })
    expect(apiMock.promptCancel).not.toHaveBeenCalled()
    const s = usePromptStore.getState()
    expect(s.status).toBe('done')
    expect(s.steps).toHaveLength(4)          // rebuilt from the replay, not doubled
    expect(s.connectionDropped).toBe(false)
  })

  it('dismiss() while running hides the log but does not cancel; idle dismiss clears the run', async () => {
    let release: (r: Response) => void = () => undefined
    apiMock.promptStream.mockReturnValue(new Promise<Response>((r) => { release = r }))
    const running = usePromptStore.getState().run('x')
    await flush()
    usePromptStore.getState().dismiss()
    expect(usePromptStore.getState().logOpen).toBe(false)
    expect(usePromptStore.getState().status).toBe('planning')
    expect(apiMock.promptCancel).not.toHaveBeenCalled()
    release(sseResponse(turn('tighten_and_captions')))
    await running
    expect(usePromptStore.getState().status).toBe('done')
    usePromptStore.getState().dismiss()
    expect(usePromptStore.getState().status).toBe('idle')
    expect(usePromptStore.getState().steps).toEqual([])
  })
})

describe('reconnect from prompt_run.json', () => {
  it('hydrates a finished run into the log when nothing newer is on screen', async () => {
    apiMock.promptRun.mockResolvedValue({ run: {
      run_id: 'p_aaaa0001', status: 'done', prompt: 'add captions', brain: 'recipes',
      steps: [
        { index: 0, total: 1, tool: 'add_caption_track', status: 'running' },
        { index: 0, total: 1, tool: 'add_caption_track', status: 'ok', summary: 'Laid 9 cues' },
      ],
      verify: { plan_id: 'p_aaaa0001', checks: [{ check: 'captions_nonempty', human: 'captions were laid', pass: true, headline: true }], passed: 1, total: 1, rendered: false },
      reply: 'via Recipes — 1 of 1 checks passed.', op: { seq: 3 },
    }, live: false, replayable: false })
    await usePromptStore.getState().reconnect('s_test')
    const s = usePromptStore.getState()
    expect(s.status).toBe('done')
    expect(s.runId).toBe('p_aaaa0001')
    expect(s.prompt).toBe('add captions')
    expect(s.brain?.brain).toBe('recipes')
    expect(s.steps).toEqual([{ index: 0, total: 1, tool: 'add_caption_track', status: 'ok', summary: 'Laid 9 cues',
                               progress: undefined, effect: undefined, error: undefined }])
    expect(s.verify?.passed).toBe(1)
    expect(s.opSeen).toBe(true)
    expect(s.logOpen).toBe(true)
    expect(apiMock.promptStream).not.toHaveBeenCalled()
  })

  it('leaves a newer local run alone and treats 404 as "never ran"', async () => {
    apiMock.promptStream.mockResolvedValue(sseResponse(turn('tighten_and_captions')))
    await usePromptStore.getState().run('remove the ums and add captions')
    apiMock.promptRun.mockResolvedValue({ run: { run_id: 'p_older', status: 'done', reply: 'old' }, live: false })
    await usePromptStore.getState().reconnect('s_test')
    expect(usePromptStore.getState().runId).toBe('p_3f9a1c2e')

    usePromptStore.getState().dismiss()
    apiMock.promptRun.mockRejectedValue(new Error('404 Not Found: {"error":{"message":"no run"}}'))
    await usePromptStore.getState().reconnect('s_test')
    expect(usePromptStore.getState().status).toBe('idle')
    // The route's own "never ran" answer, and a stub that is not the envelope at all.
    apiMock.promptRun.mockResolvedValue({ run: null, live: false, replayable: false })
    await usePromptStore.getState().reconnect('s_test')
    expect(usePromptStore.getState().status).toBe('idle')
    apiMock.promptRun.mockResolvedValue({ id: 's_test' })
    await usePromptStore.getState().reconnect('s_test')
    expect(usePromptStore.getState().status).toBe('idle')
    expect(apiMock.promptStream).toHaveBeenCalledTimes(1)
  })

  it('a record still "running" that the process no longer holds is reported as interrupted, not replayed', async () => {
    apiMock.promptRun.mockResolvedValue({
      run: { run_id: 'p_lost', status: 'running', prompt: 'make 3 shorts', brain: 'recipes',
             steps: [{ index: 0, total: 4, tool: 'remove_silences', status: 'ok', summary: 'Removed 2 silences' }] },
      live: false, replayable: false,
    })
    await usePromptStore.getState().reconnect('s_test')
    const s = usePromptStore.getState()
    expect(s.status).toBe('error')
    expect(s.lastError).toMatch(/interrupted — the server no longer has it/)
    expect(s.steps).toHaveLength(1)
    expect(s.logOpen).toBe(true)
    expect(apiMock.promptStream).not.toHaveBeenCalled()
  })

  it('a failed record carries the server error text', async () => {
    apiMock.promptRun.mockResolvedValue({
      run: { run_id: 'p_fail', status: 'failed', prompt: 'reframe', error: 'Step 1/2 auto_reframe failed: cv2 not importable. Timeline unchanged; transcript restored.' },
      live: false,
    })
    await usePromptStore.getState().reconnect('s_test')
    expect(usePromptStore.getState().status).toBe('error')
    expect(usePromptStore.getState().lastError).toMatch(/^Step 1\/2 auto_reframe failed/)
  })

  it('a pending clarification comes back as the card after a reload, with its token', async () => {
    const clarify = JSON.parse(turn('clarify_downloads').split('\n').find((l) => l.includes('"type": "clarify"'))!.slice(6))
    apiMock.promptPending.mockResolvedValue({ pending: {
      token: clarify.token, plan_id: clarify.plan_id, prompt: 'hindi captions', brain: 'recipes',
      questions: clarify.questions, expires_in_s: 412,
    } })
    apiMock.promptRun.mockResolvedValue({ run: { run_id: clarify.plan_id, status: 'clarify', prompt: 'hindi captions' }, live: false })
    await usePromptStore.getState().reconnect('s_test')
    const s = usePromptStore.getState()
    expect(s.status).toBe('clarify')
    expect(s.clarify?.token).toBe('clr_5e6f7a8b')
    expect(s.clarify?.questions[0].kind).toBe('confirm')
    expect(s.clarify?.expiresInS).toBe(412)
    expect(s.prompt).toBe('hindi captions')
    // Answering it posts the same token the server issued.
    apiMock.promptAnswer.mockResolvedValue(sseResponse(turn('tighten_and_captions')))
    await usePromptStore.getState().answer({ downloads: 'yes' })
    expect(apiMock.promptAnswer).toHaveBeenCalledWith('s_test', 'clr_5e6f7a8b', { downloads: 'yes' })
  })

  it('does not restore a pending card over a run that is busy here', async () => {
    let release: (r: Response) => void = () => undefined
    apiMock.promptStream.mockReturnValue(new Promise<Response>((r) => { release = r }))
    const running = usePromptStore.getState().run('x')
    await flush()
    apiMock.promptPending.mockResolvedValue({ pending: { token: 'clr_x', questions: [{ key: 'k', question: 'q?', required: true }] } })
    expect(await usePromptStore.getState().restorePending('s_test')).toBe(false)
    expect(usePromptStore.getState().status).toBe('planning')
    release(sseResponse(turn('tighten_and_captions')))
    await running
  })

  it('loadBrains normalises the report and records a failure honestly', async () => {
    await usePromptStore.getState().loadBrains()
    expect(usePromptStore.getState().brains?.brains.map((b) => `${b.id}:${b.available}`)).toEqual([
      'recipes:true', 'apple_intelligence:false', 'local_model:false', 'claude:false',
    ])
    // A second plain call is served from what is held; only Recheck refetches.
    await usePromptStore.getState().loadBrains()
    expect(apiMock.promptBrains).toHaveBeenCalledTimes(1)
    apiMock.promptBrains.mockRejectedValueOnce(new Error('503 Service Unavailable: {}'))
    await usePromptStore.getState().loadBrains(true)
    expect(usePromptStore.getState().brainsError).toMatch(/503/)
    expect(apiMock.promptBrains).toHaveBeenLastCalledWith(true)
    expect(apiMock.promptBrains).toHaveBeenCalledTimes(2)
  })
})

describe('409 from /dispatch (store.ts → firePromptRunning)', () => {
  it('toasts once with Cancel and attaches the bar to the run holding the lock', async () => {
    apiMock.promptRun.mockResolvedValue({ run: { run_id: 'p_3f9a1c2e', status: 'running', prompt: 'tighten it' }, live: true, replayable: true })
    apiMock.promptStream.mockResolvedValue(sseResponse(turn('tighten_and_captions')))
    expect(firePromptRunning('s_test')).toBe(true)
    expect(toastCalls).toEqual(['action:Prompt running — wait or cancel'])
    await flush(); await flush()
    expect(apiMock.promptRun).toHaveBeenCalledWith('s_test')
    // Wait for the replay to be consumed.
    for (let i = 0; i < 20 && usePromptStore.getState().status !== 'done'; i++) await flush()
    expect(usePromptStore.getState().status).toBe('done')
    expect(usePromptStore.getState().prompt).toBe('tighten it')
  })
})
