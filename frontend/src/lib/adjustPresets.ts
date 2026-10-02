// Saved colour-adjustment presets (brief §5 "Save presets with stable
// identifiers and parameter versions"): the clip's `color_grade` params under
// a name, kept in localStorage (`aive.adjustPresets`), versioned.
export interface AdjustPreset {
  id: string
  name: string
  version: 1
  params: Record<string, number>
  hue: number
}

export const ADJUST_PRESETS_KEY = 'aive.adjustPresets'

export function readAdjustPresets(kv: Pick<Storage, 'getItem'> | null = storage()): AdjustPreset[] {
  try {
    const raw = kv?.getItem(ADJUST_PRESETS_KEY)
    if (!raw) return []
    const v = JSON.parse(raw)
    return Array.isArray(v) ? v.filter((p) => p && typeof p.id === 'string' && typeof p.name === 'string' && p.params) : []
  } catch { return [] }
}

export function saveAdjustPreset(name: string, params: Record<string, number>, kv: Pick<Storage, 'getItem' | 'setItem'> | null = storage()): AdjustPreset {
  const list = readAdjustPresets(kv)
  const preset: AdjustPreset = { id: `adj_${Date.now().toString(36)}`, name, version: 1, params, hue: Math.floor(Math.random() * 360) }
  try { kv?.setItem(ADJUST_PRESETS_KEY, JSON.stringify([preset, ...list])) } catch { /* private mode */ }
  return preset
}

function storage(): Storage | null {
  try { return typeof localStorage === 'undefined' ? null : localStorage } catch { return null }
}
