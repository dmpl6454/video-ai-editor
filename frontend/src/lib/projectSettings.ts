// Per-project settings the engine does not store (brief §7): the imported-
// media policy, the proxy switch and "Arrange layers" (a one-way product
// rule: once on, it stays on for the project). Kept in localStorage under the
// session id (`aive.project.<sid>`), so a project reopened on this computer
// keeps them; a .vae carries the EDL, not these.
import { create } from 'zustand'

export interface ProjectSettings {
  importPolicy: 'copy' | 'stay'
  proxy: boolean
  arrangeLayers: boolean
}

export const DEFAULT_PROJECT_SETTINGS: ProjectSettings = { importPolicy: 'copy', proxy: false, arrangeLayers: false }
export const ONBOARD_KEY = 'aive.onboard.arrange'
const PREFIX = 'aive.project.'

function read(sid: string | null): ProjectSettings {
  if (!sid) return DEFAULT_PROJECT_SETTINGS
  try {
    const raw = localStorage.getItem(PREFIX + sid)
    if (!raw) return DEFAULT_PROJECT_SETTINGS
    const o = JSON.parse(raw) as Partial<ProjectSettings>
    return {
      importPolicy: o.importPolicy === 'stay' ? 'stay' : 'copy',
      proxy: !!o.proxy,
      arrangeLayers: !!o.arrangeLayers,
    }
  } catch { return DEFAULT_PROJECT_SETTINGS }
}

interface State {
  cache: Record<string, ProjectSettings>
  forSession(sid: string | null): ProjectSettings
  save(sid: string, next: Partial<ProjectSettings>): ProjectSettings
}

export const useProjectSettings = create<State>((set, get) => ({
  cache: {},
  forSession: (sid) => (sid && get().cache[sid]) || read(sid),
  save: (sid, next) => {
    const cur = get().forSession(sid)
    // Arrange layers cannot be turned off after activation (the reference's
    // own rule, kept: the brief asks for "a real project-state rule").
    const merged: ProjectSettings = { ...cur, ...next, arrangeLayers: cur.arrangeLayers || !!next.arrangeLayers }
    try { localStorage.setItem(PREFIX + sid, JSON.stringify(merged)) } catch { /* private mode */ }
    set({ cache: { ...get().cache, [sid]: merged } })
    return merged
  },
}))
