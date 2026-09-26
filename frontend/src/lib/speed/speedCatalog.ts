// The speed presets, from the server (`GET /api/speed/presets`, backed by
// `edl/speed_presets.py`) — the SAME table `set_speed` and the agent tool
// read. The browser keeps no copy: a preset added or reshaped server-side
// reaches this menu, the handler and the agent together (wave D, lane S2).
//
// Fetched once per page and shared; a failure is not cached, so the next
// render of the Speed section asks again.

import { useEffect, useState } from 'react'
import { limitsFromCatalog, setCurveLimits } from './curveMath'

export interface SpeedPreset {
  id: string
  label: string
  hint: string
  /** In the Curve menu (CapCut's six); the others are agent/prompt names. */
  menu: boolean
  points: [number, number][]
}

export interface SpeedCatalog {
  presets: SpeedPreset[]
  custom: string
  curve_range: [number, number]
  constant_range: [number, number]
  max_points: number
  freeze_default: number
  freeze_range: [number, number]
}

const URL_PRESETS = '/api/speed/presets'
let pending: Promise<SpeedCatalog> | null = null

function isCatalog(v: unknown): v is SpeedCatalog {
  const c = v as SpeedCatalog
  return !!c && Array.isArray(c.presets) && c.presets.every((p) =>
    typeof p.id === 'string' && typeof p.label === 'string' && Array.isArray(p.points))
}

export function loadSpeedCatalog(): Promise<SpeedCatalog> {
  if (!pending) {
    pending = fetch(URL_PRESETS, { headers: { Accept: 'application/json' } })
      .then(async (r) => {
        if (!r.ok) throw new Error(`speed presets: HTTP ${r.status}`)
        const body: unknown = await r.json()
        if (!isCatalog(body)) throw new Error('speed presets: unexpected answer')
        // the curve editor's range and point budget come from here too
        setCurveLimits(limitsFromCatalog(body))
        return body
      })
      .catch((e: unknown) => { pending = null; throw e })
  }
  return pending
}

export type CatalogState =
  | { status: 'loading' }
  | { status: 'ready'; catalog: SpeedCatalog }
  | { status: 'error'; message: string }

/** The catalog for a component (loading → ready | error). */
export function useSpeedCatalog(): CatalogState {
  const [state, setState] = useState<CatalogState>({ status: 'loading' })
  useEffect(() => {
    let live = true
    loadSpeedCatalog().then(
      (catalog) => { if (live) setState({ status: 'ready', catalog }) },
      (e: unknown) => { if (live) setState({ status: 'error', message: e instanceof Error ? e.message : String(e) }) },
    )
    return () => { live = false }
  }, [])
  return state
}

/** Test seam: forget the cached catalog. */
export function resetSpeedCatalogForTests(): void { pending = null }
