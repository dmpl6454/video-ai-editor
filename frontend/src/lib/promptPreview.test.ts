// 0.8.0 "Preview, then apply" on the desktop side: the `clarify` frame that
// carries a `preview` becomes a preview card (not a question), the card is
// announced, Apply posts `{apply: true}` for THAT token, Change drops it
// with a cancel (nothing committed), a reload brings the card back from
// `GET …/prompt/pending`, and the Settings switch reads/writes the setting.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const sseResponse = (frames: object[]) =>
  new Response(frames.map((f) => `data: ${JSON.stringify(f)}\n\n`).join(''),
               { headers: { 'content-type': 'text/event-stream' } })

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

const { reduce, startRun, normalizePreview, terminalAnnouncement, previewAnnouncement, EMPTY_RUN } =
  await import('./promptEvents')
const { usePromptStore, _detachPromptStream } = await import('./promptStore')
const { normalizePromptSettings, promptApplyNote } = await import('./promptApplySetting')
const { moreLine, typeAheadKey, typedOverCard } = await import('./previewCard')

const PREVIEW = {
  summary: 'Slow clip 2: 2 changes',
  lines: ["Clip 2 'beach.mp4': speed 1x -> 0.75x (4.0 s -> 5.3 s)", '1 later clip moves to keep the video continuous'],
  more: 0, total: 2, hidden: [] as string[], note: null, nothing_changed: 'Nothing has changed yet.',
}
import type { ClarifyEvent, NeedsInput, Plan } from './promptEvents'

const APPLY_Q: NeedsInput = { key: 'apply', question: 'Apply these changes?', kind: 'confirm', required: true,
                  options: [{ value: 'yes', label: 'Apply' }, { value: 'no', label: 'Change' }] }
const CARD: ClarifyEvent = { type: 'clarify', token: 'q_card1', plan_id: 'p_1', questions: [APPLY_Q], expires_in_s: 600,
               preview: PREVIEW }
const PLAN: Plan = { version: 1, id: 'p_1', intent: 'speed', title: 'Slow clip 2', brain: 'recipes', confidence: 0.9,
               needs_input: [], postconditions: [],
               steps: [{ tool: 'set_speed', args: { clip_id: 'c_2', factor: 0.75 }, why: 'slow it' }] }

const flush = () => new Promise((r) => setTimeout(r, 0))

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

describe('the preview frame', () => {
  it('becomes a card, not a question, and says nothing has changed', () => {
    const s = reduce(reduce(startRun(), { type: 'plan', plan: PLAN }), CARD)
    expect(s.status).toBe('clarify')
    expect(s.clarify?.preview).toEqual(PREVIEW)
    expect(s.clarify?.token).toBe('q_card1')
    expect(terminalAnnouncement(s)).toBe(
      "Preview ready: 2 changes. Clip 2 'beach.mp4': speed 1x -> 0.75x (4.0 s -> 5.3 s); " +
      '1 later clip moves to keep the video continuous. ' +
      'Nothing has changed yet. Enter applies, Escape goes back to the prompt to change it.')
  })

  it('a plain question stays a question', () => {
    const s = reduce(startRun(), { ...CARD, preview: undefined })
    expect(s.clarify?.preview).toBeNull()
    expect(terminalAnnouncement(s)).toBe('One question before running')
  })

  it('reads a partial or foreign preview tolerantly', () => {
    expect(normalizePreview(null)).toBeNull()
    expect(normalizePreview({ summary: 'x', lines: [] })).toBeNull()
    expect(normalizePreview({ summary: 'x', lines: ['a', 3, 'b'], more: -2 })).toEqual({
      summary: 'x', lines: ['a', 'b'], more: 0, total: 2, hidden: [], note: null,
      nothing_changed: 'Nothing has changed yet.' })
    expect(normalizePreview({ summary: 'x', lines: ['a'], more: 2, total: 3, hidden: ['b', 7, 'c'] })?.hidden)
      .toEqual(['b', 'c'])
    expect(previewAnnouncement({ ...PREVIEW, total: 1 })).toMatch(/^Preview ready: 1 change\. /)
  })

  it('announces the note and what the card would do, not just a count (final sweep 3)', () => {
    const said = previewAnnouncement({ ...PREVIEW, total: 5, lines: ['one', 'two', 'three'],
      note: 'The timeline changed since the preview, so nothing was applied. Here is a fresh one.' })
    expect(said).toBe('Preview ready: 5 changes. The timeline changed since the preview, so nothing was applied. ' +
      'Here is a fresh one. one; two; and 3 more. Nothing has changed yet. Enter applies, Escape goes back to ' +
      'the prompt to change it.')
  })

  it('a key typed over the card goes to the prompt, Space included (final sweep 3)', () => {
    const k = (key: string, mods: Partial<{ metaKey: boolean; ctrlKey: boolean; altKey: boolean }> = {}) =>
      typeAheadKey({ key, metaKey: false, ctrlKey: false, altKey: false, ...mods })
    expect(k(' ')).toBe(' ')
    expect(k('n')).toBe('n')
    expect(k('/')).toBe('/')
    expect(k('Enter')).toBeNull()
    expect(k('Escape')).toBeNull()
    expect(k('Tab')).toBeNull()
    expect(k('a', { metaKey: true })).toBeNull()
    expect(k('z', { ctrlKey: true })).toBeNull()
    expect(typedOverCard('/', 'delete clip 2')).toBe('delete clip 2')
    expect(typedOverCard('n', 'delete clip 2')).toBe('n')
  })

  it('counts the capped lines in the last row', () => {
    expect(moreLine(12)).toBe('and 12 more changes')
    expect(moreLine(1)).toBe('and 1 more change')
  })
})

