// Final QA: the Effects panel's own .cube import and "Apply to all clips"
// (they were reachable only through the Prompt bar, which stacked LUTs).
import { describe, expect, it } from 'vitest'
import { isCubeFile, lutApplyArgs, lutSrcFor } from './lutActions'

describe('LUT actions', () => {
  it('apply to all: no clip_id (every main-track clip) and replace, never stack', () => {
    expect(lutApplyArgs({ src: 'warm.cube', intensity: 0.8 })).toEqual({ src: 'warm.cube', intensity: 0.8, replace: true })
  })
  it('one clip: its id, and replace', () => {
    expect(lutApplyArgs({ src: '/w/s/uploads/luts/teal.cube', intensity: 1, clipId: 'c_1' }))
      .toEqual({ src: '/w/s/uploads/luts/teal.cube', intensity: 1, replace: true, clip_id: 'c_1' })
  })
  it('clamps intensity into 0..1', () => {
    expect(lutApplyArgs({ src: 'a.cube', intensity: 1.4 }).intensity).toBe(1)
    expect(lutApplyArgs({ src: 'a.cube', intensity: -1 }).intensity).toBe(0)
  })
  it('a bundled look re-resolves by bare name; an imported one keeps its path', () => {
    expect(lutSrcFor('/app/presets/luts/warm.cube', ['warm.cube'])).toBe('warm.cube')
    expect(lutSrcFor('/w/s/uploads/luts/teal_ab12.cube', ['warm.cube'])).toBe('/w/s/uploads/luts/teal_ab12.cube')
  })
  it('only .cube files are LUTs', () => {
    expect(isCubeFile('Warm Teal.CUBE')).toBe(true)
    expect(isCubeFile('notes.txt')).toBe(false)
    expect(isCubeFile('cube')).toBe(false)
  })
})
