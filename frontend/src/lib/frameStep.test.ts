import { describe, expect, it, vi } from 'vitest'
import { frameDuration, projectFps, stepFrames } from './frameStep'
import type { Store } from '../keymap/commands'

// The command registry imports the store, which reads localStorage at module
// load; node has none (store.test.ts stubs it the same way).
vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {}, removeItem: () => {} })
const { COMMANDS } = await import('../keymap/commands')

describe('frame step follows the project frame rate (QA-009)', () => {
  it('25 steps on a 25 fps project is exactly one second', () => {
    let t = 0
    for (let i = 0; i < 25; i++) t = stepFrames(t, 1, 25)
    expect(t).toBe(1)
  })

  it('NTSC rates step on the exact 1001/30000 grid', () => {
    const fps = 30000 / 1001
    let t = 0
    for (let i = 0; i < 30; i++) t = stepFrames(t, 1, fps)
    expect(t).toBeCloseTo(1.001, 9)
    expect(stepFrames(t, -30, fps)).toBe(0)
  })

  it('a playhead between frames snaps onto the grid and never goes negative', () => {
    expect(stepFrames(0.513, 1, 25)).toBeCloseTo(0.56, 9)
    expect(stepFrames(0.01, -1, 25)).toBe(0)
  })

  it('falls back to 30 fps for a missing or nonsense rate', () => {
    expect(projectFps(undefined)).toBe(30)
    expect(projectFps(0)).toBe(30)
    expect(projectFps(Number.NaN)).toBe(30)
    expect(frameDuration(50)).toBeCloseTo(0.02, 12)
  })

  it('the frameForward / nudge commands use edl.canvas.fps', () => {
    let playhead = 0
    let nudged = 0
    const s = {
      get playhead() { return playhead },
      edl: { canvas: { w: 1920, h: 1080, fps: 25 } },
      setPlaying: () => {},
      setPlayhead: (t: number) => { playhead = t },
      nudgeSelection: (d: number) => { nudged = d },
    } as unknown as Store
    const fwd = COMMANDS.find((c) => c.id === 'frameForward')!
    for (let i = 0; i < 25; i++) fwd.run(s)
    expect(playhead).toBe(1)
    COMMANDS.find((c) => c.id === 'nudgeRight')!.run(s)
    expect(nudged).toBeCloseTo(0.04, 12)
  })
})

describe('displaySeekTime (QA-077: a paused seek never lands on a frame pts)', () => {
  it('seeks to the MIDDLE of the frame the playhead names, at any rate', async () => {
    const { displaySeekTime } = await import('./frameStep')
    for (const fps of [30, 25, 24, 30000 / 1001, 60]) {
      for (let n = 0; n < 12; n++) {
        const t = displaySeekTime(n / fps, fps)
        // Chromium keeps µs: the truncated time must still be inside frame n.
        const truncated = Math.floor(t * 1e6) / 1e6
        expect(Math.floor(truncated * fps + 1e-9), `${fps} fps frame ${n}`).toBe(n)
        expect(truncated * fps - n).toBeGreaterThan(0.25)
      }
    }
    // frame 2 at 30 fps: the exact pts truncates to 0.066666 (frame 1)
    expect(Math.floor(Math.floor((2 / 30) * 1e6) / 1e6 * 30)).toBe(1)
    expect(displaySeekTime(2 / 30, 30)).toBeCloseTo(2.5 / 30, 12)
  })
})
