import { describe, expect, it } from 'vitest'
import { createSliderCommitter, type SliderTimers } from './sliderCommit'

/** A manual clock: `run()` fires every pending timer, like the idle elapsing. */
function fakeTimers() {
  let next = 1
  const live = new Map<number, () => void>()
  const timers: SliderTimers = {
    set: (fn) => { const id = next++; live.set(id, fn); return id },
    clear: (h) => { live.delete(h as number) },
  }
  const run = () => { const fns = [...live.values()]; live.clear(); fns.forEach((f) => f()) }
  return { timers, run, count: () => live.size }
}

describe('slider commits (QA-087)', () => {
  it('twelve keyboard nudges commit ONE op with the last value', () => {
    const sent: number[] = []
    const t = fakeTimers()
    const c = createSliderCommitter(-12, (v) => sent.push(v), { timers: t.timers })
    for (let i = 1; i <= 12; i++) {
      const v = -12 + i * 0.5
      c.change(v)
      c.nudge(v)
    }
    expect(sent).toEqual([])          // nothing while the keys are still moving
    expect(t.count()).toBe(1)         // one timer, re-armed, never twelve
    t.run()
    expect(sent).toEqual([-6])
  })

  it('pointer-up then the trailing blur is still one op', () => {
    const sent: number[] = []
    const c = createSliderCommitter(0, (v) => sent.push(v), { timers: fakeTimers().timers })
    c.change(10); c.change(25)
    c.release(25)
    c.flush()                         // blur after release
    expect(sent).toEqual([25])
  })

  it('blur flushes a waiting keyboard commit immediately, exactly once', () => {
    const sent: number[] = []
    const t = fakeTimers()
    const c = createSliderCommitter(1, (v) => sent.push(v), { timers: t.timers })
    c.change(1.1); c.nudge(1.1)
    c.change(1.2); c.nudge(1.2)
    c.flush()
    t.run()
    expect(sent).toEqual([1.2])
  })

  it('returning to the stored value sends nothing', () => {
    const sent: number[] = []
    const t = fakeTimers()
    const c = createSliderCommitter(5, (v) => sent.push(v), { timers: t.timers })
    c.nudge(6); c.nudge(5)
    t.run()
    expect(sent).toEqual([])
  })

  it('an outside change (undo) becomes the baseline, but never mid-burst', () => {
    const sent: number[] = []
    const t = fakeTimers()
    const c = createSliderCommitter(0, (v) => sent.push(v), { timers: t.timers })
    c.sync(3)                         // undo moved the stored value to 3
    c.release(3)
    expect(sent).toEqual([])          // 3 is already stored
    c.nudge(4)
    c.sync(0)                         // a stale refresh lands mid-burst: ignored
    t.run()
    expect(sent).toEqual([4])
  })
})

describe('colour well commits (QA-078)', () => {
  it('dragging through colours commits once, the last one, when the well goes idle', async () => {
    const { createCommitter } = await import('./sliderCommit')
    const sent: string[] = []
    const t = fakeTimers()
    const c = createCommitter<string>('#ffffff', (v) => sent.push(v), { timers: t.timers })
    for (const v of ['#fe0000', '#fd0000', '#ff0000']) c.nudge(v)
    expect(sent).toEqual([])
    t.run()
    expect(sent).toEqual(['#ff0000'])
    c.flush()
    expect(sent).toEqual(['#ff0000'])
  })
})
