// The editor shell's layout state (docs/design/LEFT_RAIL_SPEC.md §6.1): which
// tool panel the left rail shows and whether it is open, the two side-panel
// widths, and the right panel's open state and tab. A module-level Zustand
// store (the aiRuns.ts / promptStore.ts pattern) so the rail, the tool panel,
// the right panel, App's grid and — from R4 — keyboard commands all drive ONE
// truth. `store.ts` is not touched: from R1 on nothing calls its
// setPanelSize('leftW'|'rightW') or setRightPanelOpen, and R6 deletes them.
//
// Widths are `number | null` (critique H7). Null means "never dragged": App
// then writes no inline --left-w/--right-w and the CSS media-query defaults of
// §1.2 apply, so a first run gets a wider panel on a wider screen. A dragged
// width is stored and wins.
import { create } from 'zustand'
import { createStore, type StateCreator } from 'zustand/vanilla'
import { asRailId, DEFAULT_RAIL_ID, type RailId } from '../components/rail/railModel'
import { browserStorage, readRightTab, writeRightTab, type RightTab } from './rightTab'

export const LEFT_TAB_KEY = 'vai.leftTab'
export const LEFT_OPEN_KEY = 'vai.leftOpen'
export const LEFT_W_KEY = 'vai.leftW'
export const RIGHT_W_KEY = 'vai.rightW'
export const RIGHT_OPEN_KEY = 'vai.rightPanelOpen'

/** A side panel's width range (px). */
export const PANEL_MIN = 180
export const PANEL_MAX = 640
/** The picture + timeline column never goes below this (the grid's own
 *  minmax(var(--center-min), 1fr) guarantees it in CSS; the clamp here only
 *  keeps a stored width from drifting far past what can be drawn). */
export const CENTRE_MIN = 440
/** The two 6 px splitter tracks. */
const SPLITTERS = 12
/** The collapsed right panel's rail (--right-rail-w). */
export const RIGHT_RAIL_W = 36

/** The first-run column widths per viewport width — the same breakpoints as
 *  the tokens in styles.css (§1.2). Used for the drag clamp only; drawing
 *  always comes from CSS. */
export function defaultWidths(viewport: number): { rail: number; left: number; right: number } {
  if (viewport < 1280) return { rail: 48, left: 220, right: 260 }
  if (viewport < 1440) return { rail: 64, left: 240, right: 280 }
  if (viewport < 1920) return { rail: 64, left: 280, right: 280 }
  return { rail: 64, left: 320, right: 320 }
}

/** Clamp a panel width to [PANEL_MIN, PANEL_MAX] and to what leaves the centre
 *  column CENTRE_MIN, never below PANEL_MIN. */
export function clampPanelWidth(px: number, centreBudget = Infinity): number {
  const hi = Math.max(PANEL_MIN, Math.min(PANEL_MAX, centreBudget))
  return Math.round(Math.min(hi, Math.max(PANEL_MIN, px)))
}

interface KV {
  getItem(k: string): string | null
  setItem(k: string, v: string): void
  removeItem?(k: string): void
}

function read(kv: KV | null, key: string): string | null {
  try { return kv?.getItem(key) ?? null } catch { return null }
}
function write(kv: KV | null, key: string, value: string | null): void {
  try {
    if (value === null) kv?.removeItem?.(key)
    else kv?.setItem(key, value)
  } catch { /* storage blocked (private window): the choice lasts this load */ }
}

/** The remembered tool panel. The pre-rail values ('media', 'transitions',
 *  'ai') are still valid ids, so no migration; anything the rail does not
 *  show falls back to Media. */
export function readLeftTab(kv: KV | null): RailId {
  return asRailId(read(kv, LEFT_TAB_KEY)) ?? DEFAULT_RAIL_ID
}

export function readBool(kv: KV | null, key: string, fallback: boolean): boolean {
  const v = read(kv, key)
  return v === 'true' ? true : v === 'false' ? false : fallback
}

/** A stored width, clamped; absent or garbage is null (→ the CSS default).
 *  A legacy value below PANEL_MIN (store.ts allowed 160) clamps up. */
export function readWidth(kv: KV | null, key: string): number | null {
  const raw = read(kv, key)
  if (raw === null || raw.trim() === '') return null
  const n = Number(raw)
  return Number.isFinite(n) ? clampPanelWidth(n) : null
}

export type PanelSide = 'left' | 'right'

export interface AiJump { tool?: string; group?: string; from: RailId; nonce: number }

