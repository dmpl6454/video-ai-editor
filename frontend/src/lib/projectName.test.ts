// QA-099: a project is never shown by its raw session id, and the picker tells
// same-named copies apart by when they were last edited.
import { describe, expect, it } from 'vitest'
import { editedLabel, projectLabel } from './projectName'

describe('projectLabel', () => {
  it('never shows a session id', () => {
    expect(projectLabel('s_327a167d67', 's_327a167d67')).toBe('Untitled project')
    expect(projectLabel('s_398e1ebaba')).toBe('Untitled project')
    expect(projectLabel('', 's_1')).toBe('Untitled project')
    expect(projectLabel(undefined)).toBe('Untitled project')
  })
  it('keeps a real name', () => {
    expect(projectLabel('  Trip to Goa ', 's_abcdef1234')).toBe('Trip to Goa')
    expect(projectLabel('Untitled project 3', 's_abcdef1234')).toBe('Untitled project 3')
  })
})

describe('editedLabel', () => {
  const now = new Date(2026, 8, 26, 12, 0, 0).getTime()
  const ago = (s: number) => now / 1000 - s
  it('reads as a relative time, then a date', () => {
    expect(editedLabel(ago(20), now)).toBe('edited just now')
    expect(editedLabel(ago(5 * 60), now)).toBe('edited 5 min ago')
    expect(editedLabel(ago(3 * 3600), now)).toBe('edited 3 h ago')
    expect(editedLabel(new Date(2026, 8, 24, 9).getTime() / 1000, now)).toBe('edited Sep 24')
    expect(editedLabel(new Date(2025, 8, 24, 9).getTime() / 1000, now)).toBe('edited Sep 24, 2025')
  })
  it('is empty without a time', () => {
    expect(editedLabel(undefined, now)).toBe('')
    expect(editedLabel(0, now)).toBe('')
  })
})
