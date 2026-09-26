import { describe, expect, it } from 'vitest'
import { effectsTargetLine } from './effectsTarget'

describe('Effects target line', () => {
  const src = '/wd/s_1/uploads/clip20_ab12cd34/clip20.normalized.mp4'
  it('names the clip the way the Media panel does, never the editing copy', () => {
    const names = new Map([[src, 'Beach day.mov']])
    expect(effectsTargetLine(src, names, false)).toBe('Applies to: Beach day.mov')
    expect(effectsTargetLine(src, names, true)).toBe('Applies to the clip at the playhead: Beach day.mov')
  })
  it('falls back to a readable name, not the .normalized disk file', () => {
    const line = effectsTargetLine(src, new Map(), false)
    expect(line).not.toMatch(/normalized/)
    expect(line).toMatch(/^Applies to: clip20/)
  })
})
