// Turning a pairing payload into something a phone camera can read.
//
// WHY `qrcode-generator` AND NOT `qrcode`. The obvious package pulls Node
// built-ins on its browser paths and commonly needs a `Buffer` polyfill under
// Vite. That failure mode is the worst kind for this product: `vite build`
// succeeds, `tsc` succeeds, and the page throws "Buffer is not defined" at
// runtime — inside a PyWebView window, which has no console the user will ever
// open. `qrcode-generator` is ~30 KB of dependency-free arithmetic that hands
// back a module matrix; the twenty lines below turn that into an SVG path we
// paint with our own tokens, so the code inherits the editor's theme instead
// of arriving as a black-and-white PNG.
//
// NOTHING HERE FETCHES ANYTHING. The QR is computed in-process. A local-first
// editor must not need a network round trip to show a pairing code, least of
// all one whose entire purpose is to work on a LAN with no internet.
//
// THE PAYLOAD IS A SECRET FOR TEN MINUTES. It carries a single-use claim code
// (`api/pairing.py`), so it is rendered only while the panel is deliberately
// showing it, and no part of it is logged.

import qrcode from 'qrcode-generator'

// Error correction level M — 15% recovery. The payload is short (~60 chars),
// the code is read off a bright screen at close range, and H would add modules
// for a robustness this situation does not need. L would be smaller still but
// leaves nothing for a reflection off a glossy display.
const ERROR_CORRECTION = 'M'

// 0 = let the library pick the smallest version that fits. Pinning a version
// would silently fail the day a hostname gets longer.
const AUTO_TYPE_NUMBER = 0

/** The quiet zone, in modules. Four is the spec's minimum; scanners rely on
 *  it to find the code's edge against whatever is behind it. */
export const QUIET_MODULES = 4

export interface QrCode {
  /** Modules per side, excluding the quiet zone. */
  count: number
  /** Side length including the quiet zone — the SVG's viewBox is `0 0 s s`. */
  size: number
  /** `true` where a module is dark. Indexed `[row][col]`. */
  modules: boolean[][]
  /** An SVG path `d` covering every dark module, ready for `fill`. */
  path: string
}

/**
 * Encode a payload. Throws for an empty string rather than emitting a code
 * that scans to nothing — a blank QR on screen is indistinguishable from a
 * working one until someone tries it.
 */
export function encodeQr(payload: string): QrCode {
  if (!payload) throw new Error('nothing to encode')

  const qr = qrcode(AUTO_TYPE_NUMBER, ERROR_CORRECTION)
  // 'Byte' mode: the payload is a urlencoded query string with lower-case hex
  // in it, and Alphanumeric mode cannot represent lower-case at all.
  qr.addData(payload, 'Byte')
  qr.make()

  const count = qr.getModuleCount()
  const modules: boolean[][] = []
  for (let row = 0; row < count; row++) {
    const line: boolean[] = []
    for (let col = 0; col < count; col++) line.push(qr.isDark(row, col))
    modules.push(line)
  }

  return {
    count,
    size: count + QUIET_MODULES * 2,
    modules,
    path: modulesToPath(modules),
  }
}

/**
 * One `d` string for the whole code, in module units, offset by the quiet zone.
 *
 * Horizontal runs are merged into a single rectangle each. That is not a
 * micro-optimisation: a version-4 code is 33×33, and one <rect> per dark
 * module is ~550 DOM nodes inside a panel that also re-renders a countdown
 * every second. Merged runs cut it to a single <path>, and the geometry is
 * identical because adjacent modules share an edge exactly.
 */
export function modulesToPath(modules: boolean[][]): string {
  const parts: string[] = []
  for (let row = 0; row < modules.length; row++) {
    const line = modules[row]
    let runStart = -1
    for (let col = 0; col <= line.length; col++) {
      const dark = col < line.length && line[col]
      if (dark && runStart === -1) runStart = col
      if (!dark && runStart !== -1) {
        const x = runStart + QUIET_MODULES
        const y = row + QUIET_MODULES
        parts.push(`M${x} ${y}h${col - runStart}v1h-${col - runStart}z`)
        runStart = -1
      }
    }
  }
  return parts.join('')
}

/**
 * A finder pattern is the 7×7 square in three corners that a scanner locks
 * onto. Exported because it is what `pairQr.test.ts` checks to prove a REAL
 * code came back rather than a plausible-looking grid — the one property that
 * distinguishes "we generated a QR" from "we generated a bitmap".
 */
export function hasFinderPatterns(modules: boolean[][]): boolean {
  const n = modules.length
  if (n < 21) return false
  const corners: [number, number][] = [
    [0, 0],
    [0, n - 7],
    [n - 7, 0],
  ]
  return corners.every(([r, c]) => isFinder(modules, r, c))
}

function isFinder(modules: boolean[][], r0: number, c0: number): boolean {
  // The pattern is a 7×7 dark ring, a light ring inside it, and a 3×3 dark
  // core. Checking the ring and the core is enough to be sure.
  for (let i = 0; i < 7; i++) {
    if (!modules[r0][c0 + i] || !modules[r0 + 6][c0 + i]) return false
    if (!modules[r0 + i][c0] || !modules[r0 + i][c0 + 6]) return false
  }
  for (let i = 1; i < 6; i++) {
    if (modules[r0 + 1][c0 + i] || modules[r0 + 5][c0 + i]) return false
    if (modules[r0 + i][c0 + 1] || modules[r0 + i][c0 + 5]) return false
  }
  for (let r = 2; r <= 4; r++) {
    for (let c = 2; c <= 4; c++) if (!modules[r0 + r][c0 + c]) return false
  }
  return true
}
