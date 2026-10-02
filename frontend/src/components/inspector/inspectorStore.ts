// What the inspector column shows (design §2c): project Details when nothing
// is selected, the Source of a previewed asset, the Clip inspector for a
// selection — plus this product's Chat, which the top bar toggles into the
// same column. The selection lives in the main store; this one holds what
// the main store does not know: the previewed asset and the chat toggle.
import { create } from 'zustand'
import { useStore } from '../../store'

export type InspectorMode = 'details' | 'source' | 'clip' | 'chat'

export interface PreviewedAsset {
  src: string
  name: string
  kind: 'video' | 'audio'
  duration: number | null
  width: number | null
  height: number | null
  still: boolean
  /** The library id, for the file URL. */
  id: string | null
}

export type ClipTab = 'Video' | 'Audio' | 'Speed' | 'Animation' | 'Adjustment' | 'AI stylize' | 'Basic' | 'Voice changer' | 'Text' | 'Sticker'

interface InspectorState {
  /** An asset being previewed in the Player (Source mode), or null. */
  preview: PreviewedAsset | null
  chatOpen: boolean
  mode: InspectorMode
  /** The clip inspector's tab (null = the clip kind's first tab). */
  clipTab: ClipTab | null
  setClipTab(tab: ClipTab | null): void
  previewAsset(a: PreviewedAsset | null): void
  toggleChat(): void
  openChat(): void
  closeChat(): void
}

function modeOf(preview: PreviewedAsset | null, chatOpen: boolean, selection: string | null): InspectorMode {
  if (chatOpen) return 'chat'
  if (selection) return 'clip'
  if (preview) return 'source'
  return 'details'
}

export const useInspector = create<InspectorState>((set, get) => ({
  preview: null,
  chatOpen: false,
  mode: modeOf(null, false, useStore.getState().selection),
  clipTab: null,
  setClipTab: (tab) => set({ clipTab: tab }),
  previewAsset: (a) => {
    // Previewing a source leaves timeline mode: the selection is cleared so
    // the Player shows the file, not the composite (design: "Click asset →
    // source preview + Source inspector").
    if (a) useStore.getState().clearSelection()
    set({ preview: a, mode: modeOf(a, get().chatOpen, a ? null : useStore.getState().selection) })
  },
  toggleChat: () => {
    const chatOpen = !get().chatOpen
    set({ chatOpen, mode: modeOf(get().preview, chatOpen, useStore.getState().selection) })
  },
  openChat: () => set({ chatOpen: true, mode: 'chat' }),
  closeChat: () => set({ chatOpen: false, mode: modeOf(get().preview, false, useStore.getState().selection) }),
}))

// A timeline selection returns the Player to timeline mode and the inspector
// to the clip (design §2 "Interactions": "Click clip → select … player
// returns to timeline mode").
useStore.subscribe((s, prev) => {
  if (s.selection === prev.selection) return
  const st = useInspector.getState()
  const preview = s.selection ? null : st.preview
  useInspector.setState({ preview, mode: modeOf(preview, st.chatOpen, s.selection) })
})
