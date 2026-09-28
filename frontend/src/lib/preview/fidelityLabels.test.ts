import { describe, expect, it } from 'vitest'
import { approxLabel, approxSummary } from './fidelityLabels'
import { liveApproxReasons, publishLiveApprox } from './liveApprox'

// Review RE: the engine classed Spin, Zoom Out In, a rotated canvas and the
// pitch / Hall voice effects APPROX, but nothing said so on screen.
describe('the ≈ chip words', () => {
  it('names each APPROX reason in words', () => {
    expect(approxLabel('anim:in:spin')).toBe('Spin In animation')
    expect(approxLabel('anim:out:spin')).toBe('Spin Out animation')
    expect(approxLabel('anim:in:zoom_out')).toBe('Zoom Out In animation')
    expect(approxLabel('canvas:blur:rotated')).toBe('Canvas behind a rotated clip')
    expect(approxLabel('audio:voice:deep')).toBe('Voice effect: Deep')
    expect(approxLabel('audio:voice:reverb')).toBe('Voice effect: Hall')
    expect(approxLabel('blend:soft_light')).toBe('Blend: Soft Light')
    expect(approxLabel('audio:limiting')).toBe('Limiter on loud sound')
    expect(approxLabel('something:new')).toBe('something:new')
    expect(approxSummary(['audio:voice:deep', 'anim:in:spin', 'audio:voice:deep']))
      .toBe('Approximate preview: Voice effect: Deep, Spin In animation. The export is exact.')
  })
  it('publishes live overlay reasons once per change', () => {
    publishLiveApprox(['blend:soft_light', 'blend:soft_light'])
    const a = liveApproxReasons()
    publishLiveApprox(['blend:soft_light'])
    expect(liveApproxReasons()).toBe(a)          // unchanged: the same array, no re-render
    publishLiveApprox([])
    expect(liveApproxReasons()).toEqual([])
  })
})
