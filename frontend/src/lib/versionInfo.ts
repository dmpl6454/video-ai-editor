// GET /api/version, normalized for the TopBar badge and — since the 0.7 ship —
// for the one decision that says whether the iPhone-pairing affordance exists
// at all.
//
// WHY this module exists instead of three `d.x || ''` reads inline in TopBar:
// the shipped desktop editor is a standalone CapCut-style editor, and the
// local-network iPhone pairing feature is TEMPORARILY off. The backend gates it
// behind one reversible flag (`PHONE_PAIRING_ENABLED` / env `VAE_PHONE_PAIRING`
// in api/pairing.py) and reports the answer as `phone_pairing` on /api/version.
// That field is the ONLY channel the frontend may use to decide whether the
// phone button and panel render — the UI must never infer it from a 404 on a
// /api/pair/* call, because probing a route that is meant not to exist is how
// you get a button that flickers in and out. A future release flips the flag
// back on and this affordance returns with no frontend change.
//
// The parse is deliberately strict rather than truthy:
//   * an OLDER backend (or any build with the feature off) omits the key, and
//     `undefined` must read as OFF, not as "unknown, show it anyway";
//   * a non-boolean (`"false"`, `0`, `null`) is a backend contract violation,
//     and the safe reading of a violation on a feature that opens a socket to
//     the local network is OFF.
import type { AppVersion } from '../api'

/** The version badge + feature gate, with every field always present. */
export interface VersionInfo {
  /** Semantic app version, e.g. "0.7.0". Empty until /api/version answers. */
  readonly version: string
  /** Git short-sha or baked BUILD_ID. Empty when the build is not stamped. */
  readonly build: string
  /** True only when this build actually serves /api/pair/*. */
  readonly phonePairing: boolean
}

/**
 * What the UI shows while the /api/version request is in flight, and after it
 * fails. `phonePairing: false` is the load-bearing part: the phone button must
 * never flash in on boot and then vanish, so "not yet known" renders as hidden.
 */
export const VERSION_UNKNOWN: VersionInfo = { version: '', build: '', phonePairing: false }

/** A string field, or '' for anything that is not a string. */
function str(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

/** Normalize a raw /api/version body. Never throws — any shape is survivable. */
export function parseVersionInfo(raw: unknown): VersionInfo {
  if (raw === null || typeof raw !== 'object') return VERSION_UNKNOWN
  const body = raw as Partial<AppVersion>
  return {
    version: str(body.version),
    build: str(body.build),
    // Strict identity, not truthiness — see the module note above.
    phonePairing: body.phone_pairing === true,
  }
}
