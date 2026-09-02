// Ad-hoc normalised rectangles other panels want drawn over the preview (the
// AI panel's bbox fields for object_erase / motion_track). Drawn by
// components/SafeZones.tsx; written by components/AiToolForm.tsx. Kept as a
// separate store so neither side touches store.ts. Coordinates are 0..1 of
// the canvas, same convention as safeZones.ts.
import { create } from 'zustand'
import type { Rect } from './safeZones'

export interface GuideRect { rect: Rect; label: string }
interface GuideRectsState {
  rects: Record<string, GuideRect>
  set: (id: string, g: GuideRect) => void
  clear: (id: string) => void
}
export const useGuideRects = create<GuideRectsState>((set) => ({
  rects: {},
  set: (id, g) => set((s) => ({ rects: { ...s.rects, [id]: g } })),
  clear: (id) => set((s) => { const { [id]: _drop, ...rest } = s.rects; return { rects: rest } }),
}))
