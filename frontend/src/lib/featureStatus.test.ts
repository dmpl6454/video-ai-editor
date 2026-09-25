import { describe, expect, it } from 'vitest'
import { featureStatus } from './featureStatus'

describe('AI feature status line (QA-101)', () => {
  const base = { packaged_app: false, python: '3.11', anthropic_key_set: false, available: [] }
  it('never prints feature keys or the n/m count', () => {
    const s = featureStatus({ ...base, summary: '13/15 optional features available; missing: interpolate, gpu_transcribe',
      unavailable: [{ key: 'interpolate', feature: 'Smooth slow motion (RIFE)', tools: ['smooth_slow_motion'] },
                    { key: 'gpu_transcribe', feature: 'Faster captions on the GPU', tools: [] }] })!
    expect(s.line).toBe('Some features need a download')
    expect(s.line).not.toMatch(/\d+\/\d+|interpolate|gpu_transcribe/)
    expect(s.missing).toEqual(['Smooth slow motion (RIFE)', 'Faster captions on the GPU'])
    expect(featureStatus({ ...base, summary: '', unavailable: [] })!.line).toBe('All AI features are ready')
    expect(featureStatus(null)).toBeNull()
  })
})
