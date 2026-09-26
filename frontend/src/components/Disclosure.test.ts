import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { Disclosure } from './Disclosure'

// COHERENCE (wave C review): bare <summary>s drew the browser's blue ▶.
describe('Disclosure', () => {
  it('puts the app chevron in the summary, closed and open', () => {
    const closed = renderToStaticMarkup(createElement(Disclosure, { summary: 'Details', children: 'body' }))
    expect(closed).toMatch(/<summary><svg[^>]*data-icon="chevronRight"/)
    const open = renderToStaticMarkup(createElement(Disclosure, { summary: 'Details', defaultOpen: true, children: 'body' }))
    expect(open).toMatch(/<details[^>]*open=""/)
    expect(open).toMatch(/<summary><svg[^>]*data-icon="chevronDown"/)
  })
})
