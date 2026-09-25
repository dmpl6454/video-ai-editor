// QA-046: History renders the server's undo horizon — the newest `undoDepth`
// ops read as undoable, older ones are greyed below a divider.
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {}, removeItem: () => {} })

describe('History undo horizon', () => {
  it('greys every op past the undo depth and marks where the horizon falls', async () => {
    const { useStore } = await import('../store')
    const ops = Array.from({ length: 5 }, (_, i) => ({ seq: i + 1, tool: 'add_marker', summary: `m${i}`, args: {} }))
    // Server rendering reads the store's initial snapshot, as OpsLog.test does.
    Object.assign(useStore.getInitialState(), { ops, undoDepth: 2 })
    const { OpsLog } = await import('./OpsLog')
    const html = renderToStaticMarkup(createElement(OpsLog))
    expect((html.match(/class="op"/g) ?? []).length).toBe(2)
    expect((html.match(/class="op past-horizon"/g) ?? []).length).toBe(3)
    expect(html.match(/Older edits cannot be undone/g)?.length).toBe(1)
    Object.assign(useStore.getInitialState(), { undoDepth: 5 })
    const all = renderToStaticMarkup(createElement(OpsLog))
    expect(all).not.toContain('past-horizon')
    expect(all).not.toContain('Older edits')
  })

  it('a 3-edit project with undo depth 3 shows no divider — the init op is the footer, not a lost edit', async () => {
    const { useStore } = await import('../store')
    const ops = [
      { seq: 0, tool: 'init', summary: 'Initial empty project', args: {} },
      ...Array.from({ length: 3 }, (_, i) => ({ seq: i + 1, tool: 'add_marker', summary: `m${i}`, args: {} })),
    ]
    Object.assign(useStore.getInitialState(), { ops, undoDepth: 3 })
    const { OpsLog } = await import('./OpsLog')
    const html = renderToStaticMarkup(createElement(OpsLog))
    expect(html).not.toContain('Older edits cannot be undone')
    expect(html).not.toContain('past-horizon')
    expect((html.match(/class="op"/g) ?? []).length).toBe(3)
    expect(html).toMatch(/class="op op-origin"[^>]*>New project</)
    // a real edit past the horizon still gets the divider
    Object.assign(useStore.getInitialState(), { undoDepth: 2 })
    const past = renderToStaticMarkup(createElement(OpsLog))
    expect(past.match(/Older edits cannot be undone/g)?.length).toBe(1)
    expect((past.match(/class="op past-horizon"/g) ?? []).length).toBe(1)
  })
})
