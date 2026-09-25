import { describe, expect, it } from 'vitest'
import { levelAt, VOLUME_RANGE, volumeCommit, volumeKeyAt, volumeKeyToggle } from './audioLevel'

describe('Volume range (QA-086)', () => {
  it('reaches +20 dB — the old slider stopped at +6', () => {
    expect(VOLUME_RANGE.max).toBe(20)
    expect(volumeCommit('c1', { gain_db: 0 }, 0, 18)).toEqual({ tool: 'set_volume', args: { target: 'c1', db: 18 } })
    expect(volumeCommit('c1', undefined, 0, 99).args.db).toBe(20)
  })
})

describe('volume automation (QA-086)', () => {
  const a = { gain_db: -6, gain_env: { keyframes: [[0, -10], [2, 0]] as [number, number][] } }
  it('reads the absolute level: clip gain + the keyed offset', () => {
    expect(levelAt(a, 0)).toBe(-16)
    expect(levelAt(a, 1)).toBe(-11)
    expect(levelAt(a, 5)).toBe(-6)
    expect(levelAt({ gain_db: 3 }, 1)).toBe(3)
  })
  it('a slider commit on an automated clip keys the playhead instead of flattening the curve', () => {
    expect(volumeCommit('c1', a, 1.5, -3)).toEqual(
      { tool: 'add_keyframe', args: { clip_id: 'c1', prop: 'audio.gain_db', time: 1.5, value: -3 } })
  })
  it('the key toggle removes a key within half a frame, else pins the current level', () => {
    expect(volumeKeyAt(a, 2.01, 30)).toBe(true)
    expect(volumeKeyToggle('c1', a, 2.01, 30).tool).toBe('remove_keyframe')
    expect(volumeKeyToggle('c1', a, 1, 30)).toEqual(
      { tool: 'add_keyframe', args: { clip_id: 'c1', prop: 'audio.gain_db', time: 1, value: -11 } })
  })
})
