// The From iPhone gate (LEFT_RAIL_SPEC §2.5, R3): ONE question to
// /api/version, answered strictly — anything but `phone_pairing: true` is off,
// a failure is off, and /api/pair/* is never asked.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { loadPhonePairing, resetPhonePairingForTests, usePhonePairing } from './phonePairing'

const answer = (body: unknown, status = 200) =>
  vi.fn(async (url: string) => {
    expect(url).toBe('/api/version')
    return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
  })

beforeEach(() => resetPhonePairingForTests())
afterEach(() => vi.restoreAllMocks())

describe('loadPhonePairing', () => {
  it('turns the action on only for a literal phone_pairing: true', async () => {
    expect(await loadPhonePairing(answer({ version: '0.7.3', phone_pairing: true }) as typeof fetch)).toBe(true)
    expect(usePhonePairing.getState().enabled).toBe(true)
  })

  it.each([
    [{ version: '0.7.3', phone_pairing: false }],
    [{ version: '0.7.3' }],                          // an older backend omits the key
    [{ version: '0.7.3', phone_pairing: 'true' }],   // a contract violation reads as off
    [{ version: '0.7.3', phone_pairing: 1 }],
  ])('stays off for %j', async (body) => {
    expect(await loadPhonePairing(answer(body) as typeof fetch)).toBe(false)
    expect(usePhonePairing.getState().enabled).toBe(false)
  })

  it('stays off, and says why, when the request fails', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    expect(await loadPhonePairing(answer({ detail: 'boom' }, 500) as typeof fetch)).toBe(false)
    expect(usePhonePairing.getState().enabled).toBe(false)
    expect(warn).toHaveBeenCalled()
  })

  it('asks once per page, and only /api/version', async () => {
    const f = answer({ phone_pairing: true })
    await Promise.all([loadPhonePairing(f as typeof fetch), loadPhonePairing(f as typeof fetch)])
    await loadPhonePairing(f as typeof fetch)
    expect(f).toHaveBeenCalledTimes(1)
    expect(f.mock.calls.every(([u]) => !String(u).includes('/api/pair'))).toBe(true)
  })
})
