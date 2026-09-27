import { afterEach, describe, expect, it } from 'vitest'
import { installTestHook, TEST_HOOK_PARAM } from './testHook'

type HookWindow = { __vaeTest?: { useStore: unknown } }

const store = { getState: () => ({}) }

afterEach(() => {
  delete (globalThis as unknown as HookWindow).__vaeTest
})

describe('installTestHook', () => {
  it('exposes the store only when the page was opened with ?vae-test', () => {
    const w = {} as HookWindow
    expect(installTestHook(`?${TEST_HOOK_PARAM}`, store, w)).toBe(true)
    expect(w.__vaeTest?.useStore).toBe(store)
  })

  it('does nothing for an ordinary launch', () => {
    const w = {} as HookWindow
    expect(installTestHook('', store, w)).toBe(false)
    expect(installTestHook('?session=abc', store, w)).toBe(false)
    expect(w.__vaeTest).toBeUndefined()
  })

  it('reads the flag among other query parameters', () => {
    const w = {} as HookWindow
    expect(installTestHook(`?session=abc&${TEST_HOOK_PARAM}`, store, w)).toBe(true)
  })
})
