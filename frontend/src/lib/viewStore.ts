// Which screen the app shows (design handoff 2026-10-02, §1 Home / §2 Editor):
// the Home / projects screen or the four-panel editor. A module-level Zustand
// store (the layoutStore pattern) so the top bar's home button, a project
// card and the keymap all drive ONE truth.
//
// First paint: a returning user (a remembered `vai.sessionId`) lands in the
// editor, exactly as before the redesign; a first run lands on Home, which is
// the reference journey (S01). `?view=home` / `?view=editor` force either, so
// a test or a deep link never depends on what a previous visit remembered.
import { create } from 'zustand'

export type AppView = 'home' | 'editor'

export const VIEW_PARAM = 'view'

export function initialView(search: string, remembered: string | null): AppView {
  const forced = new URLSearchParams(search).get(VIEW_PARAM)
  if (forced === 'home' || forced === 'editor') return forced
  return remembered ? 'editor' : 'home'
}

function rememberedSession(): string | null {
  try { return localStorage.getItem('vai.sessionId') } catch { return null }
}

interface ViewState {
  view: AppView
  /** Where the editor should land when it opens: an asset tab to show first
   *  (the Home sidebar's Templates row), else nothing. */
  openWith: string | null
  showHome(): void
  showEditor(openWith?: string | null): void
  consumeOpenWith(): string | null
}

export const useViewStore = create<ViewState>((set, get) => ({
  view: typeof window === 'undefined' ? 'editor' : initialView(window.location.search, rememberedSession()),
  openWith: null,
  showHome: () => set({ view: 'home' }),
  showEditor: (openWith = null) => set({ view: 'editor', openWith }),
  consumeOpenWith: () => { const v = get().openWith; if (v) set({ openWith: null }); return v },
}))
