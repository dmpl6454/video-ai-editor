// The voice-effect presets for the Inspector, from the server (`GET
// /api/voice/presets`, backed by `edl/voice_effects.py`) — the SAME table
// `set_voice_effect`, the agent tool and the Prompt bar read (wave E, F3).
// The preview engine reads the build's dump of it (voiceFxTable.ts, pinned
// equal by tests/test_voice_effects.py); the menu asks the running engine,
// so it always offers what that engine renders.
//
// Fetched once per page and shared; a failure is not cached, so the next
// render of the section asks again.

import { useEffect, useState } from 'react'
import type { VoiceTable } from './voiceFx'

const URL_PRESETS = '/api/voice/presets'
let pending: Promise<VoiceTable> | null = null

function isTable(v: unknown): v is VoiceTable {
  const t = v as VoiceTable
  return !!t && Array.isArray(t.presets) && t.presets.every((p) =>
    typeof p.id === 'string' && typeof p.label === 'string' && typeof p.icon === 'string')
}

export function loadVoiceCatalog(): Promise<VoiceTable> {
  if (!pending) {
    pending = fetch(URL_PRESETS, { headers: { Accept: 'application/json', 'X-VAE-Client': '1' } })
      .then(async (r) => {
        if (!r.ok) throw new Error(`voice presets: HTTP ${r.status}`)
        const body: unknown = await r.json()
        if (!isTable(body)) throw new Error('voice presets: unexpected answer')
        return body
      })
      .catch((e: unknown) => { pending = null; throw e })
  }
  return pending
}

export type VoiceCatalogState =
  | { status: 'loading' }
  | { status: 'ready'; table: VoiceTable }
  | { status: 'error'; message: string }

export function useVoiceCatalog(): VoiceCatalogState {
  const [state, setState] = useState<VoiceCatalogState>({ status: 'loading' })
  useEffect(() => {
    let live = true
    loadVoiceCatalog().then(
      (table) => { if (live) setState({ status: 'ready', table }) },
      (e: unknown) => { if (live) setState({ status: 'error', message: e instanceof Error ? e.message : String(e) }) },
    )
    return () => { live = false }
  }, [])
  return state
}

/** Test seam: forget the cached catalog. */
export function resetVoiceCatalogForTests(): void { pending = null }
