// One edit lead for picture and sound, in time (clock/editLead.ts, §11.1).
import { describe, expect, it } from 'vitest'
import { EDIT_LEAD_S, editLeadFrames } from './editLead'
import { samplesForFrames } from '../timeline/timebase'

describe('editLeadFrames', () => {
  it('is 150 ms rounded UP to whole frames at every standard rate', () => {
    const cases: Array<[number, number, number]> = [
      [24000, 1001, 4], [24, 1, 4], [25, 1, 4], [30000, 1001, 5], [30, 1, 5], [48, 1, 8], [50, 1, 8], [60000, 1001, 9], [60, 1, 9],
    ]
    for (const [num, den, n] of cases) {
      expect(editLeadFrames({ num, den })).toBe(n)
      // never below the lead, never a whole frame past it
      const s = n * den / num
      expect(s).toBeGreaterThanOrEqual(EDIT_LEAD_S - 1e-12)
      expect(s - den / num).toBeLessThan(EDIT_LEAD_S)
    }
  })

  it('keeps the audible lead well inside the 250 ms budget at 24 fps (6 frames was all of it)', () => {
    const R = { num: 24, den: 1 }
    expect(samplesForFrames(editLeadFrames(R), R) / 48000).toBeLessThan(0.17)
    expect(samplesForFrames(6, R) / 48000).toBe(0.25)
  })
})
