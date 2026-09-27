import { describe, expect, it } from 'vitest'
import { RunStartGate } from './engineSeek'

// The soak's end-of-program restart (tests/wk/test_wk_soak.py): after play()
// from 0 with the last frame on screen, frames of the OLD position and a
// 'waiting' before the first new frame must not end the run.
describe('RunStartGate', () => {
  it('is closed when the run starts on the frame on screen', () => {
    const g = new RunStartGate()
    g.begin(120, 120, 0)
    expect(g.pending).toBe(false)
    expect(g.stale(2879, 30, 1)).toBe(false)
  })

  it('marks frames of the old position stale until the new start is presented', () => {
    const g = new RunStartGate()
    g.begin(0, 2879, 1000)
    expect(g.pending).toBe(true)
    expect(g.stale(2879, 30, 1008)).toBe(true)     // the old last frame, 8 ms later
    expect(g.stale(2878, 30, 1400)).toBe(true)     // still old, however late
    expect(g.pending).toBe(true)
    expect(g.stale(1, 30, 1010)).toBe(false)       // the new run: opens for good
    expect(g.pending).toBe(false)
    expect(g.stale(2879, 30, 5000)).toBe(false)
  })

  it('admits a first frame as far from the start as the time elapsed allows', () => {
    const g = new RunStartGate()
    g.begin(0, 500, 0)
    expect(g.stale(12, 30, 200)).toBe(true)        // 200 ms at 30 fps: ≤ 8 frames
    expect(g.stale(7, 30, 200)).toBe(false)
  })

  it('a new begin replaces the old one', () => {
    const g = new RunStartGate()
    g.begin(0, 2879, 0)
    g.begin(300, 300, 10)
    expect(g.pending).toBe(false)
  })
})
