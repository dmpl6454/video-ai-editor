// The speed badge on a timeline clip (wave D3, lane E3) — CapCut's "2.0x" /
// "Hero" / "Freeze" pill, so a retimed clip reads as retimed at a glance.
//
// Two pure pieces the Timeline canvas draws from:
//  * `speedBadgeText(clip)` — WHAT the badge says: a constant speed as
//    "2.0x" / "0.5x" / "0.25x", a speed curve by its preset's label ("Hero",
//    "Jump Cut"; a hand-drawn curve is "Curve"), a freeze frame "Freeze".
//    Normal speed (1x) has no badge.
//  * `layoutSpeedBadge(...)` — WHERE, and at which size: the top-right corner
//    of the clip's VISIBLE part (sticky, like the name is sticky on the left),
//    clear of a transition bowtie on the clip's tail. It degrades with the
//    zoom — icon + text → text → icon alone → nothing — so whatever is drawn
//    is readable, and it hands back the right edge the clip NAME must stop at
//    (`nameMaxRight`), so the two never overlap. The waveform is drawn with
//    the badge's rect cut out (Timeline: `excludeRect`), so the badge never
//    sits on top of it either.

import { clipFreeze, isMediaClip, type AnyClip } from '../types'
import { loadSpeedCatalog } from './speed/speedCatalog'

export type BadgeKind = 'constant' | 'curve' | 'freeze'

export interface BadgeText { text: string; kind: BadgeKind }

/** Preset labels from the server catalog, once it has loaded (the browser
 *  keeps no copy of the table); until then a preset id reads title-cased,
 *  which is what every label in `edl/speed_presets.py` is. */
let presetLabels: Record<string, string> | null = null

export function rememberPresetLabels(presets: readonly { id: string; label: string }[]): void {
  presetLabels = Object.fromEntries(presets.map((p) => [p.id, p.label]))
}

let asked = false
function askCatalog(): void {
  if (asked) return
  asked = true
  loadSpeedCatalog().then((c) => rememberPresetLabels(c.presets), () => { asked = false })
}

/** Test seam. */
export function resetPresetLabelsForTests(): void { presetLabels = null; asked = true }

function titleCase(id: string): string {
  return id.split(/[_\s-]+/).filter(Boolean).map((w) => w[0].toUpperCase() + w.slice(1)).join(' ')
}

/** "2.0x", "0.5x", "1.5x", "0.25x", "12x": one decimal unless the value
 *  needs two (CapCut shows 0.25x, not 0.3x). */
export function formatSpeed(v: number): string {
  if (v >= 10) return `${Math.round(v)}x`
  const one = Math.round(v * 10) / 10
  return Math.abs(one - v) < 1e-6 ? `${one.toFixed(1)}x` : `${(Math.round(v * 100) / 100).toFixed(2)}x`
}

/** What the badge on `c` says, or null for a clip at normal speed. */
export function speedBadgeText(c: AnyClip): BadgeText | null {
  if (!isMediaClip(c)) return null
  if (clipFreeze(c) !== null) return { text: 'Freeze', kind: 'freeze' }
  const sp = (c as unknown as { speed?: unknown }).speed
  if (typeof sp === 'number') {
    if (!(sp > 0) || Math.abs(sp - 1) < 1e-6) return null
    return { text: formatSpeed(sp), kind: 'constant' }
  }
  if (sp && typeof sp === 'object' && Array.isArray((sp as { curve?: unknown }).curve)) {
    askCatalog()
    const name = (sp as { name?: unknown }).name
    if (typeof name === 'string' && name && name !== 'custom') {
      return { text: presetLabels?.[name] ?? titleCase(name), kind: 'curve' }
    }
    return { text: 'Curve', kind: 'curve' }
  }
  return null
}

// --------------------------------------------------------------------------- layout

export const BADGE_H = 12
export const BADGE_ICON = 8
const PAD_X = 3
const GAP = 2
const EDGE = 3            // inset from the clip's visible right edge
const TOP = 3             // below the clip rect's top edge (y + 4)
const NAME_GAP = 4        // air between the clip name and the badge

export interface BadgeLayout {
  x: number
  y: number
  w: number
  h: number
  /** '' when only the icon fits */
  text: string
  icon: boolean
  /** The clip name must end left of this x. */
  nameMaxRight: number
}

export interface BadgeGeometry {
  clipX: number
  clipW: number
  /** the visible lane span: left edge after the label column, right edge */
  viewLeft: number
  viewRight: number
  /** clip rect top (the row's y + 4) */
  rectTop: number
  /** px reserved on the clip's tail (a transition bowtie sits there) */
  tailReserve?: number
}

/** Where the badge goes and how much of it fits, or null when not even the
 *  icon fits (a sliver of a clip). */
export function layoutSpeedBadge(
  text: string, g: BadgeGeometry, measure: (s: string) => number,
): BadgeLayout | null {
  const visL = Math.max(g.clipX, g.viewLeft)
  const visR = Math.min(g.clipX + g.clipW, g.viewRight)
  const right = visR - EDGE - (g.clipX + g.clipW <= g.viewRight ? (g.tailReserve ?? 0) : 0)
  const room = right - (visL + EDGE)
  const tw = measure(text)
  const tiers: { w: number; text: string; icon: boolean }[] = [
    { w: PAD_X + BADGE_ICON + GAP + tw + PAD_X, text, icon: true },
    { w: PAD_X + tw + PAD_X, text, icon: false },
    { w: PAD_X + BADGE_ICON + PAD_X, text: '', icon: true },
  ]
  // The full badge only when it leaves the name some room on a wide clip; on
  // a narrow one the badge outranks the name (CapCut hides the name first).
  const pick = tiers.find((t) => t.w <= room)
  if (!pick) return null
  const x = right - pick.w
  return { x, y: g.rectTop + TOP, w: pick.w, h: BADGE_H, text: pick.text, icon: pick.icon, nameMaxRight: x - NAME_GAP }
}

/** Do two rects overlap (a test helper and the Timeline's own guard)? */
export function rectsOverlap(
  a: { x: number; y: number; w: number; h: number }, b: { x: number; y: number; w: number; h: number },
): boolean {
  return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h
}