export interface LayoutState {
  leftTab: RailId
  leftOpen: boolean
  leftW: number | null
  rightW: number | null
  rightOpen: boolean
  rightTab: RightTab
  /** A deep link into the AI panel (R5 consumes it). */
  aiJump: AiJump | null
  /** Show a tool panel. With `toggle`, asking for the panel already shown
   *  collapses (or re-opens) the tool panel instead — a click on the active
   *  rail tab. Any other id selects it and opens the panel. */
  showTab(id: RailId, opts?: { toggle?: boolean }): void
  setLeftOpen(open: boolean): void
  /** Store a dragged width (clamped; returns what was stored), or null to go
   *  back to the CSS default. */
  setPanelWidth(side: PanelSide, px: number | null): number | null
  setRightOpen(open: boolean): void
  setRightTab(tab: RightTab): void
  /** Open the right panel on a tab. */
  showRight(tab: RightTab): void
  jumpToAi(req: Omit<AiJump, 'nonce'>): void
  clearAiJump(): void
}

/** The store's logic over any key-value storage and viewport-width source,
 *  so tests run it on a Map with a fixed width. */
export function layoutStateCreator(kv: KV | null, viewport: () => number): StateCreator<LayoutState> {
  return (set, get) => {
    const openRight = (open: boolean) => {
      write(kv, RIGHT_OPEN_KEY, String(open))
      set({ rightOpen: open })
    }
    const centreBudget = (side: PanelSide): number => {
      const s = get()
      const vw = viewport()
      const d = defaultWidths(vw)
      const other = side === 'left'
        ? (s.rightOpen ? (s.rightW ?? d.right) : RIGHT_RAIL_W)
        : (s.leftOpen ? (s.leftW ?? d.left) : 0)
      return vw - d.rail - other - SPLITTERS - CENTRE_MIN
    }
    return {
      leftTab: readLeftTab(kv),
      leftOpen: readBool(kv, LEFT_OPEN_KEY, true),
      leftW: readWidth(kv, LEFT_W_KEY),
      rightW: readWidth(kv, RIGHT_W_KEY),
      rightOpen: readBool(kv, RIGHT_OPEN_KEY, true),
      rightTab: readRightTab(kv),
      aiJump: null,

      showTab: (id, opts = {}) => {
        const rid = asRailId(id)
        if (!rid) return
        const s = get()
        if (opts.toggle && s.leftTab === rid) {
          get().setLeftOpen(!s.leftOpen)
          return
        }
        write(kv, LEFT_TAB_KEY, rid)
        write(kv, LEFT_OPEN_KEY, 'true')
        set({ leftTab: rid, leftOpen: true })
      },
      setLeftOpen: (open) => {
        write(kv, LEFT_OPEN_KEY, String(open))
        set({ leftOpen: open })
      },
      setPanelWidth: (side, px) => {
        const key = side === 'left' ? LEFT_W_KEY : RIGHT_W_KEY
        const field = side === 'left' ? 'leftW' : 'rightW'
        if (px === null || !Number.isFinite(px)) {
          write(kv, key, null)
          set({ [field]: null } as Partial<LayoutState>)
          return null
        }
        const w = clampPanelWidth(px, centreBudget(side))
        write(kv, key, String(w))
        set({ [field]: w } as Partial<LayoutState>)
        return w
      },
      setRightOpen: openRight,
      setRightTab: (tab) => {
        writeRightTab(kv, tab)
        set({ rightTab: tab })
      },
      showRight: (tab) => {
        writeRightTab(kv, tab)
        write(kv, RIGHT_OPEN_KEY, 'true')
        set({ rightTab: tab, rightOpen: true })
      },
      jumpToAi: (req) => {
        const nonce = (get().aiJump?.nonce ?? 0) + 1
        write(kv, LEFT_TAB_KEY, 'ai')
        write(kv, LEFT_OPEN_KEY, 'true')
        set({ aiJump: { ...req, nonce }, leftTab: 'ai', leftOpen: true })
      },
      clearAiJump: () => set({ aiJump: null }),
    }
  }
}

/** A standalone store instance (tests). */
export function createLayoutStore(kv: KV | null, viewport: () => number) {
  return createStore<LayoutState>()(layoutStateCreator(kv, viewport))
}

export const useLayoutStore = create<LayoutState>()(layoutStateCreator(
  browserStorage(),
  () => (typeof window === 'undefined' ? 1440 : window.innerWidth),
))
