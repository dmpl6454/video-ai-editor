import { describe, expect, it } from 'vitest'
import { encodeQr, hasFinderPatterns, modulesToPath, QUIET_MODULES } from './pairQr'

// This suite deliberately GENERATES codes rather than type-checking a wrapper.
// The failure mode being guarded against is a QR library that builds and
// type-checks but throws at runtime inside PyWebView (`qrcode`'s Buffer
// dependency) — which only a real `encodeQr()` call can catch. Every test here
// therefore runs the encoder for real.

// The exact grammar `api/pairing.py::pair_payload` emits.
const PAYLOAD = 'vaepair:v=1&h=10.120.2.82&p=8765&c=3f2a9c1b7e4d05286a1f9c3e7b0d4a52'

describe('encodeQr', () => {
  it('encodes a real pairing payload into a square module matrix', () => {
    const qr = encodeQr(PAYLOAD)
    expect(qr.count).toBeGreaterThanOrEqual(21)
    // Every QR version is (4v + 17) modules on a side, so the count is always
    // ≡ 21 (mod 4). A matrix that fails this is not a QR code.
    expect((qr.count - 21) % 4).toBe(0)
    expect(qr.modules).toHaveLength(qr.count)
    for (const row of qr.modules) expect(row).toHaveLength(qr.count)
  })

  it('produces the three finder patterns a scanner locks onto', () => {
    // The single assertion that distinguishes a genuine code from a grid of
    // plausible-looking squares.
    expect(hasFinderPatterns(encodeQr(PAYLOAD).modules)).toBe(true)
  })

  it('reserves a four-module quiet zone on every side', () => {
    const qr = encodeQr(PAYLOAD)
    expect(QUIET_MODULES).toBe(4)
    expect(qr.size).toBe(qr.count + 8)
  })

  it('is deterministic for the same payload', () => {
    expect(encodeQr(PAYLOAD).path).toBe(encodeQr(PAYLOAD).path)
  })

  it('encodes a different code for a different claim code', () => {
    const other = PAYLOAD.replace(/c=[0-9a-f]{32}/, 'c=' + 'a'.repeat(32))
    expect(encodeQr(other).path).not.toBe(encodeQr(PAYLOAD).path)
  })

  it('handles lower-case hex, which Alphanumeric mode cannot represent', () => {
    // Byte mode is not an arbitrary choice: the claim code is lower-case hex
    // and QR's Alphanumeric mode has no lower-case letters at all.
    expect(() => encodeQr('vaepair:v=1&h=192.168.1.20&p=8765&c=' + 'abcdef01'.repeat(4))).not.toThrow()
  })

  it('still fits a long .local hostname', () => {
    const long = 'vaepair:v=1&h=' + encodeURIComponent('sudhanshus-macbook-pro-16-inch.local') +
      '&p=8765&c=' + '0'.repeat(32)
    const qr = encodeQr(long)
    expect(qr.count).toBeGreaterThanOrEqual(21)
    expect(hasFinderPatterns(qr.modules)).toBe(true)
  })

  it('refuses an empty payload rather than drawing a code that scans to nothing', () => {
    expect(() => encodeQr('')).toThrow()
  })
})

describe('modulesToPath', () => {
  it('offsets every module by the quiet zone', () => {
    // A single dark module at 0,0 must be drawn at the quiet-zone origin, or
    // the code is painted flush against the panel edge and stops scanning.
    expect(modulesToPath([[true]])).toBe('M4 4h1v1h-1z')
  })

  it('merges a horizontal run into one rectangle', () => {
    // ~550 <rect> nodes inside a panel with a live countdown is the reason.
    expect(modulesToPath([[true, true, true]])).toBe('M4 4h3v1h-3z')
  })

  it('splits a run at a light module', () => {
    expect(modulesToPath([[true, false, true]])).toBe('M4 4h1v1h-1zM6 4h1v1h-1z')
  })

  it('emits nothing for an all-light row', () => {
    expect(modulesToPath([[false, false]])).toBe('')
  })

  it('keeps every drawn module inside the declared viewBox', () => {
    const qr = encodeQr(PAYLOAD)
    const coords = [...qr.path.matchAll(/M(\d+) (\d+)h(\d+)/g)]
    expect(coords.length).toBeGreaterThan(0)
    for (const [, x, y, w] of coords) {
      expect(Number(x)).toBeGreaterThanOrEqual(QUIET_MODULES)
      expect(Number(y)).toBeGreaterThanOrEqual(QUIET_MODULES)
      expect(Number(x) + Number(w)).toBeLessThanOrEqual(qr.size - QUIET_MODULES)
      expect(Number(y) + 1).toBeLessThanOrEqual(qr.size - QUIET_MODULES)
    }
  })
})

describe('hasFinderPatterns', () => {
  it('rejects a matrix that is merely dark', () => {
    const solid = Array.from({ length: 25 }, () => Array.from({ length: 25 }, () => true))
    expect(hasFinderPatterns(solid)).toBe(false)
  })

  it('rejects a matrix too small to be a QR code', () => {
    expect(hasFinderPatterns([[true]])).toBe(false)
  })
})
