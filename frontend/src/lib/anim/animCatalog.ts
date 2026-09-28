// The clip-animation presets for the Inspector, from the server
// (`GET /api/animations/presets`, backed by `edl/clip_animations.py`) — the
// SAME table `set_animation`, the agent tool and the renderers read (the
// browser's renderers through the generated clipAnimTable.json). Fetched once
// per page and shared; a failure is not cached, so the next render asks again.

import { useEffect, useState } from 'react'
import type { AnimTable } from './clipAnim'

const URL_PRESETS = '/api/animations/presets'
let pending: Promise<AnimTable> | null = null

function isTable(v: unknown): v is AnimTable {
  const t = v as AnimTable
  const ok = (a: unknown) => Array.isArray(a) && a.every((p) =>
    typeof p?.id === 'string' && typeof p?.label === 'string' && typeof p?.kind === 'string')
  return !!t && ok(t.in) && ok(t.out) && ok(t.combo) && Array.isArray(t.dur_range)
}

export function loadAnimCatalog(): Promise<AnimTable> {
  if (!pending) {
    pending = fetch(URL_PRESETS, { headers: { Accept: 'application/json' } })
      .then(async (r) => {
        if (!r.ok) throw new Error(`animation presets: HTTP ${r.status}`)
        const body: unknown = await r.json()
        if (!isTable(body)) throw new Error('animation presets: unexpected answer')
        return body
      })
      .catch((e: unknown) => { pending = null; throw e })
  }
  return pending
}

export type AnimCatalogState =
  | { status: 'loading' }
  | { status: 'ready'; table: AnimTable }
  | { status: 'error'; message: string }

export function useAnimCatalog(): AnimCatalogState {
  const [state, setState] = useState<AnimCatalogState>({ status: 'loading' })
  useEffect(() => {
    let live = true
    loadAnimCatalog().then(
      (table) => { if (live) setState({ status: 'ready', table }) },
      (e: unknown) => { if (live) setState({ status: 'error', message: e instanceof Error ? e.message : String(e) }) },
    )
    return () => { live = false }
  }, [])
  return state
}

/** Test seam: forget the cached catalog. */
export function resetAnimCatalogForTests(): void { pending = null }
