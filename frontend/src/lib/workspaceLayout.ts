// Workspace layouts (design handoff §2 "Editor shell", §8 "Layout dropdown"):
// Default / Media / Attributes / Vertical presets for the top row's three
// columns and the two rows, plus "Reset current layout". Persisted apart from
// project content (`aive.layout.*`), as the brief's §7 asks — resetting a
// layout never touches an edit.
//
// Every value is a GRID FRACTION, never a pixel width ("panels are CSS grid
// tracks, never fixed widths"): the editor is fluid between 1440×900 and
// 1920×1080 and a dragged splitter adjusts the fractions it sits between.
import { create } from 'zustand'

export const LAYOUT_IDS = ['Default', 'Media', 'Attributes', 'Vertical'] as const
export type LayoutId = typeof LAYOUT_IDS[number]

export interface LayoutSpec {
  /** Asset browser · Player · Inspector, in fr. */
  cols: [number, number, number]
  /** Top row · Timeline, in fr. */
  rows: [number, number]
}

export const LAYOUT_PRESETS: Record<LayoutId, LayoutSpec> = {
  Default: { cols: [38, 32, 30], rows: [57, 43] },
  Media: { cols: [48, 30, 22], rows: [57, 43] },
  Attributes: { cols: [28, 32, 40], rows: [57, 43] },
  Vertical: { cols: [34, 26, 40], rows: [62, 38] },
}

export const LAYOUT_KEY = 'aive.layout'
const OVERRIDES_KEY = 'aive.layout.overrides'

/** A column never shrinks below this share of the row (keeps every panel's
 *  controls reachable; the brief's "practical minimum widths"). */
export const MIN_COL_FR = 14
export const MIN_ROW_FR = 22

interface KV { getItem(k: string): string | null; setItem(k: string, v: string): void; removeItem?(k: string): void }

function read(kv: KV | null, key: string): string | null {
  try { return kv?.getItem(key) ?? null } catch { return null }
}
function write(kv: KV | null, key: string, value: string | null): void {
  try {
    if (value === null) kv?.removeItem?.(key)
    else kv?.setItem(key, value)
  } catch { /* storage blocked (private window): the choice lasts this load */ }
}

export function asLayoutId(v: unknown): LayoutId | null {
  return LAYOUT_IDS.includes(v as LayoutId) ? (v as LayoutId) : null
}

function readOverrides(kv: KV | null): Partial<Record<LayoutId, LayoutSpec>> {
  const raw = read(kv, OVERRIDES_KEY)
  if (!raw) return {}
  try {
    const o = JSON.parse(raw) as Record<string, unknown>
    const out: Partial<Record<LayoutId, LayoutSpec>> = {}
    for (const id of LAYOUT_IDS) {
      const v = o[id] as LayoutSpec | undefined
      if (v && Array.isArray(v.cols) && v.cols.length === 3 && Array.isArray(v.rows) && v.rows.length === 2
          && [...v.cols, ...v.rows].every((n) => typeof n === 'number' && Number.isFinite(n) && n > 0)) {
        out[id] = { cols: [v.cols[0], v.cols[1], v.cols[2]], rows: [v.rows[0], v.rows[1]] }
      }
    }
    return out
  } catch { return {} }
}

/** Move `delta` fr from column `i` to column `i+1` (negative: the other way),
 *  holding both at MIN_COL_FR. Pure, so the splitter math is testable. */
export function resizeCols(cols: [number, number, number], i: 0 | 1, delta: number): [number, number, number] {
  const out: [number, number, number] = [...cols]
  const a = out[i], b = out[i + 1]
  const d = Math.max(-(a - MIN_COL_FR), Math.min(b - MIN_COL_FR, delta))
  out[i] = a + d
  out[i + 1] = b - d
  return out
}

export function resizeRows(rows: [number, number], delta: number): [number, number] {
  const [a, b] = rows
  const d = Math.max(-(a - MIN_ROW_FR), Math.min(b - MIN_ROW_FR, delta))
  return [a + d, b - d]
}

/** The grid tracks, with the 6 px splitter tracks between the panels. */
export function gridColumns(spec: LayoutSpec): string {
  return spec.cols.map((c) => `minmax(0, ${c}fr)`).join(' 6px ')
}
export function gridRows(spec: LayoutSpec): string {
  return spec.rows.map((r) => `minmax(0, ${r}fr)`).join(' 6px ')
}

interface WorkspaceState {
  layout: LayoutId
  overrides: Partial<Record<LayoutId, LayoutSpec>>
  spec(): LayoutSpec
  setLayout(id: LayoutId): void
  /** A splitter drag: pass the new spec for the CURRENT layout. */
  setSpec(spec: LayoutSpec): void
  resetCurrent(): void
}

export function createWorkspaceStore(kv: KV | null) {
  return create<WorkspaceState>((set, get) => ({
    layout: asLayoutId(read(kv, LAYOUT_KEY)) ?? 'Default',
    overrides: readOverrides(kv),
    spec: () => get().overrides[get().layout] ?? LAYOUT_PRESETS[get().layout],
    setLayout: (id) => { write(kv, LAYOUT_KEY, id); set({ layout: id }) },
    setSpec: (spec) => {
      const overrides = { ...get().overrides, [get().layout]: spec }
      write(kv, OVERRIDES_KEY, JSON.stringify(overrides))
      set({ overrides })
    },
    resetCurrent: () => {
      const overrides = { ...get().overrides }
      delete overrides[get().layout]
      write(kv, OVERRIDES_KEY, JSON.stringify(overrides))
      set({ overrides })
    },
  }))
}

function browserStorage(): KV | null {
  try { return typeof localStorage === 'undefined' ? null : localStorage } catch { return null }
}

export const useWorkspace = createWorkspaceStore(browserStorage())
