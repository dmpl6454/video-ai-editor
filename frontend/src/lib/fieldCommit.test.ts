// Final sweep 2: a Duration typed past what a Flash-In clip can reach was
// answered 200 with no change (trim_clip clamped to the source it already
// used). The answer was truthy, so the field kept the typed 00:00:02:12 while
// Start/End said 1.93 s — until the clip was reselected.
import { describe, expect, it } from 'vitest'
import { afterCommit } from './fieldCommit'

describe('afterCommit (TimecodeField)', () => {
  it('a truthy answer that changed nothing still restores the field', async () => {
    let restored = 0
    await afterCommit(Promise.resolve({ summary: 'Trim … out=5.00' }), () => false, () => { restored++ })
    expect(restored).toBe(1)
  })
  it('a refused (null) answer restores it', async () => {
    let restored = 0
    await afterCommit(Promise.resolve(null), () => false, () => { restored++ })
    expect(restored).toBe(1)
  })
  it('never while the user is typing in it again', async () => {
    let restored = 0
    await afterCommit(Promise.resolve(null), () => true, () => { restored++ })
    expect(restored).toBe(0)
  })
})

describe('afterCommit on a throwing commit', () => {
  it('restores and still surfaces the error', async () => {
    let restored = 0
    await expect(afterCommit(Promise.reject(new Error('boom')), () => false, () => { restored++ }))
      .rejects.toThrow('boom')
    expect(restored).toBe(1)
  })
})
