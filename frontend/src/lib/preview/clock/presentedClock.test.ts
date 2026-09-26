// presentedClock (spec §3.5, R8, R11): the presented-frame clock, the
// play-start latency median, the audio anchor error and the drift monitor.
import { describe, expect, it } from 'vitest'
import {
  ANCHOR_TOLERANCE_S, DisplayTimeBase, DriftMonitor, DRIFT_MIN_GAP_MS, PlayStartLatency, PresentedClock, anchorError,
  playStartLatencyMs, reanchor, sampleAt,
} from './presentedClock'

describe('PresentedClock', () => {
  it('is the frame on screen while paused', () => {
    const c = new PresentedClock({ num: 30000, den: 1001 })
    c.setPaused(300)
    expect(c.now(123456)).toBeCloseTo((300 * 1001) / 30000, 12)
  })

  it('advances between presented frames, but never past the next one', () => {
    const c = new PresentedClock({ num: 25, den: 1 })
    c.onPresented(50, 1000)
    expect(c.now(1000)).toBeCloseTo(2.0, 12)
    expect(c.now(1020)).toBeCloseTo(2.02, 12)
    expect(c.now(1200)).toBeCloseTo(2.04, 12) // capped at one frame (40 ms)
    expect(c.now(900)).toBeCloseTo(2.0, 12)   // before its display time: the frame itself
  })
})

describe('PlayStartLatency', () => {
  it('seeds at 40 ms and keeps the median of the last 8', () => {
    const l = new PlayStartLatency()
    expect(l.median()).toBe(40)
    for (const ms of [10, 12, 14, 16, 18, 20, 22, 24, 26]) l.add(ms)
    expect(l.count).toBe(8)
    expect(l.median()).toBe(19) // 12..26 → (18+20)/2... of the last 8: 12..26
    l.add(Number.NaN)
    l.add(-5)
    expect(l.count).toBe(8)
  })
})

describe('audio anchor', () => {
  it('measures how far the sound is from the presented frame', () => {
    const a = { ctxTime: 10.0, mediaTime: 2.0 }
    // frame of media time 2.5 displayed at ctx 10.5: in sync
    expect(anchorError(a, 10.5, 2.5)).toBeCloseTo(0, 12)
    // displayed 12 ms later than the sound for it: the sound leads
    expect(anchorError(a, 10.512, 2.5)).toBeCloseTo(0.012, 12)
    expect(Math.abs(anchorError(a, 10.503, 2.5)) <= ANCHOR_TOLERANCE_S).toBe(true)
  })

  it('re-anchors a little in the future, keeping the same media ↔ ctx relation', () => {
    const r = reanchor(20, 4, 0.05)
    expect(anchorError(r, 20, 4)).toBeCloseTo(0, 12)
    expect(r.ctxTime).toBeCloseTo(20.05, 12)
  })

  it('maps media time to the 48 kHz output sample', () => {
    expect(sampleAt(1)).toBe(48000)
    expect(sampleAt((301 * 1001) / 30000)).toBe(Math.round((301 * 1001 * 48000) / 30000))
  })

  it('play-start latency subtracts the media time that elapsed', () => {
    expect(playStartLatencyMs(1000, 1080, 2.0, 2.0 + 1 / 30)).toBeCloseTo(80 - 33.333, 3)
  })
})

describe('DriftMonitor', () => {
  it('checks every 500 ms and re-anchors above 8 ms at most once per 2 s', () => {
    const d = new DriftMonitor()
    expect(d.due(0)).toBe(true)
    expect(d.check(0.005, 0)).toBe(false)
    expect(d.due(400)).toBe(false)
    expect(d.due(500)).toBe(true)
    // 12 ms: an output clock that lost time under load (WK: render-thread
    // underruns) — within the spec's 20 ms, but p95 ≤ 10 ms needs it fixed
    expect(d.check(0.012, 500)).toBe(true)
    expect(d.check(0.03, 1000)).toBe(false) // within 2 s of the last re-anchor
    expect(d.check(-0.03, 500 + DRIFT_MIN_GAP_MS)).toBe(true)
    expect(d.reanchors).toBe(2)
    expect(d.maxAbsErrorS).toBeCloseTo(0.03, 12)
  })
})

describe('DisplayTimeBase (WebKit MSE rVFC time base)', () => {
  it('passes a sane expectedDisplayTime through unchanged', () => {
    const b = new DisplayTimeBase()
    expect(b.fix(1000, 974)).toBe(974)
    expect(b.fix(1033, 1007)).toBe(1007)
    expect(b.repaired).toBe(0)
  })

  it('moves an expectedDisplayTime on another clock onto performance.now(), keeping its spacing', () => {
    // measured in WKWebView (macOS 27): with a MediaSource-fed <video>,
    // metadata.expectedDisplayTime ≈ −70.3e6 ms while the callback's `now` is
    // ~700 ms; the frames stay 33.3 ms apart on that clock
    const b = new DisplayTimeBase()
    const off = -70_304_204
    const out: number[] = []
    for (let i = 0; i < 20; i++) {
      const E = off + 697 + i * 33.3333
      const cbNow = 697 + i * 33.3333 + (i === 3 ? -10 : 0)   // one early callback
      out.push(b.fix(cbNow, E))
    }
    expect(b.repaired).toBe(20)
    // from the earliest callback on, the frames keep their own spacing
    for (let i = 4; i < out.length; i++) expect(out[i] - out[i - 1]).toBeCloseTo(33.3333, 3)
    // on the performance.now() clock, never after its callback: the earliest
    // callback (10 ms sooner after its frame than the others) sets the lag
    for (let i = 0; i < out.length; i++) expect(out[i]).toBeLessThanOrEqual(697 + i * 33.3333 + (i === 3 ? -10 : 0) + 1e-6)
    expect(out[19]).toBeCloseTo(697 + 19 * 33.3333 - 10, 3)
    // after reset() (a new play) only the new run's offsets count
    b.reset()
    expect(b.fix(5000, off + 5000 - 13)).toBe(5000)
  })
})
