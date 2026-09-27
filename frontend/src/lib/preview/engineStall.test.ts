// The stuck-play watchdog's rule (review RD3): a playing run whose presented
// frame does not move for STALL_MS, while the element is not waiting for
// data, is restarted — at most STALL_MAX_RESTARTS times without progress.
import { describe, expect, it } from 'vitest'
import { STALL_MAX_RESTARTS, STALL_MS, StallWatch } from './engineLoop'

describe('StallWatch', () => {
  it('fires once per STALL_MS without progress, and gives up after the max', () => {
    const w = new StallWatch()
    let t = 0
    expect(w.check(1, false, t)).toBe(false)
    const fired: number[] = []
    for (t = 250; t <= 20_000; t += 250) if (w.check(1, false, t)) fired.push(t)
    expect(fired).toEqual([1500, 3000, 4500].slice(0, STALL_MAX_RESTARTS))
    expect(STALL_MS).toBe(1500)
  })

  it('progress resets it; waiting for data is not a stall', () => {
    const w = new StallWatch()
    w.check(1, false, 0)
    expect(w.check(1, false, 1600)).toBe(true)
    expect(w.check(2, false, 1700)).toBe(false)          // a new frame: progress
    expect(w.restarts).toBe(0)
    for (let t = 1800; t < 6000; t += 250) expect(w.check(2, true, t)).toBe(false)
    expect(w.check(2, false, 6000)).toBe(false)          // the wait just ended
    expect(w.check(2, false, 7600)).toBe(true)
  })

  it('a waiting flag with the element clock MOVING is a stall (no frame reaches the canvas)', () => {
    const w = new StallWatch()
    let fired = false
    for (let t = 0, et = 0; t <= 2000; t += 250, et += 0.25) fired ||= w.check(0, true, t, et)
    expect(fired).toBe(true)
    const still = new StallWatch()
    for (let t = 0; t <= 6000; t += 250) expect(still.check(0, true, t, 3.0)).toBe(false)
  })

  it('arm() restarts the clock for a new run', () => {
    const w = new StallWatch()
    w.check(5, false, 0)
    w.arm(1400)
    expect(w.check(5, false, 1600)).toBe(false)
    expect(w.check(5, false, 2900)).toBe(true)
  })
})
