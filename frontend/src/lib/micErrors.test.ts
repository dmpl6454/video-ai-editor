import { describe, expect, it } from 'vitest'
import { MIC_UNAVAILABLE, micErrorMessage } from './micErrors'

// Wave C review: the recorder showed the browser's own words ("Not supported")
// and, with no mediaDevices, told users to "Try the browser-dev mode instead
// of the packaged app".
describe('microphone errors in plain sentences', () => {
  it.each([
    ['NotAllowedError', /Allow Video AI Editor.*Microphone/],
    ['SecurityError', /Allow Video AI Editor.*Microphone/],
    ['NotFoundError', /No microphone/],
    ['NotReadableError', /another app/],
    ['NotSupportedError', /can’t record/],
    ['OverconstrainedError', /can’t record/],
    ['AbortError', /stopped/],
  ])('%s', (name, want) => {
    const msg = micErrorMessage(new DOMException('Not supported', name))
    expect(msg).toMatch(want)
    expect(msg).not.toMatch(/^Not supported$|DOMException|browser-dev/)
  })

  it('keeps an app-written sentence and never shows an empty one', () => {
    expect(micErrorMessage(new Error('Recording too short — nothing captured.')))
      .toBe('Recording too short — nothing captured.')
    expect(micErrorMessage('')).toMatch(/Recording failed/)
  })

  it('says what to do when the window has no microphone access at all', () => {
    expect(MIC_UNAVAILABLE).toMatch(/System Settings › Privacy & Security › Microphone/)
    expect(MIC_UNAVAILABLE).toMatch(/import an audio file/)
    expect(MIC_UNAVAILABLE).not.toMatch(/browser-dev|packaged app/)
  })
})
