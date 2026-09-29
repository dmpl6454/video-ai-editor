// Final sweep 3, round 2 (render-audio): `schema.sound_render_windows` —
// an UNLINKED sound run never starts before the unlinked run before it on
// its lane ends. v1 A|B|C (4 s) with a 0.5 s fade at 4; voiceover line 1 at
// [1.0, 4.8) plays whole, line 2 laid 0.2 s after it was pulled by the whole
// 0.5 s overlap and played over line 1 for 0.3 s. Mirrors
// tests/test_sound_run_gap_and_pip_limit.py.
import { describe, expect, it } from 'vitest'
import { soundPulls, soundWindows } from './audioPlan'

const seams: Array<[number, number]> = [[4, 0.5]]
const line = (id: string, start: number, out: number, linked?: string) =>
  ({ id, src: `${id}.wav`, in: 0, out, start, ...(linked ? { linked_to: linked } : {}) })

describe('soundWindows: a laid gap never becomes an overlap', () => {
  it('keeps abutting lines back to back', () => {
    const [l1, l2] = [line('L1', 1, 3.8), line('L2', 4.8, 3)]
    const w = soundWindows([l1, l2], seams, 12)
    expect(w.get(l1)).toEqual([expect.closeTo(1, 9), expect.closeTo(4.8, 9)])
    expect(w.get(l2)).toEqual([expect.closeTo(4.8, 9), expect.closeTo(7.8, 9)])
  })

  it.each([0.05, 0.2, 0.45])('shrinks a %s s gap to zero, not past it', (gap) => {
    const [l1, l2] = [line('L1', 1, 3.8), line('L2', 4.8 + gap, 3)]
    const w = soundWindows([l1, l2], seams, 12)
    expect(w.get(l2)).toEqual([expect.closeTo(4.8, 9), expect.closeTo(7.8, 9)])
    expect(soundPulls([l1, l2], seams, 12).get(l2)).toBeCloseTo(gap, 9)
  })

  it('keeps what is left of a gap wider than the overlap', () => {
    const [l1, l2] = [line('L1', 1, 3.8), line('L2', 5.6, 3)]
    expect(soundWindows([l1, l2], seams, 12).get(l2)).toEqual([expect.closeTo(5.1, 9), expect.closeTo(8.1, 9)])
  })

  it('lets a detached sound keep its J/L overlap', () => {
    const [l1, l2] = [line('L1', 1, 3.8, 'a'), line('L2', 5, 3, 'b')]
    expect(soundWindows([l1, l2], seams, 12).get(l2)).toEqual([expect.closeTo(4.5, 9), expect.closeTo(7.5, 9)])
  })

  it('clamps by where the programme end CUT the run before', () => {
    // line 1 at 3.0 (pull 0) laid to 13.0 past v1's end 12.0: cut at 12.5
    const [l1, l2] = [line('L1', 3, 10), line('L2', 13.2, 1)]
    const w = soundWindows([l1, l2], seams, 12)
    expect(w.get(l1)).toEqual([expect.closeTo(3, 9), expect.closeTo(12.5, 9)])
    expect(w.get(l2)).toEqual([expect.closeTo(12.7, 9), expect.closeTo(13.7, 9)])
  })
})
