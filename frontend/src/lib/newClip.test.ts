// QA-128: every insert path hands back an id the UI can select.
import { describe, expect, it, vi } from 'vitest'

vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {}, removeItem: () => {} })
const { newClipIdOf } = await import('./newClip')

describe('newClipIdOf', () => {
  it('reads add_text / apply_text_template ids', () => {
    expect(newClipIdOf({ id: 't_33c09552', summary: 'Added text' })).toBe('t_33c09552')
  })
  it('reads add_sticker and the sticker upload ids', () => {
    expect(newClipIdOf({ sticker_id: 'st_95b0ea0b', summary: 'Sticker', sticker_count: 1 })).toBe('st_95b0ea0b')
  })
  it('is null when nothing was created', () => {
    expect(newClipIdOf(null)).toBeNull()
    expect(newClipIdOf(undefined)).toBeNull()
    expect(newClipIdOf({ summary: 'nothing' })).toBeNull()
    expect(newClipIdOf({ id: '' })).toBeNull()
  })
})
