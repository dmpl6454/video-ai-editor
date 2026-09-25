import { describe, expect, it } from 'vitest'
import { formatDb } from './dbFormat'

describe('formatDb (QA-088)', () => {
  it('uses a real minus sign that a line break cannot orphan', () => {
    expect(formatDb(-12)).toBe('\u221212\u00a0dB')
    expect(formatDb(-12)).not.toMatch(/-/)
  })
  it('keeps half-dB steps and marks boosts', () => {
    expect(formatDb(-6.5)).toBe('\u22126.5\u00a0dB')
    expect(formatDb(3)).toBe('+3\u00a0dB')
    expect(formatDb(0)).toBe('0\u00a0dB')
  })
  it('never has a break opportunity between number and unit', () => {
    for (const v of [-30, -12, -0.5, 0, 6]) expect(formatDb(v)).not.toMatch(/[ \-]/)
  })
})
