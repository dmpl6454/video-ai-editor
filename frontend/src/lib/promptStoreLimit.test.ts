// QA-124 through the store: a 422 for the prompt's length ends the run with a
// sentence the user can act on, not the envelope's bare "invalid request".
import { describe, expect, it, vi } from 'vitest'

vi.mock('../store', () => ({
  useStore: { getState: () => ({ sessionId: 's_test', selection: null, multiSelection: [], playhead: 0 }) },
  // The real mapper would say "invalid request: message — String should…".
  errorMessage: (e: unknown) => {
    const raw = e instanceof Error ? e.message : String(e)
    const at = raw.indexOf('{')
    try { return (JSON.parse(raw.slice(at)) as { error: { message: string } }).error.message } catch { return raw }
  },
}))
vi.mock('../toast', () => ({ toast: { info: () => {}, error: () => {}, success: () => {}, action: () => {} } }))
const body = JSON.stringify({ error: { code: 'VALIDATION_ERROR',
  message: 'invalid request: message — String should have at most 4000 characters',
  details: [{ type: 'string_too_long', loc: ['body', 'message'], msg: 'String should have at most 4000 characters',
              input: 'a'.repeat(200), ctx: { max_length: 4000 } }] } })
const promptStream = vi.fn(async () => { throw new Error(`422 Unprocessable Entity: ${body}`) })
vi.mock('../api', () => ({ api: { promptStream } }))
;(globalThis as { localStorage?: unknown }).localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} }

const { usePromptStore } = await import('./promptStore')

describe('a prompt refused for its length (QA-124)', () => {
  it('fails the run with the length in words', async () => {
    await usePromptStore.getState().run('a'.repeat(4700))
    const s = usePromptStore.getState()
    expect(promptStream).toHaveBeenCalledTimes(1)
    expect(s.status).toBe('error')
    expect(s.lastError).toBe('Prompt is too long — the limit is 4,000 characters. Shorten it and run it again.')
  })
})
