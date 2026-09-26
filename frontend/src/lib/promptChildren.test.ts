// QA-068 (remainder): the projects a shorts run creates become Open buttons in
// the run log, by name. The events below are the executor's exact frames
// (agent/prompt/executor.py: the make_shorts step's tool_result carries
// `new_sessions`; `_finish_children` emits one `finish_short` tool_use /
// tool_result pair per short with ids `<plan>_child<k>` and the record
// {session, name, status, …} — pinned server-side by
// tests/test_c2_panels.py::test_created_shorts_are_named_in_the_reply_and_carry_their_name_for_open).
import { describe, expect, it } from 'vitest'
import { createdProjects, reduce, startRun, type PromptEvent, type PromptRunState } from './promptEvents'

const fold = (events: object[], from: PromptRunState = startRun()) =>
  events.reduce<PromptRunState>((s, e) => reduce(s, e as PromptEvent), from)

const STEP = [
  { type: 'step', index: 0, total: 1, tool: 'make_shorts', status: 'running' },
  { type: 'tool_use', name: 'make_shorts', args: { target_count: 2, save_as_sessions: true }, id: 'p_1234abcd_s0' },
  { type: 'tool_result', name: 'make_shorts', id: 'p_1234abcd_s0',
    result: { summary: 'Made 2 short(s), saved as 2 new session(s)', shorts: [], new_sessions: ['s_aaaaaaaaaa', 's_bbbbbbbbbb'] } },
  { type: 'step', index: 0, total: 1, tool: 'make_shorts', status: 'ok', summary: 'Made 2 short(s)' },
]
const FINISH = [
  { type: 'tool_use', name: 'finish_short', args: { session: 's_aaaaaaaaaa' }, id: 'p_1234abcd_child0' },
  { type: 'tool_result', name: 'finish_short', id: 'p_1234abcd_child0',
    result: { session: 's_aaaaaaaaaa', status: 'ok', name: 'talk short 1', error: null, applied: 3, op: null } },
  { type: 'tool_use', name: 'finish_short', args: { session: 's_bbbbbbbbbb' }, id: 'p_1234abcd_child1' },
  { type: 'tool_result', name: 'finish_short', id: 'p_1234abcd_child1', is_error: true,
    result: { session: 's_bbbbbbbbbb', status: 'failed', name: 'talk short 2', error: 'RuntimeError: x' } },
]

describe('created projects (QA-068)', () => {
  it('names every finished short, in order, with its outcome', () => {
    const s = fold([...STEP, ...FINISH, { type: 'done' }])
    expect(createdProjects(s)).toEqual([
      { session: 's_aaaaaaaaaa', name: 'talk short 1', status: 'ok' },
      { session: 's_bbbbbbbbbb', name: 'talk short 2', status: 'failed' },
    ])
    // The finishing records are not plan steps: no phantom row.
    expect(s.steps.map((r) => r.tool)).toEqual(['make_shorts'])
    expect(s.unknownEvents).toBe(0)
  })

  it('still offers the shorts when nobody finished them (ids from the step result)', () => {
    expect(createdProjects(fold(STEP))).toEqual([
      { session: 's_aaaaaaaaaa', name: null, status: 'ok' },
      { session: 's_bbbbbbbbbb', name: null, status: 'ok' },
    ])
  })

  it('a replayed stream (reconnect) does not list a short twice', () => {
    const s = fold([...STEP, ...FINISH, ...FINISH])
    expect(createdProjects(s).map((c) => c.session)).toEqual(['s_aaaaaaaaaa', 's_bbbbbbbbbb'])
  })

  it('a new run starts with none', () => {
    expect(createdProjects(startRun())).toEqual([])
  })
})
