// QA-019: a clarify card must own the keyboard. These are the three decisions
// PromptBar and ClarifyCard now make through lib/promptFocus; before the fix
// the first one said "refocus the input" for a clarify pause, which is what
// sent Enter back to the prompt (316 re-plans, 0 answers in the QA pass).
import { describe, expect, it, vi } from 'vitest'

vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {}, removeItem: () => {} })
const { canSubmitPrompt, shouldRefocusPrompt } = await import('./promptFocus')
const { clarifyEnterAction } = await import('./clarifyKeys')

describe('shouldRefocusPrompt', () => {
  it('leaves focus in the clarify card when a plan pauses to ask', () => {
    // The card sits INSIDE the bar's form and has just focused itself.
    expect(shouldRefocusPrompt('planning', 'clarify', 'inside-bar')).toBe(false)
    expect(shouldRefocusPrompt('running', 'clarify', 'none')).toBe(false)
  })

  it('still returns focus to the input when a run ends', () => {
    expect(shouldRefocusPrompt('running', 'done', 'inside-bar')).toBe(true)
    expect(shouldRefocusPrompt('verifying', 'error', 'none')).toBe(true)
  })

  it('never steals focus the user put elsewhere, and ignores non-endings', () => {
    expect(shouldRefocusPrompt('running', 'done', 'elsewhere')).toBe(false)
    expect(shouldRefocusPrompt('idle', 'planning', 'inside-bar')).toBe(false)
    expect(shouldRefocusPrompt('clarify', 'idle', 'inside-bar')).toBe(false)
  })
})

describe('canSubmitPrompt', () => {
  it('does not re-plan over an open clarify card', () => {
    expect(canSubmitPrompt('clarify', { disabled: false, text: 'add my brand kit' })).toBe(false)
  })

  it('runs a non-empty prompt when idle or finished, and never while busy', () => {
    expect(canSubmitPrompt('idle', { disabled: false, text: 'remove the ums' })).toBe(true)
    expect(canSubmitPrompt('done', { disabled: false, text: 'again' })).toBe(true)
    expect(canSubmitPrompt('running', { disabled: false, text: 'x' })).toBe(false)
    expect(canSubmitPrompt('idle', { disabled: true, text: 'x' })).toBe(false)
    expect(canSubmitPrompt('idle', { disabled: false, text: '   ' })).toBe(false)
  })
})

describe('clarifyEnterAction', () => {
  it('lets Enter click the focused action button (Start / Skip / Cancel)', () => {
    expect(clarifyEnterAction({ tagName: 'BUTTON', role: null })).toBe('native')
  })

  it('answers the card from a field or a picked chip', () => {
    expect(clarifyEnterAction({ tagName: 'INPUT', role: null })).toBe('submit')
    expect(clarifyEnterAction({ tagName: 'BUTTON', role: 'radio' })).toBe('submit')
    expect(clarifyEnterAction({ tagName: 'DIV', role: 'group' })).toBe('submit')
  })

  it('keeps Enter as a newline in a multi-line field', () => {
    expect(clarifyEnterAction({ tagName: 'textarea' })).toBe('ignore')
  })
})
