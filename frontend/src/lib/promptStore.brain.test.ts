// Editor Brain (EB1-F, FX-E wave): a stored brain run, reloaded (`hydrate`), reads
// like the live one — its advisory audit is a note, not a miss in the headline.
import { describe, expect, it, vi } from 'vitest'

vi.mock('../store', () => ({
  useStore: { getState: () => ({ sessionId: 's_x', selection: null, multiSelection: [], playhead: 0, refresh: vi.fn() }) },
  errorMessage: (e: unknown) => (e instanceof Error ? e.message : String(e)),
}))
vi.mock('../toast', () => ({ toast: { info: vi.fn(), error: vi.fn(), success: vi.fn(), action: vi.fn() } }))
vi.mock('../api', () => ({ api: {} }))

const { usePromptStore } = await import('./promptStore')
const { checksHeadline } = await import('./checkProse')

const record = (tools: string[]) => ({
  run_id: 'p_1', plan_id: 'p_1', prompt: 'make a 45-second reel', brain: 'recipes', status: 'done', started: 0, ended: 1,
  steps: tools.map((tool, index) => ({ index, total: tools.length, tool, status: 'ok' })),
  verify: { plan_id: 'p_1', passed: 2, total: 3, rendered: false, checks: [
    { check: 'captions_nonempty', human: 'captions were laid', pass: true, headline: true, blocking: false },
    { check: 'canvas_aspect', human: 'the canvas has the requested aspect', pass: true, headline: true, blocking: true },
    { check: 'audit_ok', human: 'the aesthetic audit passes', pass: false, headline: true, blocking: false },
  ] },
  reply: 'done', op: null, error: null, child_runs: [], events: [],
})

describe('hydrate', () => {
  it('a reloaded brain run reads clean: 2 of 2', () => {
    usePromptStore.getState().hydrate('s_x', record(['cut_source_ranges', 'audit_aesthetic']) as never)
    const v = usePromptStore.getState().verify!
    expect([v.passed, v.total]).toEqual([2, 2])
    expect(checksHeadline(v)).toBe('2 of 2 checks passed')
  })
  it('an ordinary run keeps its 0.8.0 headline', () => {
    usePromptStore.getState().hydrate('s_x', record(['remove_fillers', 'audit_aesthetic']) as never)
    const v = usePromptStore.getState().verify!
    expect(checksHeadline(v)).toBe('2 of 3 checks passed · 1 failed')
  })
})
