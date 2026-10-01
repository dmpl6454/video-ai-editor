// Editor Brain (EB1): the `brain.enabled` flag as the frontend sees it.
//
// The backend owns it (brain_setting.py: settings.json `brain.enabled`,
// overridden by VAI_BRAIN_ENABLED; GET/PUT /api/settings/brain). OFF by
// default this wave: with it off the preview card, the right panel and every
// brain route are exactly the 0.8.0 ones — the components below read
// `useBrainEnabled()` and render nothing brain-shaped when it is false. An
// older backend without the route reads as off.

import { create } from 'zustand'
import { api, type BrainSettingsWire } from '../api'

export interface BrainFlagState {
  enabled: boolean
  source: string
  loaded: boolean
  load(): Promise<void>
  set(enabled: boolean): Promise<void>
}

export function normalizeBrainSettings(raw: unknown): BrainSettingsWire | null {
  if (!raw || typeof raw !== 'object') return null
  const r = raw as Record<string, unknown>
  if (typeof r.enabled !== 'boolean') return null
  return { enabled: r.enabled, source: typeof r.source === 'string' ? r.source : 'default',
           default: typeof r.default === 'boolean' ? r.default : false }
}

export const useBrainFlag = create<BrainFlagState>((set) => ({
  enabled: false,
  source: 'default',
  loaded: false,
  load: async () => {
    try {
      const s = normalizeBrainSettings(await api.brainSettings())
      set({ enabled: s?.enabled ?? false, source: s?.source ?? 'default', loaded: true })
    } catch {
      set({ enabled: false, source: 'default', loaded: true })
    }
  },
  set: async (enabled) => {
    const s = normalizeBrainSettings(await api.setBrainSettings(enabled))
    set({ enabled: s?.enabled ?? enabled, source: s?.source ?? 'settings', loaded: true })
  },
}))

/** Whether the brain's surfaces show. */
export function useBrainEnabled(): boolean {
  return useBrainFlag((s) => s.enabled)
}
