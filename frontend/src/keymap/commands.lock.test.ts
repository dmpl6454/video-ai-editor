import { describe, expect, it, vi } from 'vitest'
import type { Store } from './commands'

// The command registry imports the store, which reads localStorage at module
// load; node has none (store.test.ts stubs it the same way).
vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {}, removeItem: () => {} })
const { COMMAND_BY_ID } = await import('./commands')

function fakeStore(result: unknown) {
  const calls: { tool: string; args: unknown }[] = []
  let cleared = 0
  const s = {
    selection: 'c_red', multiSelection: [] as string[],
    dispatch: async (tool: string, args: unknown) => { calls.push({ tool, args }); return result },
    clearSelection: () => { cleared++ },
  }
  return { s: s as unknown as Store, calls, cleared: () => cleared }
}

describe('ripple delete keeps the selection when the edit is refused (QA-023)', () => {
  it('a refused delete (locked track -> dispatch resolves null) leaves the clip selected', async () => {
    const f = fakeStore(null)
    await COMMAND_BY_ID.rippleDelete.run(f.s)
    expect(f.calls).toEqual([{ tool: 'ripple_delete', args: { clip_id: 'c_red' } }])
    expect(f.cleared()).toBe(0)
  })

  it('a successful delete clears the selection', async () => {
    const f = fakeStore({ result: {}, edl_hash: 'h', op: null })
    await COMMAND_BY_ID.rippleDelete.run(f.s)
    expect(f.cleared()).toBe(1)
  })
})
