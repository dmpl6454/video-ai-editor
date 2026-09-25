// QA-101: the History panel, rendered for real against the store, shows
// editor labels — no tool ids, no clip ids — with the raw op in the title.
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {}, removeItem: () => {} })

describe('History panel', () => {
  it('renders human labels and hides internal ids', async () => {
    const { useStore } = await import('../store')
    Object.assign(useStore.getInitialState(), {
      ops: [
        { seq: 1, tool: 'add_clip', summary: 'Add clip c_f1487d04 to v1 (0.00–85.00)', args: {} },
        { seq: 2, tool: 'split_at', summary: 'Split at 5.00s on v1 (1 clip(s) split)', args: {} },
      ],
    })
    const { OpsLog } = await import('./OpsLog')
    const html = renderToStaticMarkup(createElement(OpsLog))
    const visible = html.replace(/title="[^"]*"/g, '')
    expect(visible).toContain('<b>Split</b>')
    expect(visible).toContain('<b>Add clip</b>')
    expect(visible).not.toMatch(/split_at|add_clip|c_f1487d04|clip\(s\)/)
    expect(html).toContain('title="split_at — Split at 5.00s on v1 (1 clip(s) split)"')
  })
})
