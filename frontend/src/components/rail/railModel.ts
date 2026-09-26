// THE list behind the left tool rail (docs/design/LEFT_RAIL_SPEC.md §2.2): one
// entry drives the rail tab, its tooltip, the tool panel's title and (from R4)
// its keyboard command. Nothing else in the app spells a rail label or id.
//
// R1 shipped six items; Text and Captions joined in R2 (out of the top bar),
// which completes CapCut's order. A stored `vai.leftTab` of any of the eight
// ids is valid, so no phase ever needed a migration.
import type { IconName } from '../../lib/icons'
import { AI_CATALOG } from '../../lib/aiCatalog'

export type RailId =
  | 'media' | 'audio' | 'text' | 'stickers' | 'effects' | 'transitions' | 'captions' | 'ai'

export interface RailItem {
  id: RailId
  /** The visible label AND the tab's exact accessible name. */
  label: string
  icon: IconName
  /** The tooltip's description (the label leads the tooltip). */
  tip: string
  /** The keymap command that shows this panel. A chord is shown (tooltip,
   *  panel header, aria-keyshortcuts) only while the live keymap binds it, so
   *  the rail never names a key that does nothing (components/CommandKey). */
  command: string
}

export const RAIL_ITEMS: readonly RailItem[] = [
  { id: 'media', label: 'Media', icon: 'film', tip: 'Footage, photos and the project bin', command: 'panelMedia' },
  { id: 'audio', label: 'Audio', icon: 'music', tip: 'Music, voiceover and the mix', command: 'panelAudio' },
  { id: 'text', label: 'Text', icon: 'text', tip: 'Titles, styles and templates', command: 'panelText' },
  { id: 'stickers', label: 'Stickers', icon: 'sticker', tip: 'Emoji and stickers', command: 'panelStickers' },
  { id: 'effects', label: 'Effects', icon: 'effects', tip: 'Filters, LUT looks and effects', command: 'panelEffects' },
  { id: 'transitions', label: 'Transitions', icon: 'transitions', tip: 'Transitions for a cut', command: 'panelTransitions' },
  { id: 'captions', label: 'Captions', icon: 'captions', tip: 'Auto captions and subtitles', command: 'panelCaptions' },
  { id: 'ai', label: 'AI', icon: 'ai', tip: `Every AI tool (${AI_CATALOG.length})`, command: 'panelAI' },
]

export const DEFAULT_RAIL_ID: RailId = 'media'

export function railItem(id: RailId): RailItem {
  return RAIL_ITEMS.find((r) => r.id === id) ?? RAIL_ITEMS[0]
}

/** A stored or requested id the rail actually shows, else null. */
export function asRailId(v: unknown): RailId | null {
  return RAIL_ITEMS.some((r) => r.id === v) ? (v as RailId) : null
}

/** The DOM ids that tie a rail tab to its tabpanel (aria-controls/-labelledby). */
export const railTabId = (id: RailId) => `rail-tab-${id}`
export const railPanelId = (id: RailId) => `tool-panel-${id}`

/** Roving-tabindex step for a vertical tablist: ↑/↓ wrap, Home/End jump.
 *  Returns the new index, or -1 for a key the rail does not handle. */
export function railKeyStep(key: string, index: number, count: number): number {
  if (count <= 0) return -1
  switch (key) {
    case 'ArrowDown': return (index + 1) % count
    case 'ArrowUp': return (index - 1 + count) % count
    case 'Home': return 0
    case 'End': return count - 1
    default: return -1
  }
}

/** A keymap chord ("Alt+Digit1", "Mod+Alt+KeyK") in the ARIA
 *  `aria-keyshortcuts` syntax ("Alt+1", "Meta+Alt+K"). `isMac` picks what
 *  the platform's primary modifier is called. */
export function ariaKeyshortcuts(chord: string, isMac: boolean): string {
  if (!chord) return ''
  return chord.split('+').map((p) => {
    if (p === 'Mod') return isMac ? 'Meta' : 'Control'
    if (p === 'Ctrl') return 'Control'
    if (p.startsWith('Key')) return p.slice(3)
    if (p.startsWith('Digit')) return p.slice(5)
    return PUNCTUATION[p] ?? p
  }).join('+')
}

/** Punctuation `code`s as the key VALUE aria-keyshortcuts names ("Meta+,"
 *  for ⌘, — "Comma" is not a key value; R3's rail foot carries Settings). */
const PUNCTUATION: Record<string, string> = {
  Backslash: '\\', Comma: ',', Period: '.', Slash: '/', Semicolon: ';', Quote: "'",
  BracketLeft: '[', BracketRight: ']', Minus: '-', Equal: '=', Backquote: '`',
}
