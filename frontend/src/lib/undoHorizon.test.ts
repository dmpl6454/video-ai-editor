import { describe, expect, it } from 'vitest'
import { isWithinUndoHorizon, undoDepthOf, undoRefusedMessage, undoTitle } from './undoHorizon'

describe('undo horizon (QA-046)', () => {
  it('binds to the server depth, not to how many ops History lists', () => {
    const ops = Array.from({ length: 40 }, (_, i) => ({ seq: i }))
    expect(undoDepthOf({ undo_depth: 29, ops })).toBe(29)
    expect(undoDepthOf({ undo_depth: 0, ops: [{ seq: 1 }] })).toBe(0)   // fresh project: init only
    expect(undoDepthOf({ ops })).toBe(40)                                 // older backend
  })

  it('marks exactly the newest `depth` entries as reachable', () => {
    expect([0, 1, 2, 3].map((i) => isWithinUndoHorizon(i, 2))).toEqual([true, true, false, false])
    expect(isWithinUndoHorizon(0, 0)).toBe(false)
  })

  it('explains a refused undo and titles the button with the depth', () => {
    expect(undoRefusedMessage(14)).toMatch(/past the undo limit/)
    expect(undoTitle(0, '⌘Z')).toBe('Nothing to undo')
    expect(undoTitle(3, '⌘Z')).toMatch(/3 steps/)
  })
})
