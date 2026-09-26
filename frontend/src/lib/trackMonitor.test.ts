import { describe, expect, it } from 'vitest'
import type { Track } from '../types'
import { isHeard, monitorButtons, monitorLabels, MONITOR_BUTTON } from './trackMonitor'

const tr = (id: string, extra: Partial<Track> = {}): Track => ({ id, type: 'audio', z: 0, clips: [], ...extra })

describe('solo (QA-086)', () => {
  it('with nothing soloed every unmuted lane is heard', () => {
    const ts = [tr('v1'), tr('music'), tr('vo', { muted: true })]
    expect(ts.map((t) => isHeard(t, ts))).toEqual([true, true, false])
  })
  it('while any lane is soloed only soloed lanes are heard', () => {
    const ts = [tr('v1'), tr('music', { solo: true }), tr('vo')]
    expect(ts.map((t) => isHeard(t, ts))).toEqual([false, true, false])
  })
  it('a muted lane stays silent even when soloed', () => {
    const ts = [tr('v1', { solo: true, muted: true })]
    expect(isHeard(ts[0], ts)).toBe(false)
  })
})

describe('lane monitor buttons (wave C review)', () => {
  it('sit side by side inside the row, under the name, each at least 18 px tall', () => {
    const { mute, solo } = monitorButtons(100, 36)
    expect(mute.y).toBe(solo.y)
    expect(solo.x).toBeGreaterThanOrEqual(mute.x + mute.w)
    expect(mute.y).toBeGreaterThan(100 + 13)            // below the name's baseline
    expect(mute.y + mute.h).toBeLessThanOrEqual(100 + 36)
    expect(Math.min(MONITOR_BUTTON.w, MONITOR_BUTTON.h)).toBeGreaterThanOrEqual(18)
    expect(solo.x + solo.w).toBeLessThanOrEqual(80)       // inside the 80 px label column
  })
  it('are named by action and lane', () => {
    expect(monitorLabels('Main video')).toEqual({ mute: 'Mute Main video', solo: 'Solo Main video' })
  })
})
