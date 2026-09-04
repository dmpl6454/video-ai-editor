// Whether Claude chat has a key, and the one way to give it one.
//
// The packaged .app has no terminal and no visible dotfile: the ONLY documented
// route to a working chat pane was hand-creating
// ~/Library/Application Support/Video AI Editor/.env in a Finder-hidden
// folder. So the chat pane failed, said nothing useful, and the fix was
// unreachable from inside the app that needed it.
//
// A module-level Zustand store (the toast.ts / aiRuns.ts pattern) rather than
// component state, because three places need the same answer — the modal that
// sets it, the TopBar affordance that offers it, and the chat pane that is
// unusable without it — and they must not each ask the server.
import { create } from 'zustand'
import { api } from '../api'
import { errorMessage } from './errorMessage'

/** `unknown` = not asked yet, or the probe itself failed — deliberately NOT
 *  `missing`: a transient fetch failure must never accuse a user who has a
 *  perfectly good key, nor disable a chat pane that would have worked.
 *  `unsupported` = the backend has no settings route (404), which is every
 *  build older than this one; the UI then behaves exactly as it always did. */
export type KeyStatus = 'unknown' | 'unsupported' | 'missing' | 'configured'

interface ApiKeyState {
  status: KeyStatus
  saving: boolean
  /** Last save failure, as a sentence — cleared on the next attempt. */
  error: string | null
  /** Bumped on every successful save so a view can show a confirmation
   *  without keeping (or ever re-rendering) the key itself. */
  savedAt: number
  refresh(): Promise<void>
  save(key: string): Promise<boolean>
}

export const useApiKey = create<ApiKeyState>((set, get) => ({
  status: 'unknown',
  saving: false,
  error: null,
  savedAt: 0,

  refresh: async () => {
    try {
      const r = await api.getApiKeyStatus()
      set({ status: !r.supported ? 'unsupported' : r.configured ? 'configured' : 'missing' })
    } catch {
      // Leave the status alone (see KeyStatus): showing "no key" because the
      // probe timed out would be a lie the user cannot argue with.
    }
  },

  save: async (key: string) => {
    const trimmed = key.trim()
    if (!trimmed || get().saving) return false
    set({ saving: true, error: null })
    try {
      const r = await api.setApiKey(trimmed)
      if (!r.supported) {
        set({ status: 'unsupported', saving: false,
              error: 'This build of the app cannot save a key.' })
        return false
      }
      set({ status: r.configured ? 'configured' : 'missing', saving: false,
            savedAt: Date.now() })
      return r.configured
    } catch (e) {
      set({ saving: false, error: errorMessage(e) })
      return false
    }
  },
}))

/** True only when we KNOW there is no key — never on `unknown`/`unsupported`,
 *  so nothing is disabled or nagged about on a guess. */
export function keyIsMissing(status: KeyStatus): boolean {
  return status === 'missing'
}
