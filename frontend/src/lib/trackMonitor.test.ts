import { describe, expect, it } from 'vitest'
import type { Track } from '../types'
import { isHeard, monitorHit } from './trackMonitor'

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

describe('label boxes', () => {
  it('M sits above the middle of the row, S below it', () => {
    expect(monitorHit(10, 100 + 18 - 8, 100, 36)).toBe('mute')
    expect(monitorHit(10, 100 + 18 + 6, 100, 36)).toBe('solo')
    expect(monitorHit(30, 100 + 18 + 6, 100, 36)).toBe(null)
  })
})
