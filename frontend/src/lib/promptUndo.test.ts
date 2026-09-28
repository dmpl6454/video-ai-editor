// Final QA: the run log's "Undo" (the whole prompt, one history step) sent a
// plain `undo`, so after any later edit it undid THAT edit and left the
// prompt's in place — and it stayed on screen after History undid the prompt.
import { describe, expect, it } from 'vitest'
import { promptOpRef, promptUndoAvailable } from './promptUndo'
import { reduce, EMPTY_RUN } from './promptEvents'

const op = (seq: number, after: string) => ({ seq, ts: 0, tool: 'prompt', args: {}, summary: 'Speed', edl_hash_before: 'x', edl_hash_after: after, by: 'claude' })

describe('the run log’s Undo', () => {
  const ref = promptOpRef(op(7, 'h7'))

  it('is offered while the prompt’s edit is the newest state of the timeline', () => {
    expect(promptUndoAvailable(true, ref, { edlHash: 'h7', ops: [op(6, 'h6'), op(7, 'h7')] })).toBe(true)
  })

  it('is withdrawn once another edit lands after it (it would undo that edit instead)', () => {
    expect(promptUndoAvailable(true, ref, { edlHash: 'h8', ops: [op(7, 'h7'), op(8, 'h8')] })).toBe(false)
  })

  it('is withdrawn once the prompt was undone elsewhere (History, ⌘Z)', () => {
    expect(promptUndoAvailable(true, ref, { edlHash: 'h6', ops: [op(6, 'h6'), op(7, 'h7')] })).toBe(false)
  })

  it('falls back to the op sequence while the timeline hash is unknown', () => {
    expect(promptUndoAvailable(true, ref, { edlHash: null, ops: [op(7, 'h7')] })).toBe(true)
    expect(promptUndoAvailable(true, ref, { edlHash: null, ops: [op(7, 'h7'), op(8, 'h8')] })).toBe(false)
  })

  it('is never offered without an op, or when nothing says which op it was', () => {
    expect(promptUndoAvailable(false, ref, { edlHash: 'h7', ops: [] })).toBe(false)
    expect(promptUndoAvailable(true, promptOpRef(null), { edlHash: 'h7', ops: [] })).toBe(false)
  })

  it('the op event records which op the prompt made', () => {
    const s = reduce(EMPTY_RUN, { type: 'op', op: op(7, 'h7') } as never)
    expect(s.opSeen).toBe(true)
    expect(s.opRef).toEqual({ seq: 7, hashAfter: 'h7' })
  })
})
