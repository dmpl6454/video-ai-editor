// Final QA: the FOLDED run summary hung over the top of the preview as an
// absolute drawer (87–125 px against a picture starting at 103 px), hiding the
// top ~22 px of the frame until "Clear" — across reloads. The expanded log
// stays a drawer (its height must not come out of the picture, QA-017); the
// one-line folded strip takes its own row instead.
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { ruleDeclarations } from '../lib/contrast'

const css = readFileSync(new URL('./promptBar.css', import.meta.url), 'utf8')

describe('prompt run log placement', () => {
  it('the open log is a drawer over the preview', () => {
    expect(ruleDeclarations(css, '.prompt-bar > .prompt-log').position).toBe('absolute')
  })
  it('the folded summary sits in flow, so the preview is laid out below it', () => {
    const d = ruleDeclarations(css, '.prompt-bar > .prompt-log.is-collapsed')
    expect(d.position).toBe('static')
    expect(d['box-shadow']).toBe('none')
  })
})
