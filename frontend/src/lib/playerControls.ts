// The Player footer's choices (design §2b, brief §6): playback quality,
// viewing zoom, the ratio list — and what each does to the project. Pure, so
// the geometry is unit-tested.
import type { Canvas } from '../types'

export const QUALITIES = ['Full', 'High', 'Medium', 'Low'] as const
export const PZOOMS = ['Fit', '25%', '50%', '100%', '200%'] as const
export type PZoom = typeof PZOOMS[number]

export type RatioId = 'Original' | 'Custom' | '16:9' | '4:3' | '2.35:1' | '2:1' | '1.85:1' | '9:16' | '3:4' | '5.8-inch' | '1:1'

/** The reference's ratio list, in its order. The four the backend names
 *  (`set_aspect_ratio`) keep their sizes; the rest go through `set_canvas`
 *  on a 1080-based short side, even-snapped. "5.8-inch" is the iPhone X
 *  screen (1125 × 2436, 2.165:1 tall) — the reference does not show its
 *  geometry, so this is the proposed one, and the item's title says so. */
export const RATIOS: readonly { id: RatioId; label: string; title?: string; w?: number; h?: number; named?: '9:16' | '16:9' | '1:1' | '4:5' }[] = [
  { id: 'Original', label: 'Original', title: 'Match the first video clip\'s own shape' },
  { id: 'Custom', label: 'Custom', title: 'Set a width and height in Project settings' },
  { id: '16:9', label: '16:9', named: '16:9', w: 1920, h: 1080 },
  { id: '4:3', label: '4:3', w: 1440, h: 1080 },
  { id: '2.35:1', label: '2.35:1', w: 2538, h: 1080 },
  { id: '2:1', label: '2:1', w: 2160, h: 1080 },
  { id: '1.85:1', label: '1.85:1', w: 1998, h: 1080 },
  { id: '9:16', label: '9:16', named: '9:16', w: 1080, h: 1920 },
  { id: '3:4', label: '3:4', w: 1080, h: 1440 },
  { id: '5.8-inch', label: '5.8-inch', title: 'iPhone X screen, 1125 × 2436 (proposed geometry — the reference does not state it)', w: 1126, h: 2436 },
  { id: '1:1', label: '1:1', named: '1:1', w: 1080, h: 1080 },
]

/** Which list entry the canvas is in (tolerant to even-pixel rounding), else
 *  'Custom'. */
export function ratioOf(c: Pick<Canvas, 'w' | 'h'> | null | undefined): RatioId {
  if (!c || !c.w || !c.h) return 'Custom'
  const r = c.w / c.h
  for (const e of RATIOS) {
    if (!e.w || !e.h) continue
    if (Math.abs(e.w / e.h - r) / (e.w / e.h) < 0.01) return e.id
  }
  return 'Custom'
}

/** The dispatch that applies a ratio: the backend's named tool where one
 *  exists (it rescales overlays), else `set_canvas` with the size. Null for
 *  Original (the caller decides from the footage) and Custom. */
export function ratioCommand(id: RatioId, c: Pick<Canvas, 'w' | 'h'>): { tool: string; args: Record<string, unknown> } | null {
  const e = RATIOS.find((r) => r.id === id)
  if (!e || !e.w || !e.h) return null
  if (ratioOf(c) === id) return null
  if (e.named) return { tool: 'set_aspect_ratio', args: { ratio: e.named } }
  return { tool: 'set_canvas', args: { w: e.w, h: e.h } }
}

export function zoomFactor(z: PZoom): number | null {
  return z === 'Fit' ? null : Number(z.replace('%', '')) / 100
}

/** The stage box: at Fit, the largest canvas-shaped box inside 92 % of the
 *  pane; at a percentage, the canvas's own pixel size scaled (so 100 % is
 *  one CSS px per canvas px, and the pane scrolls). */
export function stageSize(c: Pick<Canvas, 'w' | 'h'>, pane: { w: number; h: number }, z: PZoom): { w: number; h: number } {
  const f = zoomFactor(z)
  if (f !== null) return { w: Math.round(c.w * f), h: Math.round(c.h * f) }
  if (pane.w <= 0 || pane.h <= 0) return { w: 0, h: 0 }
  const maxW = pane.w * 0.92, maxH = pane.h * 0.92
  const k = Math.min(maxW / c.w, maxH / c.h)
  return { w: Math.max(1, Math.floor(c.w * k)), h: Math.max(1, Math.floor(c.h * k)) }
}

/** `/api/sessions/{sid}/files/uploads/<rel>` for a file under the session's
 *  uploads; null for a file elsewhere (an allowed external root), which the
 *  session route cannot serve. */
export function sourceFileUrl(sid: string, src: string): string | null {
  const i = src.replace(/\\/g, '/').indexOf('/uploads/')
  if (i < 0) return null
  const rel = src.replace(/\\/g, '/').slice(i + '/uploads/'.length)
  if (!rel || rel.includes('..')) return null
  return `/api/sessions/${sid}/files/uploads/${rel.split('/').map(encodeURIComponent).join('/')}`
}
