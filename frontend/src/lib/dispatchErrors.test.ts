import { describe, expect, it } from 'vitest'
import { isCancelMessage, stripExceptionPrefix } from './dispatchErrors'

describe('stripExceptionPrefix', () => {
  it('drops the Python class name a failed job carries', () => {
    // api/jobs.py records `f"{type(e).__name__}: {e}"` — the user should see
    // the sentence, not the class.
    expect(stripExceptionPrefix('RuntimeError: upscale only supports media clips'))
      .toBe('upscale only supports media clips')
    expect(stripExceptionPrefix('ValueError: object_erase: bbox must be [x, y, w, h] normalised 0..1'))
      .toBe('object_erase: bbox must be [x, y, w, h] normalised 0..1')
    expect(stripExceptionPrefix('HTTPException: (400, "nope")')).toBe('(400, "nope")')
  })

  it('leaves a plain message untouched', () => {
    // Sync failures come through the hardening envelope with no prefix, and a
    // tool name followed by a colon is not an exception class.
    expect(stripExceptionPrefix('auto_caption: no clip on v1 to caption'))
      .toBe('auto_caption: no clip on v1 to caption')
    expect(stripExceptionPrefix('422 Unprocessable Entity')).toBe('422 Unprocessable Entity')
    expect(stripExceptionPrefix('')).toBe('')
  })

  it('only strips a leading prefix, never one mid-sentence', () => {
    expect(stripExceptionPrefix('failed with RuntimeError: x')).toBe('failed with RuntimeError: x')
  })
})

describe('isCancelMessage', () => {
  it('recognises the store\'s cancellation sentence', () => {
    expect(isCancelMessage('auto_caption was cancelled')).toBe(true)
    expect(isCancelMessage('upscale was cancelled')).toBe(true)
  })

  it('does not match ordinary failures', () => {
    expect(isCancelMessage('auto_caption failed')).toBe(false)
    expect(isCancelMessage('the job was cancelled by someone')).toBe(false)
  })
})
