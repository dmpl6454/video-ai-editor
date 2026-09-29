// Final sweep 3 r2 (Prompt bar, key-free, preview then apply):
//   * a preview that would change nothing is not "✓ 1 step done" / "Done";
//   * a run that ends with no result is not "Nothing to change";
//   * a card that expired (or was dropped elsewhere) does not leave the bar
//     stuck in `clarify` with no card, where Enter and Run did nothing;
//   * "yes" / "no" typed in the bar over an open card answers it (the card's
//     own text says "Reply yes to apply") instead of dropping it;
//   * the same sentence can be run again once the card is gone.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'


const refresh = vi.fn(async () => undefined)
const renderPreview = vi.fn(async () => 'hash')
vi.mock('../store', () => ({
  useStore: {
    getState: () => ({ sessionId: 's_test', selection: null, multiSelection: [], playhead: 0, refresh, renderPreview }),
  },
  errorMessage: (e: unknown) => (e instanceof Error ? e.message : String(e)),
}))
vi.mock('../toast', () => ({ toast: { info: () => {}, error: () => {}, success: () => {}, action: () => {} } }))

const apiMock = {
  promptStream: vi.fn<(sid: string, body: unknown) => Promise<Response>>(),
  promptAnswer: vi.fn<(sid: string, token: string, answers: unknown) => Promise<Response>>(),
  promptApply: vi.fn<(sid: string, token: string, apply: boolean) => Promise<Response>>(),
  promptCancel: vi.fn(async () => ({ cancelled: true })),
  promptRun: vi.fn<(sid: string) => Promise<unknown>>(),
  promptPending: vi.fn<(sid: string) => Promise<unknown>>(),
  promptBrains: vi.fn(async () => ({ brains: [] })),
}
vi.mock('../api', () => ({ api: apiMock }))

const storage = new Map<string, string>()
;(globalThis as { localStorage?: unknown }).localStorage = {
  getItem: (k: string) => storage.get(k) ?? null,
  setItem: (k: string, v: string) => { storage.set(k, v) },
  removeItem: (k: string) => { storage.delete(k) },
}


const { reduce, startRun, terminalAnnouncement, EMPTY_RUN } = await import('./promptEvents')
const { usePromptStore, _detachPromptStream } = await import('./promptStore')
const { previewReplyOf } = await import('./previewCard')
const { canSubmitPrompt } = await import('./promptFocus')

const PLAN = { version: 1, id: 'p_1', intent: 'adjust', title: 'Adjust', brain: 'recipes', confidence: 0.9,
               needs_input: [], postconditions: [],
               steps: [{ tool: 'set_clip_adjust', args: { clip_id: '$v1_all', brightness: 0.1 }, why: 'brighter' }] }

beforeEach(() => {
  for (const fn of Object.values(apiMock)) fn.mockClear()
  apiMock.promptRun.mockResolvedValue({ run: null, live: false })
  apiMock.promptPending.mockResolvedValue({ pending: null })
  _detachPromptStream()
  usePromptStore.setState({ ...EMPTY_RUN, sid: null, runId: null, prompt: '', history: [], chatBusy: false,
                            logOpen: false, connectionDropped: false, reconnecting: false, cancelling: false,
                            appliedFromPreview: false })
})
afterEach(() => { storage.clear() })

