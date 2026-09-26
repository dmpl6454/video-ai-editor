import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { BrainRow } from '../lib/promptEvents'
import { brainStatus } from '../lib/brainCopy'
import { BRAINS_HEADING, BrainRows, CHECK_AGAIN } from './BrainRows'

const rows = [
  { id: 'apple_intelligence', label: 'Apple Intelligence', available: true, detail: 'ready' },
  { id: 'claude', label: 'Claude', available: false, detail: 'no key', action: 'add_key' },
] as unknown as BrainRow[]

// COHERENCE (wave C review): the popover and Settings named, marked and
// re-checked the same list differently. One component, one vocabulary.
describe('BrainRows', () => {
  it('uses one heading and one verb', () => {
    expect(BRAINS_HEADING).toBe('Who answers your prompts')
    expect(CHECK_AGAIN).toBe('Check again')
  })
  it('marks availability with one dot and says the add-key hint as plain text', () => {
    const html = renderToStaticMarkup(createElement(BrainRows, { rows, answered: 'apple_intelligence' }))
    expect(html.match(/brain-list-dot" data-on="true"/g)?.length).toBe(1)
    expect(html.match(/brain-list-dot" data-on="false"/g)?.length).toBe(1)
    expect(html).toContain('Add yours in Settings.')
    expect(html).not.toMatch(/<code|<pre|brain-row-fix/)
    expect(html).toContain('>Answered<')
  })
  it('lets Settings replace the hint that would point at itself', () => {
    const html = renderToStaticMarkup(createElement(BrainRows, {
      rows, fixFor: (r: BrainRow, f: string | null) => (r.id === 'claude' ? 'Add your key under Claude above.' : f),
    }))
    expect(html).toContain('Add your key under Claude above.')
    expect(html).not.toContain('Add yours in Settings.')
  })
  it('states each row one way', () => {
    expect(brainStatus(rows[0], null)).toBe('Available')
    expect(brainStatus(rows[1], null)).toBe('Not available')
    expect(brainStatus(rows[0], 'apple_intelligence')).toBe('Answered')
  })
})
