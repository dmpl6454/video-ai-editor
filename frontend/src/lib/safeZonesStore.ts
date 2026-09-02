// The safe-zone toggle's state, shared by the TopBar control and the Preview
// overlay. A separate module-level Zustand store (the toast.ts pattern) rather
// than a field on store.ts: this is view chrome, not project state — it must
// not ride along with the EDL, undo or the session, and it persists per
// browser profile (lib/safeZones.ts) rather than per project.
import { create } from 'zustand'
import { readStoredMode, writeStoredMode, type SafeZoneMode } from './safeZones'

interface SafeZonesState {
  mode: SafeZoneMode
  setMode: (mode: SafeZoneMode) => void
}

export const useSafeZones = create<SafeZonesState>((set) => ({
  mode: readStoredMode(),
  setMode: (mode) => {
    writeStoredMode(mode)
    set({ mode })
  },
}))
