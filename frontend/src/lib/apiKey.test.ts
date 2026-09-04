import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { keyIsMissing, useApiKey } from './apiKey'

const realFetch = globalThis.fetch

function stub(res: Partial<Response> | (() => never)) {
  globalThis.fetch = vi.fn(async () => {
    if (typeof res === 'function') res()
    return res as Response
  }) as unknown as typeof fetch
}
const json = (status: number, body: unknown): Partial<Response> => ({
  ok: status >= 200 && status < 300, status, statusText: '',
  json: async () => body, text: async () => JSON.stringify(body),
})

beforeEach(() => useApiKey.setState({ status: 'unknown', saving: false, error: null, savedAt: 0 }))
afterEach(() => { globalThis.fetch = realFetch })

describe('key status', () => {
  it('reads a configured / unconfigured backend', async () => {
    stub(json(200, { configured: false }))
    await useApiKey.getState().refresh()
    expect(useApiKey.getState().status).toBe('missing')

    stub(json(200, { configured: true }))
    await useApiKey.getState().refresh()
    expect(useApiKey.getState().status).toBe('configured')
  })

  it('treats a 404 as "this backend has no such route", not as a missing key', async () => {
    // Measured against the live 0.4.1 backend, which answers
    // GET /api/settings/api-key with 404 {"detail":"Not Found"}. The UI hides
    // the whole affordance rather than showing a control that cannot work.
    stub(json(404, { detail: 'Not Found' }))
    await useApiKey.getState().refresh()
    expect(useApiKey.getState().status).toBe('unsupported')
  })

  it('leaves the status alone when the probe itself fails', async () => {
    // A dropped request must never accuse a user who has a perfectly good key,
    // nor disable a chat pane that would have worked.
    stub(() => { throw new Error('Failed to fetch') })
    await useApiKey.getState().refresh()
    expect(useApiKey.getState().status).toBe('unknown')
  })

  it('only ever reports missing on a real answer', () => {
    expect(keyIsMissing('missing')).toBe(true)
    expect(keyIsMissing('unknown')).toBe(false)
    expect(keyIsMissing('unsupported')).toBe(false)
    expect(keyIsMissing('configured')).toBe(false)
  })
})

describe('saving a key', () => {
  it('marks it configured and records when', async () => {
    stub(json(200, { configured: true }))
    expect(await useApiKey.getState().save('  sk-ant-abc  ')).toBe(true)
    expect(useApiKey.getState().status).toBe('configured')
    expect(useApiKey.getState().savedAt).toBeGreaterThan(0)
    expect(useApiKey.getState().error).toBeNull()
  })

  it('never posts an empty key', async () => {
    stub(json(200, { configured: true }))
    expect(await useApiKey.getState().save('   ')).toBe(false)
    expect(globalThis.fetch).not.toHaveBeenCalled()
  })

  it('surfaces the backend sentence when a save is rejected', async () => {
    stub(json(400, { error: { message: 'request failed', details: { message: 'that key was rejected by Anthropic' } } }))
    expect(await useApiKey.getState().save('sk-ant-bad')).toBe(false)
    expect(useApiKey.getState().error).toBe('that key was rejected by Anthropic')
    expect(useApiKey.getState().saving).toBe(false)
  })

  it('degrades to "unsupported" rather than throwing on an older backend', async () => {
    stub(json(404, { detail: 'Not Found' }))
    expect(await useApiKey.getState().save('sk-ant-abc')).toBe(false)
    expect(useApiKey.getState().status).toBe('unsupported')
  })
})
