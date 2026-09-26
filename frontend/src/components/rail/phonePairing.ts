// Whether this build has the iPhone affordance at all (LEFT_RAIL_SPEC §2.5,
// R3): the Media panel's "From iPhone" header action renders only while the
// backend reports `phone_pairing: true` on GET /api/version.
//
// The local-network pairing feature is TEMPORARILY off behind one reversible
// flag (`VAE_PHONE_PAIRING` / `PHONE_PAIRING_ENABLED` in api/pairing.py). With
// it off there is no button, no panel, no phone wording and no request to
// /api/pair/* — the panel's code is not even loaded (ToolPanel lazy-imports
// it on the first open). /api/version is the ONLY channel for the decision
// (lib/versionInfo explains why a 404 probe of /api/pair/* is not one), and
// the reading is strict: anything but a literal `true` is off.
//
// One module-level answer, asked once per page: it moved here from TopBar,
// whose version badge is gone (the version lives in Help's about line).
import { create } from 'zustand'
import { parseVersionInfo } from '../../lib/versionInfo'

interface PhonePairingState {
  /** True only once /api/version answered `phone_pairing: true`. */
  enabled: boolean
}

export const usePhonePairing = create<PhonePairingState>(() => ({ enabled: false }))

let asked: Promise<boolean> | null = null

/** Ask /api/version once; every later call shares the first answer. A failed
 *  request leaves the affordance off (and is logged, never swallowed). */
export function loadPhonePairing(fetchFn: typeof fetch = fetch): Promise<boolean> {
  asked ??= fetchFn('/api/version')
    .then((r) => {
      if (!r.ok) throw new Error(`/api/version -> HTTP ${r.status}`)
      return r.json()
    })
    .then((d) => {
      const enabled = parseVersionInfo(d).phonePairing
      usePhonePairing.setState({ enabled })
      return enabled
    })
    .catch((e) => {
      console.warn('[ToolPanel] version fetch failed:', e)
      usePhonePairing.setState({ enabled: false })
      return false
    })
  return asked
}

/** Tests only: forget the cached answer. */
export function resetPhonePairingForTests(): void {
  asked = null
  usePhonePairing.setState({ enabled: false })
}
