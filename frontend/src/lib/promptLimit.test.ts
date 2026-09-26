// QA-124: a prompt over the server's 4000-character limit is counted and
// refused in the bar (never sent), a refusal that does reach the server is
// said in words, and a failed run keeps the sentence in the field.
import { describe, expect, it, vi } from 'vitest'

// promptFocus → promptStore → store reads localStorage at module load.
vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {}, removeItem: () => {} })
const { PROMPT_MAX_CHARS, promptLengthNote, promptTooLongError } = await import('./promptLimit')
const { canSubmitPrompt, shouldClearPromptText } = await import('./promptFocus')

// The engine's real answer to a 4700-character prompt (POST …/prompt,
// recorded from a live backend; `input` is echoed capped at 200 characters).
const REAL_422 = '422 Unprocessable Entity: ' + JSON.stringify({ error: {
  code: 'VALIDATION_ERROR',
  message: 'invalid request: message — String should have at most 4000 characters',
  request_id: '0d483f8991cc',
  details: [{ type: 'string_too_long', loc: ['body', 'message'],
              msg: 'String should have at most 4000 characters', input: 'a'.repeat(200),
              ctx: { max_length: 4000 } }],
} })

describe('the prompt length limit (QA-124)', () => {
  it('matches the server limit', () => {
    expect(PROMPT_MAX_CHARS).toBe(4000)
  })

  it('says nothing for a normal sentence, counts near the limit, refuses past it', () => {
    expect(promptLengthNote('make it a reel')).toBeNull()
    expect(promptLengthNote('a'.repeat(3599))).toBeNull()
    expect(promptLengthNote('a'.repeat(3812))).toEqual({ over: false, text: '3,812 / 4,000 characters' })
    expect(promptLengthNote('a'.repeat(4000))?.over).toBe(false)
    const over = promptLengthNote('a'.repeat(4700))
    expect(over?.over).toBe(true)
    expect(over?.text).toContain('Prompt is too long (4,700 / 4,000 characters)')
  })

  it('counts what is sent — the trimmed sentence', () => {
    expect(promptLengthNote(`  ${'a'.repeat(4000)}  \n`)?.over).toBe(false)
  })

  it('does not let Enter / Run send a prompt past the limit', () => {
    expect(canSubmitPrompt('idle', { disabled: false, text: 'a'.repeat(4000) })).toBe(true)
    expect(canSubmitPrompt('idle', { disabled: false, text: 'a'.repeat(4001) })).toBe(false)
  })

  it('turns the server refusal into words instead of "invalid request"', () => {
    const msg = promptTooLongError(REAL_422)
    expect(msg).toBe('Prompt is too long — the limit is 4,000 characters. Shorten it and run it again.')
    expect(msg).not.toMatch(/invalid request/i)
  })

  it('leaves every other failure to the general mapper', () => {
    expect(promptTooLongError('409 Conflict: {"error":{"message":"busy"}}')).toBeNull()
    expect(promptTooLongError('422 Unprocessable Entity: {"error":{"details":[{"type":"missing","loc":["body","token"]}]}}')).toBeNull()
    expect(promptTooLongError('422 Unprocessable Entity: not json')).toBeNull()
  })

  it('empties the field only after a run that finished', () => {
    expect(shouldClearPromptText('running', 'done')).toBe(true)
    expect(shouldClearPromptText('planning', 'error')).toBe(false)
    expect(shouldClearPromptText('running', 'error')).toBe(false)
    expect(shouldClearPromptText('running', 'cancelled')).toBe(false)
    expect(shouldClearPromptText('planning', 'clarify')).toBe(false)
    expect(shouldClearPromptText('idle', 'done')).toBe(false)
  })
})
