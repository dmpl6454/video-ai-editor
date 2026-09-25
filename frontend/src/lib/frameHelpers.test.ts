// FPS-FLOAT-DISPLAY + FRONTEND-FRAME-HELPER-DUP.
//
// canvas.fps is a float now (QA-009), so the top bar printed
// "1080×1920 · 29.97002997002997fps". And three places each derived "one
// frame" with their own fallback — nudge.ts had no upper bound, and the
// store's replay-from-start still assumed 1/30 — so every frame computation
// now goes through lib/frameStep.ts.
import { describe, expect, it, vi } from 'vitest'
import type { EDL } from '../types'
import { canvasFacts, formatFps } from './frameStep'
import { projectFrame } from './nudge'

vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {}, removeItem: () => {} })
const { useStore } = await import('../store')

describe('fps for display', () => {
  it('prints broadcast rates the way an editor writes them', () => {
    expect(formatFps(30000 / 1001)).toBe('29.97')
    expect(formatFps(24000 / 1001)).toBe('23.976')
    expect(formatFps(60000 / 1001)).toBe('59.94')
    expect(formatFps(30)).toBe('30')
    expect(formatFps(25)).toBe('25')
    expect(formatFps(undefined)).toBe('30')
  })

  it('the canvas facts line uses it', () => {
    expect(canvasFacts({ w: 1080, h: 1920, fps: 29.97002997002997 }, 12.34))
      .toBe('1080×1920 · 29.97fps · 12.3s')
  })
})

function edlAt(fps: number, duration = 10): EDL {
  return { canvas: { w: 1920, h: 1080, fps }, duration, tracks: [] } as unknown as EDL
}

describe('one project frame, one rule', () => {
  it('nudge uses the shared clamp (1..240, else 30)', () => {
    expect(projectFrame(edlAt(25))).toBeCloseTo(0.04, 12)
    expect(projectFrame(edlAt(1000))).toBeCloseTo(1 / 30, 12)
  })

  it('replay-from-start treats the last frame of a 25 fps timeline as the end', () => {
    useStore.setState({ edl: edlAt(25, 10), isPlaying: false, playhead: 10 - 0.035 })
    expect(useStore.getState().replayFromStart()).toBe(true)
    expect(useStore.getState().playhead).toBe(0)
  })
})
