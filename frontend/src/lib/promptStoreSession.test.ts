// QA-062: the prompt run log and pending question belong to ONE project.
//   * Clear must stick — through the op-log reconnect and through a reload.
//   * A new or switched project starts with an empty bar, not the previous
//     project's "Fit c_… → cover".
// QA-064: a cancelled run reads "Cancelled", says "Stopping…" while the
// current step finishes, and is never labelled "Failed".
//
// Runs the REAL prompt store against a mocked api and a store whose session
// can change, with the reload simulated by re-importing the modules.
import { beforeEach, describe, expect, it, vi } from 'vitest'

const ui = { sessionId: 's_A' }
vi.mock('../store', () => ({
  useStore: { getState: () => ({ ...ui, selection: null, multiSelection: [], playhead: 0,
                                 refresh: vi.fn(async () => undefined), renderPreview: vi.fn(async () => '') }) },
  errorMessage: (e: unknown) => (e instanceof Error ? e.message : String(e)),
}))
vi.mock('../toast', () => ({ toast: { info: vi.fn(), error: vi.fn(), success: vi.fn(), action: vi.fn() } }))

const apiMock = {
  promptStream: vi.fn(),
  promptAnswer: vi.fn(),
  promptCancel: vi.fn(async () => ({ cancelled: true })),
  promptRun: vi.fn(),
  promptPending: vi.fn(async () => ({ pending: null })),
  promptBrains: vi.fn(),
}
vi.mock('../api', () => ({ api: apiMock }))

const storage = new Map<string, string>()
;(globalThis as { localStorage?: unknown }).localStorage = {
  getItem: (k: string) => storage.get(k) ?? null,
  setItem: (k: string, v: string) => { storage.set(k, v) },
  removeItem: (k: string) => { storage.delete(k) },
}

const RUN_A = {
  run_id: 'p_A1', plan_id: 'p_A1', status: 'done', prompt: 'make it 9:16',
  steps: [{ index: 0, total: 1, tool: 'set_clip_fit', status: 'ok', summary: 'Fit c_1631352c → cover' }],
}

async function load() {
  const store = await import('./promptStore')
  const events = await import('./promptEvents')
  return { ...store, ...events }
}

beforeEach(() => {
  vi.resetModules()           // every test is a fresh page load
  ui.sessionId = 's_A'
  for (const f of Object.values(apiMock)) f.mockReset()
  apiMock.promptPending.mockResolvedValue({ pending: null })
  apiMock.promptCancel.mockResolvedValue({ cancelled: true })
})

describe('Clear sticks (QA-062)', () => {
  it('after Clear, neither the op-log reconnect nor a reload brings the run back', async () => {
    storage.clear()
    apiMock.promptRun.mockImplementation(async (sid: string) => (sid === 's_A' ? { run: RUN_A, live: false } : { run: null }))
    let { usePromptStore } = await load()
    await usePromptStore.getState().reconnect('s_A')
    expect(usePromptStore.getState().steps).toHaveLength(1)        // the finished run is shown
    usePromptStore.getState().dismiss()                           // Clear
    // PromptBar's effect: the last op is a `prompt` whose plan_id is no longer
    // runId (null after Clear) → reconnect.
    await usePromptStore.getState().reconnect('s_A')
    expect(usePromptStore.getState().steps).toEqual([])
    expect(usePromptStore.getState().logOpen).toBe(false)
    // Reload.
    vi.resetModules()
    ;({ usePromptStore } = await load())
    await usePromptStore.getState().reconnect('s_A')
    expect(usePromptStore.getState().steps).toEqual([])
    expect(usePromptStore.getState().status).toBe('idle')
  })

  it('a NEWER run on the same project still shows', async () => {
    storage.clear()
    apiMock.promptRun.mockResolvedValue({ run: RUN_A, live: false })
    const { usePromptStore } = await load()
    await usePromptStore.getState().reconnect('s_A')
    usePromptStore.getState().dismiss()
    apiMock.promptRun.mockResolvedValue({ run: { ...RUN_A, run_id: 'p_A2', plan_id: 'p_A2' }, live: false })
    await usePromptStore.getState().reconnect('s_A')
    expect(usePromptStore.getState().runId).toBe('p_A2')
  })
})

