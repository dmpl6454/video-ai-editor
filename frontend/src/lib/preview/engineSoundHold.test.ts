// Play pressed while the sound under the playhead is not in memory yet
// (final sweep 3, run 3: a fresh import's chunks still loading — the first
// 0.4 s, or a whole second with a slow server, played as silence while the
// picture ran). The engine holds picture and sound together, buffering, for
// at most SOUND_HOLD_MS (INSTANT_PREVIEW_SPEC §11.1), then runs both.
import { describe, expect, it } from 'vitest'
import type { AudioSink } from './engine'
import { SOUND_HOLD_MS, SoundHold, type SoundHoldHost } from './engineSoundHold'

function host(hold: ((from: number, ms: number) => Promise<boolean> | null) | undefined) {
  const log: string[] = []
  let playing = true
  let destroyed = false
  const sink = { soundHold: hold } as unknown as AudioSink
  const h: SoundHoldHost = {
    sink: () => sink,
    isPlaying: () => playing,
    isDestroyed: () => destroyed,
    setBuffering: (on) => { log.push(on ? 'buffering' : 'ready') },
    run: () => { log.push('run') },
  }
  return { h, log, stop: () => { playing = false }, destroy: () => { destroyed = true } }
}

function deferred() {
  let resolve!: (v: boolean) => void
  const p = new Promise<boolean>((r) => { resolve = r })
  return { p, resolve }
}

const flush = async () => { for (let i = 0; i < 3; i++) await Promise.resolve() }

describe('SoundHold', () => {
  it('runs at once when the sink has nothing to wait for (or no soundHold)', () => {
    for (const fn of [undefined, () => null]) {
      const { h, log } = host(fn)
      const s = new SoundHold(h)
      expect(s.begin(96000)).toBe(false)
      expect(log).toEqual([])
      expect(s.active).toBe(false)
    }
  })

  it('holds, buffering, until the sound is in memory, then runs picture and sound', async () => {
    const d = deferred()
    const asked: Array<[number, number]> = []
    const { h, log } = host((from, ms) => { asked.push([from, ms]); return d.p })
    const s = new SoundHold(h)
    expect(s.begin(96000)).toBe(true)
    expect(asked).toEqual([[96000, SOUND_HOLD_MS]])
    expect(SOUND_HOLD_MS).toBeLessThanOrEqual(700)
    expect(log).toEqual(['buffering'])
    expect(s.active).toBe(true)
    await flush()
    expect(log).toEqual(['buffering'])
    d.resolve(true)
    await flush()
    expect(log).toEqual(['buffering', 'ready', 'run'])
    expect(s.active).toBe(false)
  })

  it('runs anyway when the wait times out (the sink labels what is missing)', async () => {
    const d = deferred()
    const { h, log } = host(() => d.p)
    const s = new SoundHold(h)
    s.begin(0)
    d.resolve(false)
    await flush()
    expect(log).toEqual(['buffering', 'ready', 'run'])
    expect(s.stats).toEqual({ holds: 1, timeouts: 1 })
  })

  it('a pause, a new hold or a destroy meanwhile: the old hold never runs', async () => {
    const d1 = deferred()
    const a = host(() => d1.p)
    const s1 = new SoundHold(a.h)
    s1.begin(0)
    s1.cancel()
    expect(s1.active).toBe(false)
    d1.resolve(true)
    await flush()
    expect(a.log).toEqual(['buffering'])

    const d2 = deferred()
    const b = host(() => d2.p)
    const s2 = new SoundHold(b.h)
    s2.begin(0)
    b.stop()                                   // paused: the engine is not playing
    d2.resolve(true)
    await flush()
    expect(b.log).toEqual(['buffering'])

    const d3 = deferred()
    const c = host(() => d3.p)
    const s3 = new SoundHold(c.h)
    s3.begin(0)
    c.destroy()
    d3.resolve(true)
    await flush()
    expect(c.log).toEqual(['buffering'])
  })
})
