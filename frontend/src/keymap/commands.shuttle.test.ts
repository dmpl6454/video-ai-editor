// QA-058: the first L from a stop played at 2× (K resets the rate to 1, and
// L doubled it). Also QA-054: ⌘\ fits the width the timeline registers.
import { describe, expect, it, vi } from 'vitest'
import type { Store } from './commands'

vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {}, removeItem: () => {} })
const { COMMAND_BY_ID } = await import('./commands')
const { registerTimelineView } = await import('../lib/timelineZoom')

function transport(isPlaying: boolean, playbackRate: number) {
  const s = {
    isPlaying, playbackRate, playhead: 4.0125, edl: { canvas: { fps: 30 }, duration: 40 },
    setPlaybackRate(r: number) { s.playbackRate = r },
    setPlaying(p: boolean) { s.isPlaying = p },
    setPlayhead(t: number) { s.playhead = t },
    setTimelineZoom: vi.fn(),
  }
  return s
}

describe('J / K / L shuttle', () => {
  it('K then L plays at 1×, a second L at 2×, then 4×', () => {
    const s = transport(false, 1)
    COMMAND_BY_ID.shuttleStop.run(s as unknown as Store)
    COMMAND_BY_ID.shuttleForward.run(s as unknown as Store)
    expect([s.isPlaying, s.playbackRate]).toEqual([true, 1])
    COMMAND_BY_ID.shuttleForward.run(s as unknown as Store)
    expect(s.playbackRate).toBe(2)
    COMMAND_BY_ID.shuttleForward.run(s as unknown as Store)
    expect(s.playbackRate).toBe(4)
  })

  it('L while shuttling backwards starts forward at 1×; J mirrors L', () => {
    const s = transport(true, -4)
    COMMAND_BY_ID.shuttleForward.run(s as unknown as Store)
    expect(s.playbackRate).toBe(1)
    const r = transport(false, 1)
    COMMAND_BY_ID.shuttleReverse.run(r as unknown as Store)
    expect(r.playbackRate).toBe(-1)
    COMMAND_BY_ID.shuttleReverse.run(r as unknown as Store)
    expect(r.playbackRate).toBe(-2)
  })

  it('a paused rate left over from an earlier shuttle does not carry over', () => {
    const s = transport(false, 4)
    COMMAND_BY_ID.shuttleForward.run(s as unknown as Store)
    expect(s.playbackRate).toBe(1)
  })
})

describe('second steps land on the frame grid (QA-049)', () => {
  it('→ one second from 4.0125 s is frame 150', () => {
    const s = transport(false, 1)
    COMMAND_BY_ID.secondForward.run(s as unknown as Store)
    expect(s.playhead * 30).toBeCloseTo(150, 9)
  })
})

describe('zoom to fit (QA-054)', () => {
  it('fits the registered lane width, not the window', () => {
    const fitTo = vi.fn()
    registerTimelineView({ laneWidth: () => 848, fitTo })
    const s = transport(false, 1)
    COMMAND_BY_ID.zoomFit.run(s as unknown as Store)
    const z = fitTo.mock.calls[0][0] as number
    expect(80 + 40 * z).toBeLessThanOrEqual(928)
    expect(80 + 40 * z).toBeGreaterThan(880)
    registerTimelineView(null)
  })
})