describe('a project switch starts the bar clean (QA-062)', () => {
  it('a new project with no runs does not show the previous project\'s log', async () => {
    storage.clear()
    apiMock.promptRun.mockImplementation(async (sid: string) => {
      if (sid === 's_A') return { run: RUN_A, live: false }
      throw new Error('404 Not Found: {"error":{"message":"no run"}}')
    })
    const { usePromptStore, fireSessionSwitch } = await load()
    await usePromptStore.getState().reconnect('s_A')
    expect(usePromptStore.getState().steps).toHaveLength(1)
    // New project from the picker: store.ts fires the switch, then the bar's
    // mount effect reconnects for the new session.
    ui.sessionId = 's_B'
    fireSessionSwitch('s_B')
    // Clean at once — not only after the (network) reconnect answers.
    expect(usePromptStore.getState().steps).toEqual([])
    expect(usePromptStore.getState().sid).toBe('s_B')
    await usePromptStore.getState().reconnect('s_B')
    const s = usePromptStore.getState()
    expect([s.sid, s.status, s.steps, s.prompt, s.logOpen]).toEqual(['s_B', 'idle', [], '', false])
  })

  it('even without the switch event, a 404 for another session resets the bar', async () => {
    storage.clear()
    apiMock.promptRun.mockImplementation(async (sid: string) => {
      if (sid === 's_A') return { run: RUN_A, live: false }
      throw new Error('404 Not Found')
    })
    const { usePromptStore } = await load()
    await usePromptStore.getState().reconnect('s_A')
    await usePromptStore.getState().reconnect('s_B')
    expect(usePromptStore.getState().steps).toEqual([])
    expect(usePromptStore.getState().sid).toBe('s_B')
  })
})

describe('cancel (QA-064)', () => {
  it('says "Stopping…" at once and ends as cancelled, not failed', async () => {
    storage.clear()
    const { usePromptStore, terminalAnnouncement } = await load()
    usePromptStore.setState({ status: 'running', sid: 's_A', runId: 'p_A1' })
    const p = usePromptStore.getState().cancel()
    expect(usePromptStore.getState().cancelling).toBe(true)
    await p
    usePromptStore.getState().applyEvent({ type: 'error', message: 'Cancelled — timeline unchanged.' } as never)
    usePromptStore.getState().applyEvent({ type: 'done' } as never)
    const s = usePromptStore.getState()
    expect([s.status, s.cancelling]).toEqual(['cancelled', false])
    expect(terminalAnnouncement(s)).toBe('Cancelled — the timeline is unchanged')
    expect(terminalAnnouncement(s)).not.toMatch(/Failed/)
  })

  it('a real failure is still a failure', async () => {
    const { usePromptStore, terminalAnnouncement } = await load()
    usePromptStore.setState({ status: 'running', sid: 's_A' })
    usePromptStore.getState().applyEvent({ type: 'error', message: 'Planning failed: boom' } as never)
    expect(usePromptStore.getState().status).toBe('error')
    expect(terminalAnnouncement(usePromptStore.getState())).toMatch(/^Failed/)
  })

  it('a reloaded cancelled run reads cancelled', async () => {
    apiMock.promptRun.mockResolvedValue({ run: { ...RUN_A, run_id: 'p_C', status: 'cancelled', error: 'Cancelled — timeline unchanged.' }, live: false })
    storage.clear()
    const { usePromptStore } = await load()
    await usePromptStore.getState().reconnect('s_A')
    expect(usePromptStore.getState().status).toBe('cancelled')
    expect(usePromptStore.getState().lastError).toBe('Cancelled — timeline unchanged.')
  })
})