describe('a preview that would change nothing', () => {
  it('is announced as nothing to change, never "Done"', () => {
    let s = startRun()
    s = reduce(s, { type: 'plan', plan: PLAN } as never)
    s = reduce(s, { type: 'step', index: 0, total: 1, tool: 'set_clip_adjust', status: 'ok', summary: 'brightness' } as never)
    s = reduce(s, { type: 'text_delta', outcome: 'nothing_to_apply',
                    text: 'via Recipes — Adjust would not change anything on this timeline, so there is nothing to apply.' } as never)
    s = reduce(s, { type: 'done' })
    expect(s.status).toBe('done')
    expect(s.nothingToApply).toBe(true)
    const said = terminalAnnouncement(s)!
    expect(said).not.toMatch(/^Done/)
    expect(said).toBe('Nothing to change — Adjust would not change anything on this timeline, so there is nothing to apply.')
  })

  it('a run that ends with no text, op or card says it ended without a result', () => {
    let s = startRun()
    s = reduce(s, { type: 'plan', plan: PLAN } as never)
    s = reduce(s, { type: 'done' })
    expect(terminalAnnouncement(s)).toBe('The run ended without a result — nothing was changed.')
  })

  it('a normal applied run is still "Done"', () => {
    let s = startRun()
    s = reduce(s, { type: 'plan', plan: PLAN } as never)
    s = reduce(s, { type: 'text_delta', text: 'via Recipes — done — 1 step applied.' })
    s = reduce(s, { type: 'op', op: { seq: 3, tool: 'prompt' } } as never)
    s = reduce(s, { type: 'done' })
    expect(terminalAnnouncement(s)).toBe('Done')
  })
})

describe('an undo from the Prompt bar', () => {
  it('is announced as what it undid, never just "Answered"', () => {
    let s = startRun()
    s = reduce(s, { type: 'plan', plan: { ...PLAN, intent: 'undo', title: 'Undo', steps: [] } } as never)
    s = reduce(s, { type: 'op', op: { seq: 5, tool: 'undo' } } as never)
    s = reduce(s, { type: 'text_delta', text: "via Recipes — Undid: Muted Clip 3 'talk.mp4'. Redo with ⇧⌘Z." })
    s = reduce(s, { type: 'done' })
    expect(terminalAnnouncement(s)).toBe("Undid: Muted Clip 3 'talk.mp4'. Redo with ⇧⌘Z.")
  })
})

describe('a card that is gone when the project is reopened', () => {
  it('does not leave the bar in clarify with no card', async () => {
    apiMock.promptRun.mockResolvedValueOnce({ live: false, run: {
      run_id: 'r_1', status: 'clarify', prompt: 'make it black and white', brain: 'recipes', steps: [] } })
    apiMock.promptPending.mockResolvedValue({ pending: null, dropped: 'the question expired' })
    await usePromptStore.getState().reconnect('s_test')
    const s = usePromptStore.getState()
    expect(s.clarify).toBeNull()
    expect(s.status).not.toBe('clarify')
    expect(s.lastError ?? '').toMatch(/nothing was changed/i)
    // and the same sentence can be run again
    expect(canSubmitPrompt(s.status, { disabled: false, text: 'make it black and white' })).toBe(true)
  })

  it('Enter is still allowed when the status says clarify but no card is shown', () => {
    expect(canSubmitPrompt('clarify', { disabled: false, text: 'mute clip 1', cardOpen: false })).toBe(true)
    expect(canSubmitPrompt('clarify', { disabled: false, text: 'mute clip 1', cardOpen: true })).toBe(false)
    expect(canSubmitPrompt('clarify', { disabled: false, text: 'mute clip 1' })).toBe(false)
  })
})

describe('a reply typed in the bar over an open preview card', () => {
  it('reads yes / apply as Apply and no / change / undo as Change', () => {
    for (const t of ['yes', 'Yes.', 'y', 'ok', 'okay', 'apply', 'Apply it', 'do it', 'go ahead', 'sure', 'yep']) {
      expect(previewReplyOf(t)).toBe('apply')
    }
    for (const t of ['no', 'No!', 'n', 'nope', 'change', 'change it', 'cancel', 'discard', "don't apply",
                     'undo', 'go back', 'revert that']) {
      expect(previewReplyOf(t)).toBe('change')
    }
  })

  it('anything else is a new sentence', () => {
    for (const t of ['yes mute clip 2', 'mute the second clip', 'no wait make it red', '', 'apply a blur']) {
      expect(previewReplyOf(t)).toBeNull()
    }
  })
})
