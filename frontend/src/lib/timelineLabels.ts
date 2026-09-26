// Timeline text that must not collide or be cut mid-word (QA-118).
//
//  * Clip names were `label.slice(0, w / 6)` — a guess at 6 px per character,
//    cut mid-glyph with no ellipsis, and drawn at the clip's left edge even
//    when that edge was scrolled off-screen. `fitLabel` measures, and
//    `stickyLabelX` keeps the name at the visible start of a long clip.
//  * A marker's label printed on the ruler's own row, so "5.0s" and "marker"
//    merged into one string. Marker labels are chips now, and a ruler tick
//    label that a chip covers is not drawn (`collides`).
//  * The lane tooltip read "Main video — Main video — drop footage here".

export type Measure = (s: string) => number

const ELLIPSIS = '…'

/** `text` shortened with an ellipsis to fit `maxW` px ('' when not even one
 *  character and the ellipsis fit). */
export function fitLabel(text: string, maxW: number, measure: Measure): string {
  if (!text || maxW <= 0) return ''
  if (measure(text) <= maxW) return text
  if (measure(ELLIPSIS) > maxW) return ''
  let lo = 0
  let hi = text.length
  while (lo < hi) {
    const mid = Math.ceil((lo + hi) / 2)
    if (measure(text.slice(0, mid).trimEnd() + ELLIPSIS) <= maxW) lo = mid
    else hi = mid - 1
  }
  return lo > 0 ? text.slice(0, lo).trimEnd() + ELLIPSIS : ''
}

/** Where a clip's name starts: `pad` inside its left edge, or inside the
 *  visible lane area when that edge is scrolled off (a sticky title). */
export function stickyLabelX(clipX: number, viewLeft: number, pad = 6): number {
  return Math.max(clipX, viewLeft) + pad
}

export interface Chip { x: number; w: number; text: string; color?: string }

/** Marker chips along the ruler, left to right; a chip that would overlap the
 *  previous one is dropped (its diamond still marks the time). */
export function markerChips(
  markers: readonly { x: number; label: string; color?: string }[], measure: Measure, maxChars = 16, padX = 4,
): Chip[] {
  const out: Chip[] = []
  for (const m of [...markers].sort((a, b) => a.x - b.x)) {
    const raw = m.label.trim() || 'Marker'
    const text = raw.length > maxChars ? raw.slice(0, maxChars - 1).trimEnd() + ELLIPSIS : raw
    const chip: Chip = { x: m.x + 5, w: measure(text) + padX * 2, text, ...(m.color ? { color: m.color } : {}) }
    const prev = out[out.length - 1]
    if (prev && chip.x < prev.x + prev.w + 2) continue
    out.push(chip)
  }
  return out
}

/** Does the span [x, x + w] overlap any chip? */
export function collides(x: number, w: number, chips: readonly { x: number; w: number }[]): boolean {
  return chips.some((c) => x < c.x + c.w && x + w > c.x)
}

/** "Main video — drop footage here" for a lane called "Main video" whose
 *  purpose already begins with its name (no "Main video — Main video — …"). */
export function laneTooltipHead(name: string, purpose: string): string {
  const [first, ...rest] = purpose.split(' — ')
  if (first.trim().toLowerCase() === name.trim().toLowerCase()) {
    return rest.length ? `${name} — ${rest.join(' — ')}` : name
  }
  return `${name} — ${purpose}`
}

/** The plate behind a clip name (QA-118 remainder, wave C review): the name
 *  was drawn in near-black straight over the dark waveform whenever no
 *  filmstrip was under it (before the sprite arrives, or on a long clip
 *  scrolled past its frames) — unreadable. A plate of --bg-0 at
 *  LABEL_PLATE_ALPHA always sits behind the text, which is --text, so the
 *  name reads the same over a filmstrip, a waveform or a bare clip colour. */
export const LABEL_PLATE_ALPHA = 0.72
export function labelPlate(textX: number, textW: number, rowY: number, rowH: number, padX = 4):
  { x: number; y: number; w: number; h: number } {
  const h = 16
  return { x: textX - padX, y: rowY + rowH / 2 - h / 2, w: textW + padX * 2, h }
}
