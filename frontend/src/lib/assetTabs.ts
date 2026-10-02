// The asset browser's tab strip and sub-navigation (design handoff §2a; the
// brief's §2 table, "Keep this tab order exactly"). ONE list drives the tabs,
// their icons, each tab's sub-nav and the remembered selection; nothing else
// spells a tab or sub-nav label.
//
// The pre-redesign left rail (components/rail/railModel) is mapped onto these
// tabs so its keyboard chords (⌥1…⌥8) and every deep link keep working: the
// rail's `ai` panel is Media › AI media.
import { create } from 'zustand'
import type { IconName } from './icons'
import type { RailId } from '../components/rail/railModel'

export const ASSET_TABS = [
  'Media', 'Audio', 'Text', 'Stickers', 'Effects', 'Transitions', 'Captions', 'Filters', 'Adjustment', 'Templates',
  'AI avatars',
] as const
export type AssetTab = typeof ASSET_TABS[number]

export const TAB_ICONS: Record<AssetTab, IconName> = {
  Media: 'filmStrip', Audio: 'music', Text: 'text', Stickers: 'sticker', Effects: 'effects',
  Transitions: 'transitions', Captions: 'captions', Filters: 'dropHalf', Adjustment: 'inspector',
  Templates: 'grid', 'AI avatars': 'avatar',
}

export const SUBNAV: Record<AssetTab, readonly string[]> = {
  Media: ['Import', 'Media', 'Subprojects', 'Yours', 'AI media', 'Spaces', 'Library'],
  Audio: ['Import', 'Yours', 'AI music', 'Music', 'Sound effects', 'Copyright'],
  Text: ['Add text', 'AI packaging', 'Yours', 'Text effects', 'Text templates', 'Auto captions', 'Local captions'],
  Stickers: ['Yours', 'Stickers'],
  Effects: ['Video effects', 'Body effects'],
  Transitions: ['Favorites', 'Transitions'],
  Captions: ['Auto captions', 'Templates', 'AI packaging', 'Auto lyrics', 'Add captions'],
  Filters: ['Favorites', 'Filters'],
  Adjustment: ['Add adjustment', 'Yours', 'LUT'],
  Templates: ['Favorites', 'Templates'],
  'AI avatars': ['AI avatar', 'Voiceover'],
}

/** The left rail's ids → the tab (and sub-nav) they mean here. */
export const RAIL_TO_TAB: Record<RailId, { tab: AssetTab; sub?: string }> = {
  media: { tab: 'Media', sub: 'Import' },
  audio: { tab: 'Audio', sub: 'Import' },
  text: { tab: 'Text', sub: 'Add text' },
  stickers: { tab: 'Stickers', sub: 'Stickers' },
  effects: { tab: 'Effects', sub: 'Video effects' },
  transitions: { tab: 'Transitions', sub: 'Transitions' },
  captions: { tab: 'Captions', sub: 'Auto captions' },
  ai: { tab: 'Media', sub: 'AI media' },
}

/** The sub-nav row a tab opens on the first time (the catalogue itself,
 *  not an empty Favorites / Yours list; the reference's order is kept). */
export const DEFAULT_SUB: Partial<Record<AssetTab, string>> = {
  Stickers: 'Stickers', Transitions: 'Transitions', Filters: 'Filters', Templates: 'Templates',
}

export function asAssetTab(v: unknown): AssetTab | null {
  return ASSET_TABS.includes(v as AssetTab) ? (v as AssetTab) : null
}

export const TAB_KEY = 'aive.assetTab'
const SUB_KEY = 'aive.assetSub'

function read(key: string): string | null {
  try { return localStorage.getItem(key) } catch { return null }
}
function write(key: string, v: string): void {
  try { localStorage.setItem(key, v) } catch { /* private mode */ }
}

function readSubs(): Partial<Record<AssetTab, string>> {
  const raw = read(SUB_KEY)
  if (!raw) return {}
  try {
    const o = JSON.parse(raw) as Record<string, unknown>
    const out: Partial<Record<AssetTab, string>> = {}
    for (const t of ASSET_TABS) {
      const s = o[t]
      if (typeof s === 'string' && SUBNAV[t].includes(s)) out[t] = s
    }
    return out
  } catch { return {} }
}

interface AssetBrowserState {
  tab: AssetTab
  subs: Partial<Record<AssetTab, string>>
  sub(): string
  setTab(tab: AssetTab, sub?: string): void
  setSub(sub: string): void
}

export const useAssetBrowser = create<AssetBrowserState>((set, get) => ({
  tab: asAssetTab(read(TAB_KEY)) ?? 'Media',
  subs: readSubs(),
  sub: () => {
    const t = get().tab
    return get().subs[t] ?? DEFAULT_SUB[t] ?? SUBNAV[t][0]
  },
  setTab: (tab, sub) => {
    write(TAB_KEY, tab)
    if (sub && SUBNAV[tab].includes(sub)) {
      const subs = { ...get().subs, [tab]: sub }
      write(SUB_KEY, JSON.stringify(subs))
      set({ tab, subs })
    } else set({ tab })
  },
  setSub: (sub) => {
    const t = get().tab
    if (!SUBNAV[t].includes(sub)) return
    const subs = { ...get().subs, [t]: sub }
    write(SUB_KEY, JSON.stringify(subs))
    set({ subs })
  },
}))

export const assetTabId = (t: AssetTab) => `asset-tab-${t.replace(/\s+/g, '-').toLowerCase()}`
export const assetPanelId = (t: AssetTab) => `asset-panel-${t.replace(/\s+/g, '-').toLowerCase()}`
