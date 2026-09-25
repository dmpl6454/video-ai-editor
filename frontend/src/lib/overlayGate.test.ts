import { describe, expect, it } from 'vitest'
import { enableWindow, inEnableWindow } from './overlayGate'
import { activeOnFrames } from './timelineLayout'

describe('overlay gate (QA-016)', () => {
  it('abutting cues share no frame at the boundary', () => {
    const fps = 30
    const cue1 = (t: number) => inEnableWindow(0.5, 3.0, t, fps)
    const cue2 = (t: number) => inEnableWindow(3.0, 6.0, t, fps)
    for (let n = 0; n < 200; n++) {
      const t = n / fps
      expect(cue1(t) && cue2(t)).toBe(false)
    }
    expect(cue1(89 / fps)).toBe(true)
    expect(cue2(90 / fps)).toBe(true)
    expect(cue1(90 / fps)).toBe(false)
  })

  it('is robust to float noise on the playhead at a boundary frame', () => {
    expect(inEnableWindow(3.0, 6.0, 3.0 - 1e-9, 30)).toBe(true)
    expect(inEnableWindow(0.5, 3.0, 3.0 - 1e-9, 30)).toBe(false)
  })

  it('mirrors timebase.enable_window numbers', () => {
    const [lo, hi] = enableWindow(3, 6, 30)
    expect(lo).toBeCloseTo(2.983333333, 8)
    expect(hi).toBeCloseTo(5.983333333, 8)
  })

  it('activeOnFrames applies the same gate on the render clock', () => {
    expect(activeOnFrames([], 3.0, 6.0, 3.0, 30)).toBe(true)
    expect(activeOnFrames([], 0.5, 3.0, 3.0, 30)).toBe(false)
  })
})