describe('Apply and Change', () => {
  async function openCard() {
    apiMock.promptStream.mockResolvedValueOnce(sseResponse([
      { type: 'brain', status: 'answered', brain: 'recipes', label: 'Recipes' },
      { type: 'plan', plan: PLAN },
      { type: 'text_delta', text: 'via Recipes — Preview — Slow clip 2: 2 changes. Nothing has changed yet.' },
      CARD, { type: 'done' }]))
    await usePromptStore.getState().run('slow clip 2 to 0.75x')
    await flush()
    expect(usePromptStore.getState().clarify?.preview?.summary).toBe('Slow clip 2: 2 changes')
  }

  it('Apply posts apply:true for the card on screen and runs the plan', async () => {
    await openCard()
    apiMock.promptApply.mockResolvedValueOnce(sseResponse([
      { type: 'brain', status: 'answered', brain: 'recipes', label: 'Recipes' },
      { type: 'plan', plan: PLAN },
      { type: 'step', index: 0, total: 1, tool: 'set_speed', status: 'ok' },
      { type: 'verify', plan_id: 'p_1', checks: [], passed: 1, total: 1, rendered: false },
      { type: 'op', op: { seq: 5, tool: 'prompt', args: { plan_id: 'p_1' }, summary: 'Prompt: Slow clip 2',
                          edl_hash_before: 'a', edl_hash_after: 'b', ts: 1, by: 'user' } },
      { type: 'text_delta', text: 'via Recipes — Slow clip 2: done.' }, { type: 'done' }]))
    await usePromptStore.getState().applyPreview()
    await flush()
    expect(apiMock.promptApply).toHaveBeenCalledWith('s_test', 'q_card1', true)
    expect(apiMock.promptCancel).not.toHaveBeenCalled()
    const s = usePromptStore.getState()
    expect(s.status).toBe('done')
    expect(s.appliedFromPreview).toBe(true)
    expect(s.clarify).toBeNull()
    expect(refresh).toHaveBeenCalled()
  })

  it('Change (dropClarify) cancels the card and commits nothing', async () => {
    await openCard()
    await usePromptStore.getState().dropClarify()
    expect(apiMock.promptCancel).toHaveBeenCalledWith('s_test', 'q_card1')
    expect(apiMock.promptApply).not.toHaveBeenCalled()
    expect(usePromptStore.getState().status).toBe('idle')
    expect(usePromptStore.getState().clarify).toBeNull()
  })

  it('Apply does nothing when the open card is a question, not a preview', async () => {
    usePromptStore.setState({ status: 'clarify', sid: 's_test',
                              clarify: { token: 'q_x', planId: 'p', questions: [], expiresInS: 1, preview: null } })
    await usePromptStore.getState().applyPreview()
    expect(apiMock.promptApply).not.toHaveBeenCalled()
  })

  it('a fresh card (the timeline moved) replaces the old one after Apply', async () => {
    await openCard()
    apiMock.promptApply.mockResolvedValueOnce(sseResponse([
      { type: 'brain', status: 'answered', brain: 'recipes', label: 'Recipes' },
      { type: 'plan', plan: PLAN },
      { ...CARD, token: 'q_card2', preview: { ...PREVIEW, note: 'The timeline changed since the preview, so nothing was applied.' } },
      { type: 'done' }]))
    await usePromptStore.getState().applyPreview()
    await flush()
    const s = usePromptStore.getState()
    expect(s.status).toBe('clarify')
    expect(s.clarify?.token).toBe('q_card2')
    expect(s.clarify?.preview?.note).toMatch(/^The timeline changed/)
    expect(s.opSeen).toBe(false)
  })

  it('a reload brings the card back from the pending route', async () => {
    apiMock.promptPending.mockResolvedValueOnce({ pending: {
      token: 'q_card1', plan_id: 'p_1', prompt: 'slow clip 2 to 0.75x', brain: 'recipes',
      questions: [APPLY_Q], expires_in_s: 500, preview: PREVIEW } })
    const ok = await usePromptStore.getState().restorePending('s_test')
    expect(ok).toBe(true)
    const s = usePromptStore.getState()
    expect(s.status).toBe('clarify')
    expect(s.prompt).toBe('slow clip 2 to 0.75x')
    expect(s.clarify?.preview?.lines).toEqual(PREVIEW.lines)
  })
})

describe('the Settings switch', () => {
  it('reads the wire and says where the value comes from', () => {
    expect(normalizePromptSettings({ confirm_before_apply: 'yes' })).toBeNull()
    const on = normalizePromptSettings({ confirm_before_apply: true, source: 'default', default: true })!
    expect(promptApplyNote(on)).toBe('On — Prompt bar edits wait for Apply.')
    expect(promptApplyNote({ ...on, confirm_before_apply: false, source: 'settings' }))
      .toBe('Off — Prompt bar edits apply as soon as they are planned.')
    expect(promptApplyNote({ ...on, source: 'env' })).toMatch(/^Set by VAI_PROMPT_CONFIRM/)
  })
})
