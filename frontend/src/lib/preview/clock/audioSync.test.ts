// AudioSync (spec §3.5): what the engine asks of the AudioSink at play, at the
// first presented frame, after a stall or seek, and on drift.
import { describe, expect, it } from 'vitest'
import type { AudioSink } from '../engine'
import { AudioSync, SETTLE_FRAMES } from './audioSync'

class FakeSink implements AudioSink {
  readonly calls: Array<[string, number, number]> = []
  /** ctx seconds = perf ms / 1000 + offset */
  offset = 100
  live = true
  prepare(): void {}
  start(at: number, sample: number): void { this.calls.push(['start', at, sample]) }
  stop(ms: number): void { this.calls.push(['stop', ms, 0]) }
  reschedule(): void {}
  setParams(): void {}
  ctxTimeAt(perf: number): number | null { return this.live ? perf / 1000 + this.offset : null }
}

describe('AudioSync', () => {
  it('starts the sound for the play position at now + the latency median (40 ms seed)', () => {
    const sink = new FakeSink()
    const a = new AudioSync(() => sink)
    a.play(1000, 2.0)
    expect(sink.calls).toEqual([['start', 1.04 + 100, 96000]])
    expect(a.anchor).toEqual({ ctxTime: 101.04, mediaTime: 2.0 })
  })

  it('re-anchors once when the first presented frame is more than 4 ms off, and learns the latency', () => {
    const sink = new FakeSink()
    const a = new AudioSync(() => sink)
    a.play(1000, 2.0)
    // media 2.0 was due at 1040 (+40 ms seed); it shows at 1070: 30 ms late
    a.onFrame({ mediaTime: 2.0, expectedDisplayTime: 1070 })
    expect(a.stats.reanchors).toBe(1)
    expect(a.stats.firstFrameErrorMs[0]).toBeCloseTo(30, 6)
    const [, at, sample] = sink.calls[1]
    // media 2.05 heard at the display time of 2.0 + 50 ms: in step with the picture
    expect(at).toBeCloseTo(1.07 + 100 + 0.05, 9)
    expect(sample).toBe(Math.round(2.05 * 48000))
    expect(a.latency.median()).toBeCloseTo(55, 6) // median of the 40 ms seed and 70 ms
  })

  it('checks the anchor once more when the display clock has settled (8th frame), at the same 4 ms', () => {
    // WebKit's MSE display times are repaired from the callbacks' own clock
    // (DisplayTimeBase): the first frame's estimate can be a few ms off
    const sink = new FakeSink()
    const a = new AudioSync(() => sink)
    a.play(1000, 2.0)
    const at = (i: number) => ({ mediaTime: 2.0 + i / 30, expectedDisplayTime: 1041 + i * 1000 / 30 + (i ? 6 : 0) })
    a.onFrame(at(0))                                   // within 4 ms: left
    for (let i = 1; i < SETTLE_FRAMES - 1; i++) a.onFrame(at(i), 1100 + i)   // 6 ms: under the drift threshold
    expect(a.stats.reanchors).toBe(0)
    a.onFrame(at(SETTLE_FRAMES - 1), 1200)              // the settle check: 6 ms > 4 ms, fixed now
    expect(a.stats.reanchors).toBe(1)
    const n = sink.calls.length
    for (let j = SETTLE_FRAMES; j < SETTLE_FRAMES + 20; j++) a.onFrame(at(j), 1300 + j)
    expect(sink.calls.length).toBe(n)                   // in step from then on
  })

  it('leaves a first frame within 4 ms alone', () => {
    const sink = new FakeSink()
    const a = new AudioSync(() => sink)
    a.play(1000, 2.0)
    a.onFrame({ mediaTime: 2.0, expectedDisplayTime: 1043 })
    expect(a.stats.reanchors).toBe(0)
    expect(sink.calls).toHaveLength(1)
  })

  it('restarts from a fresh anchor after a stall or a seek, 50 ms ahead of the frame', () => {
    const sink = new FakeSink()
    const a = new AudioSync(() => sink)
    a.play(0, 0)
    a.stop(5)
    expect(a.anchor).toBeNull()
    a.requestRestart()
    a.onFrame({ mediaTime: 10, expectedDisplayTime: 5000 })
    expect(sink.calls.slice(-2)).toEqual([['stop', 5, 0], ['start', 5.05 + 100, Math.round(10.05 * 48000)]])
  })

  it('re-anchors on drift above 8 ms, checked every 500 ms', () => {
    const sink = new FakeSink()
    const a = new AudioSync(() => sink)
    a.play(0, 0)
    a.onFrame({ mediaTime: 0, expectedDisplayTime: 40 }, 40)       // first frame: in step
    sink.offset = 100.03                                             // the output clock slips 30 ms
    a.onFrame({ mediaTime: 1, expectedDisplayTime: 1040 }, 1040)
    expect(a.stats.reanchors).toBe(1)
    a.onFrame({ mediaTime: 1.1, expectedDisplayTime: 1140 }, 1140)  // < 500 ms later: not checked
    expect(a.stats.reanchors).toBe(1)
  })

  it('works with a sink that has no clock (the null sink): start(0, sample), no anchor', () => {
    const sink = new FakeSink()
    sink.live = false
    const a = new AudioSync(() => sink)
    a.play(0, 1)
    a.onFrame({ mediaTime: 1, expectedDisplayTime: 60 })
    expect(sink.calls).toEqual([['start', 0, 48000]])
    expect(a.anchor).toBeNull()
  })

  it('anchors at the first presented frame when the sink only got a clock by starting', () => {
    // the audio lane creates its AudioContext inside start(): no clock at play()
    const sink = new FakeSink()
    sink.live = false
    const a = new AudioSync(() => sink)
    a.play(0, 1)
    sink.live = true
    a.onFrame({ mediaTime: 1.0333, expectedDisplayTime: 70 })
    expect(sink.calls[1][0]).toBe('start')
    expect(sink.calls[1][1]).toBeCloseTo(0.07 + 100 + 0.05, 9)
    expect(sink.calls[1][2]).toBe(Math.round(1.0833 * 48000))
    expect(a.anchor).not.toBeNull()
  })
})
