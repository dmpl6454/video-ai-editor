// The /api/version gate that decides whether the iPhone-pairing affordance
// exists in the UI at all.
//
// The defect this guards: the pairing feature is TEMPORARILY off in the shipped
// desktop build (backend flag `VAE_PHONE_PAIRING` / `PHONE_PAIRING_ENABLED` in
// api/pairing.py, surfaced as `phone_pairing` on GET /api/version). If the
// frontend read that field truthily, or treated "key missing" as "unknown, show
// it", the 📱 Phone button would appear in a build whose /api/pair/* routes all
// answer 404 — a dead button on a feature the user was told does not exist.
// And because the fetch is async, the pre-answer value has to read as OFF or
// the button flashes in and out on every boot.
import { describe, expect, it } from 'vitest'
import { parseVersionInfo, VERSION_UNKNOWN } from './versionInfo'

describe('parseVersionInfo — the phone-pairing gate', () => {
  it('shows the affordance only for a literal true from the backend', () => {
    expect(parseVersionInfo({ version: '0.7.0', build: 'abc1234', phone_pairing: true }))
      .toEqual({ version: '0.7.0', build: 'abc1234', phonePairing: true })
  })

  it('hides the affordance when the flag is off', () => {
    expect(parseVersionInfo({ version: '0.7.0', build: 'abc1234', phone_pairing: false }).phonePairing)
      .toBe(false)
  })

  it('hides the affordance when the key is absent (older backend)', () => {
    expect(parseVersionInfo({ version: '0.7.0', build: 'abc1234' }))
      .toEqual({ version: '0.7.0', build: 'abc1234', phonePairing: false })
  })

  // A non-boolean is a backend contract violation. The safe reading of a
  // violation, on a feature whose whole job is opening a socket to the local
  // network, is OFF — never the truthiness of the string "false".
  it.each([['false'], ['true'], [1], [0], [null], [{}], [[]]])(
    'hides the affordance for the non-boolean %o',
    (bad) => {
      expect(parseVersionInfo({ version: '0.7.0', build: '', phone_pairing: bad }).phonePairing)
        .toBe(false)
    },
  )
})

describe('parseVersionInfo — the version badge', () => {
  it('keeps version and build verbatim', () => {
    const info = parseVersionInfo({ version: '1.2.3', build: 'deadbee' })
    expect(info.version).toBe('1.2.3')
    expect(info.build).toBe('deadbee')
  })

  it('empties non-string version/build instead of rendering "undefined"', () => {
    expect(parseVersionInfo({ version: 7, build: null })).toEqual({ ...VERSION_UNKNOWN })
  })

  // /api/version answering with HTML (a dev proxy miss) or a bare string must
  // not throw inside the .then() and lose the badge silently.
  it.each([[null], [undefined], ['<!doctype html>'], [42]])(
    'falls back to VERSION_UNKNOWN for the non-object body %o',
    (body) => {
      expect(parseVersionInfo(body)).toEqual(VERSION_UNKNOWN)
    },
  )
})

describe('VERSION_UNKNOWN — the in-flight value', () => {
  // Load-bearing: TopBar seeds its state with this before GET /api/version
  // answers. A true here would flash the phone button in on every boot.
  it('hides the phone affordance while the version request is in flight', () => {
    expect(VERSION_UNKNOWN.phonePairing).toBe(false)
  })
})
