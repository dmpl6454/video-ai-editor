// The transition catalog on the desktop side: one normalised shape for the
// Transitions panel, the timeline popover and (through the prompt) the
// recipes, fed by the read-only `list_transitions` dispatch tool.
//
// The backend is the only authority on what a name renders as
// (render/transitions.py — 58 native xfades, custom exprs, post-filters,
// and 33 aliases folded onto 72 distinct looks). Since 0.7.0 its `catalog`
// carries `entries`: one product record per look with a CapCut-style
// display name, a family (the panel's tabs), a default duration and a
// description. This module prefers those verbatim and DERIVES the same
// fields from the older `categories/aliases/descriptions` payload when it
// meets a 0.6 backend (a stale packaged app, a paired dev server) — so the
// panel is never empty and never invents a name the backend would reject.
//
// The one thing the backend does not know is how to DRAW a look on a hover
// tile without a network round-trip: `previewFor(name)` maps every look to a
// tiny two-colour animation family by name pattern (all `slide*` push, all
// `wipe*` wipe, `circle*` iris…). It is a pure function so a test can pin
// that every entry of the recorded catalog gets a preview and none falls
// through to the generic crossfade by accident.

import { api } from '../api'

export type Family = 'Basic' | 'Wipe' | 'Slide' | 'Zoom' | 'Blur' | 'Shape' | 'Glitch/Stylised' | 'Light'

/** Tab order — the same order `render/transitions.py::FAMILY_NAMES` reports. */
export const FAMILY_ORDER: Family[] = ['Basic', 'Wipe', 'Slide', 'Zoom', 'Blur', 'Shape', 'Glitch/Stylised', 'Light']

export type Direction = 'left' | 'right' | 'up' | 'down' | 'tl' | 'tr' | 'bl' | 'br'

export type PreviewKind =
  | 'fade' | 'dip-black' | 'dip-white' | 'gray' | 'distance'
  | 'wipe' | 'diag' | 'slide' | 'cover' | 'reveal' | 'slice' | 'wind' | 'clock' | 'wave'
  | 'zoom' | 'squeeze' | 'crop'
  | 'blur' | 'pixel'
  | 'iris' | 'iris-close' | 'box' | 'diamond' | 'doors' | 'doors-close' | 'curtain' | 'curtain-close'
  | 'blinds' | 'bars' | 'checker' | 'ripple'
  | 'glitch' | 'spin' | 'whip' | 'burn'

export interface TransitionPreview { kind: PreviewKind; dir: Direction | null }

export interface TransitionEntry {
  /** Canonical backend name — what `add_transition.type` receives. */
  name: string
  /** CapCut-style label ("Fade to Black", "Whip Pan Up"). */
  display: string
  family: Family
  /** The renderer's own grouping ("fades", "wipes", …) when known. */
  category: string | null
  /** Seconds the transition runs when applied from the panel. */
  duration: number
  description: string
  /** Synonyms the backend also accepts for this look ("crossfade" for "fade"). */
  aliases: string[]
  /** Which mechanism draws it — informational; the preview keys off `preview`. */
  kind: 'native' | 'custom' | 'post'
  preview: TransitionPreview
}

export interface TransitionCatalog {
  /** Distinct looks in family order, then display order. */
  entries: TransitionEntry[]
  /** Every accepted name (looks + aliases) → its entry. */
  byName: Map<string, TransitionEntry>
  /** Entries grouped by family; every family present, possibly empty. */
  families: Map<Family, TransitionEntry[]>
  /** Every name `add_transition` accepts, sorted — the popover's full list. */
  names: string[]
  count: number
  looks: number
  aliasCount: number
  /** `entries` when the backend supplied product records; `derived` when this module built them. */
  source: 'entries' | 'derived' | 'fallback'
}

// Bounds `add_transition` enforces (render/transitions.py MIN_DURATION_S and
// the popover's cap); the panel clamps before it dispatches.
export const MIN_DURATION_S = 0.1
export const MAX_DURATION_S = 2.0
export const FALLBACK_DURATION_S = 0.5

export const clampDuration = (d: number): number =>
  Math.max(MIN_DURATION_S, Math.min(MAX_DURATION_S, Number.isFinite(d) ? d : FALLBACK_DURATION_S))

/**
 * The one sentence the panel's "Every cut" sends to the Prompt bar. The
 * CANONICAL backend name plus an explicit duration, never the display label:
 * the planner's slot extractor matches canonical names exactly and reads
 * "lasting N seconds" as the length, so the look and the duration a user
 * chose on the tile are the ones that get applied. A label round-tripped
 * through the grammar once sent "Barn Doors Open" to a crossdissolve.
 */
export function everyCutPrompt(e: { name: string }, durationS: number): string {
  return `add a ${e.name} transition between every clip lasting ${clampDuration(durationS)} seconds`
}

// ---------------------------------------------------------------------------
// Derivation tables — only used when the backend sends no `entries`.
// Mirrors render/transitions.py::FAMILIES / display_name / default_duration
// so a 0.6 backend groups the same way a 0.7 one does.
// ---------------------------------------------------------------------------

const FAMILY_BY_CATEGORY: Record<string, Family> = {
  fades: 'Basic', wipes: 'Wipe', slides: 'Slide', covers: 'Slide', shapes: 'Shape', slices: 'Wipe',
  zoom: 'Zoom', texture: 'Blur', shaped: 'Shape', stylized: 'Glitch/Stylised',
}
// Names whose family differs from their renderer category.
const FAMILY_OVERRIDE: Record<string, Family> = {
  fadeblack: 'Light', fadewhite: 'Light', fadegrays: 'Light', burn: 'Light',
  circlecrop: 'Zoom', rectcrop: 'Zoom',
  hlwind: 'Blur', hrwind: 'Blur', vuwind: 'Blur', vdwind: 'Blur',
  wave: 'Wipe', radial: 'Wipe', pixelize: 'Glitch/Stylised',
}

const DISPLAY_SPECIAL: Record<string, string> = {
  fadeblack: 'Fade to Black', fadewhite: 'Fade to White', fadegrays: 'Fade Through Gray',
  fadefast: 'Fast Fade', fadeslow: 'Slow Fade', hblur: 'Blur Dissolve',
  hlslice: 'Slice Left', hrslice: 'Slice Right', vuslice: 'Slice Up', vdslice: 'Slice Down',
  hlwind: 'Wind Left', hrwind: 'Wind Right', vuwind: 'Wind Up', vdwind: 'Wind Down',
  squeezeh: 'Squeeze Horizontal', squeezev: 'Squeeze Vertical', circlecrop: 'Circle Zoom',
  rectcrop: 'Box Zoom', vertopen: 'Barn Doors Open', vertclose: 'Barn Doors Close',
  horzopen: 'Curtain Open', horzclose: 'Curtain Close', radial: 'Clock Wipe',
  wipetl: 'Wipe Top-Left', wipetr: 'Wipe Top-Right', wipebl: 'Wipe Bottom-Left', wipebr: 'Wipe Bottom-Right',
  diagtl: 'Diagonal Top-Left', diagtr: 'Diagonal Top-Right', diagbl: 'Diagonal Bottom-Left', diagbr: 'Diagonal Bottom-Right',
  whip: 'Whip Pan Left', whipright: 'Whip Pan Right', whipup: 'Whip Pan Up', whipdown: 'Whip Pan Down',
  zoomin: 'Zoom In', boxopen: 'Box Open', circleopen: 'Iris Open', circleclose: 'Iris Close',
  burn: 'Film Burn', pixelize: 'Pixelate', checker: 'Checkerboard', blinds: 'Blinds', bars: 'Bars',
}

const DURATION_BY_FAMILY: Record<Family, number> = {
  Basic: 0.5, Wipe: 0.4, Slide: 0.35, Zoom: 0.3, Blur: 0.5, Shape: 0.5, 'Glitch/Stylised': 0.3, Light: 0.6,
}
const DURATION_SPECIAL: Record<string, number> = {
  fadefast: 0.3, fadeslow: 0.8, burn: 0.7, whip: 0.25, whipright: 0.25, whipup: 0.25, whipdown: 0.25,
  spiral: 0.6, pixelize: 0.5, hblur: 0.5, wave: 0.4, radial: 0.4,
}

const DIRECTIONAL = ['slide', 'smooth', 'cover', 'reveal', 'wipe'] as const
const DIRS: Direction[] = ['left', 'right', 'up', 'down']

/** "slideleft" → "Slide Left"; the same rule as the backend's `display_name`. */
export function displayNameFor(name: string): string {
  const n = name.trim().toLowerCase()
  if (DISPLAY_SPECIAL[n]) return DISPLAY_SPECIAL[n]
  for (const prefix of DIRECTIONAL) {
    for (const d of DIRS) {
      if (n === `${prefix}${d}`) return `${cap(prefix)} ${cap(d)}`
    }
  }
  return cap(n)
}
const cap = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s)

function familyFor(name: string, category: string | null): Family {
  if (FAMILY_OVERRIDE[name]) return FAMILY_OVERRIDE[name]
  return (category && FAMILY_BY_CATEGORY[category]) || 'Glitch/Stylised'
}

function durationFor(name: string, family: Family): number {
  return DURATION_SPECIAL[name] ?? DURATION_BY_FAMILY[family]
}

// ---------------------------------------------------------------------------
// Preview families — a pure name→animation mapping for the hover tiles.
// ---------------------------------------------------------------------------

const DIR_SUFFIX: [string, Direction][] = [
  ['left', 'left'], ['right', 'right'], ['up', 'up'], ['down', 'down'],
  ['tl', 'tl'], ['tr', 'tr'], ['bl', 'bl'], ['br', 'br'],
]
function dirOf(name: string, stem: string): Direction | null {
  const rest = name.slice(stem.length)
  for (const [suffix, dir] of DIR_SUFFIX) if (rest === suffix) return dir
  return null
}

const PREVIEW_EXACT: Record<string, TransitionPreview> = {
  fade: { kind: 'fade', dir: null }, fadefast: { kind: 'fade', dir: null }, fadeslow: { kind: 'fade', dir: null },
  dissolve: { kind: 'fade', dir: null }, distance: { kind: 'distance', dir: null },
  fadeblack: { kind: 'dip-black', dir: null }, fadewhite: { kind: 'dip-white', dir: null },
  fadegrays: { kind: 'gray', dir: null }, burn: { kind: 'burn', dir: null },
  radial: { kind: 'clock', dir: null }, wave: { kind: 'wave', dir: null },
  zoomin: { kind: 'zoom', dir: null }, squeezeh: { kind: 'squeeze', dir: 'left' }, squeezev: { kind: 'squeeze', dir: 'up' },
  circlecrop: { kind: 'crop', dir: null }, rectcrop: { kind: 'crop', dir: 'tl' },
  hblur: { kind: 'blur', dir: null }, pixelize: { kind: 'pixel', dir: null },
  circleopen: { kind: 'iris', dir: null }, circleclose: { kind: 'iris-close', dir: null },
  boxopen: { kind: 'box', dir: null }, diamond: { kind: 'diamond', dir: null },
  vertopen: { kind: 'doors', dir: null }, vertclose: { kind: 'doors-close', dir: null },
  horzopen: { kind: 'curtain', dir: null }, horzclose: { kind: 'curtain-close', dir: null },
  blinds: { kind: 'blinds', dir: null }, bars: { kind: 'bars', dir: null },
  checker: { kind: 'checker', dir: null }, ripple: { kind: 'ripple', dir: null },
  glitch: { kind: 'glitch', dir: null }, spiral: { kind: 'spin', dir: null },
  hlslice: { kind: 'slice', dir: 'left' }, hrslice: { kind: 'slice', dir: 'right' },
  vuslice: { kind: 'slice', dir: 'up' }, vdslice: { kind: 'slice', dir: 'down' },
  hlwind: { kind: 'wind', dir: 'left' }, hrwind: { kind: 'wind', dir: 'right' },
  vuwind: { kind: 'wind', dir: 'up' }, vdwind: { kind: 'wind', dir: 'down' },
  whip: { kind: 'whip', dir: 'left' },
}
const PREVIEW_STEMS: [string, PreviewKind][] = [
  ['whip', 'whip'], ['smooth', 'slide'], ['slide', 'slide'], ['cover', 'cover'], ['reveal', 'reveal'],
  ['diag', 'diag'], ['wipe', 'wipe'],
]

/** The two-colour animation a look gets on its tile. Total: never null. */
export function previewFor(name: string): TransitionPreview {
  const n = name.trim().toLowerCase()
  if (PREVIEW_EXACT[n]) return PREVIEW_EXACT[n]
  for (const [stem, kind] of PREVIEW_STEMS) {
    if (n.startsWith(stem)) {
      const dir = dirOf(n, stem)
      if (dir) return { kind, dir }
    }
  }
  return { kind: 'fade', dir: null }
}

// ---------------------------------------------------------------------------
// Normalisation
// ---------------------------------------------------------------------------

type Raw = Record<string, unknown>
const asObj = (v: unknown): Raw => (v && typeof v === 'object' && !Array.isArray(v) ? (v as Raw) : {})
const asStrList = (v: unknown): string[] =>
  Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string' && x.trim() !== '').map((x) => x.trim().toLowerCase()) : []
const isFamily = (v: unknown): v is Family => typeof v === 'string' && (FAMILY_ORDER as string[]).includes(v)

function fromEntries(raw: unknown[], names: Set<string>): TransitionEntry[] {
  const out: TransitionEntry[] = []
  for (const r of raw) {
    const e = asObj(r)
    const name = typeof e.name === 'string' ? e.name.trim().toLowerCase() : ''
    if (!name) continue
    names.add(name)
    const aliases = asStrList(e.aliases)
    for (const a of aliases) names.add(a)
    const category = typeof e.category === 'string' ? e.category : null
    const family = isFamily(e.family) ? e.family : familyFor(name, category)
    const dur = typeof e.default_duration === 'number' ? e.default_duration : durationFor(name, family)
    const kind = e.kind === 'custom' || e.kind === 'post' ? e.kind : 'native'
    out.push({
      name,
      display: typeof e.display === 'string' && e.display.trim() ? e.display.trim() : displayNameFor(name),
      family, category,
      duration: clampDuration(dur),
      description: typeof e.description === 'string' ? e.description : '',
      aliases, kind, preview: previewFor(name),
    })
  }
  return out
}

function derive(cat: Raw, flat: string[], names: Set<string>): TransitionEntry[] {
  const categories = asObj(cat.categories)
  const aliases = asObj(cat.aliases)
  const descriptions = asObj(cat.descriptions)
  const defaults = asObj(cat.defaults)
  const categoryOf = new Map<string, string>()
  for (const [c, list] of Object.entries(categories)) for (const n of asStrList(list)) categoryOf.set(n, c)
  const aliasTargets = new Map<string, string>()
  for (const [a, t] of Object.entries(aliases)) if (typeof t === 'string') aliasTargets.set(a.toLowerCase(), t.toLowerCase())
  // Looks = every accepted name that is not an alias.
  const looks = new Set<string>()
  for (const n of flat) { names.add(n); if (!aliasTargets.has(n)) looks.add(n) }
  for (const n of categoryOf.keys()) { names.add(n); if (!aliasTargets.has(n)) looks.add(n) }
  for (const t of aliasTargets.values()) { names.add(t); looks.add(t) }
  for (const a of aliasTargets.keys()) names.add(a)
  const aliasOf = new Map<string, string[]>()
  for (const [a, t] of aliasTargets) aliasOf.set(t, [...(aliasOf.get(t) ?? []), a].sort())
  const out: TransitionEntry[] = []
  for (const name of looks) {
    const category = categoryOf.get(name) ?? null
    const family = familyFor(name, category)
    const dur = typeof defaults[name] === 'number' ? (defaults[name] as number) : durationFor(name, family)
    out.push({
      name, display: displayNameFor(name), family, category, duration: clampDuration(dur),
      description: typeof descriptions[name] === 'string' ? (descriptions[name] as string) : '',
      aliases: aliasOf.get(name) ?? [], kind: 'native', preview: previewFor(name),
    })
  }
  return out
}

function build(entries: TransitionEntry[], names: Set<string>, meta: Raw, source: TransitionCatalog['source']): TransitionCatalog {
  const order = new Map(FAMILY_ORDER.map((f, i) => [f, i]))
  const sorted = [...entries].sort((a, b) =>
    (order.get(a.family)! - order.get(b.family)!) || a.display.localeCompare(b.display))
  const byName = new Map<string, TransitionEntry>()
  const families = new Map<Family, TransitionEntry[]>(FAMILY_ORDER.map((f) => [f, []]))
  for (const e of sorted) {
    byName.set(e.name, e)
    for (const a of e.aliases) if (!byName.has(a)) byName.set(a, e)
    families.get(e.family)!.push(e)
  }
  const nameList = [...names].sort()
  const looks = sorted.length
  return {
    entries: sorted, byName, families, names: nameList,
    count: typeof meta.count === 'number' ? meta.count : nameList.length,
    looks: typeof meta.looks === 'number' ? meta.looks : looks,
    aliasCount: typeof meta.alias_count === 'number' ? meta.alias_count : Math.max(0, nameList.length - looks),
    source,
  }
}

/**
 * `list_transitions` result → `TransitionCatalog`. Prefers `catalog.entries`
 * (0.7.0) and derives from `categories/aliases/descriptions` otherwise. An
 * unusable payload (no names at all) yields the fallback catalog rather
 * than an empty panel — the fallback names are ones every backend accepts.
 */
export function normalizeCatalog(raw: unknown): TransitionCatalog {
  const root = asObj(raw)
  const cat = asObj(root.catalog)
  const flat = asStrList(root.transitions)
  const names = new Set<string>(flat)
  const meta: Raw = { count: root.count, looks: root.looks, alias_count: root.alias_count }
  if (Array.isArray(cat.entries) && cat.entries.length) {
    const entries = fromEntries(cat.entries, names)
    if (entries.length) return build(entries, names, meta, 'entries')
  }
  const derived = derive(cat, flat, names)
  if (derived.length) return build(derived, names, meta, 'derived')
  return FALLBACK_CATALOG
}

// Names that have been valid since transitions shipped; shown while the
// listing loads and when it fails, never cached as the real catalog.
const FALLBACK_NAMES = ['fade', 'dissolve', 'fadeblack', 'fadewhite', 'wipeleft', 'wiperight',
                        'slideleft', 'slideright', 'zoomin', 'circleopen', 'pixelize', 'glitch']
export const FALLBACK_CATALOG: TransitionCatalog = (() => {
  const names = new Set(FALLBACK_NAMES)
  const entries = FALLBACK_NAMES.map((name) => {
    const family = familyFor(name, null)
    return {
      name, display: displayNameFor(name), family, category: null, duration: durationFor(name, family),
      description: '', aliases: [], kind: 'native' as const, preview: previewFor(name),
    }
  })
  return build(entries, names, {}, 'fallback')
})()

/** The entry a name (canonical or alias) resolves to, or null when the backend would reject it. */
export function lookupTransition(catalog: TransitionCatalog, name: string): TransitionEntry | null {
  return catalog.byName.get(name.trim().toLowerCase()) ?? null
}

// ---------------------------------------------------------------------------
// Fetch + cache. One catalog per backend process: a successful fetch serves
// every panel and popover open; a failed one is not cached so the next open
// retries. `list_transitions` is read-only — no op, no undo entry.
// ---------------------------------------------------------------------------

type Loader = (sid: string) => Promise<unknown>
const defaultLoader: Loader = (sid) => api.dispatch<unknown>(sid, 'list_transitions', {}).then((r) => r.result)

let _cache: TransitionCatalog | null = null
let _inflight: Promise<TransitionCatalog> | null = null

export function cachedTransitionCatalog(): TransitionCatalog | null { return _cache }

export function loadTransitionCatalog(sid: string, load: Loader = defaultLoader): Promise<TransitionCatalog> {
  if (_cache) return Promise.resolve(_cache)
  if (!_inflight) {
    _inflight = load(sid)
      .then((raw) => {
        const cat = normalizeCatalog(raw)
        // A derived/entries catalog is the backend's word; the fallback is not.
        if (cat.source !== 'fallback') _cache = cat
        return cat
      })
      .finally(() => { _inflight = null })
  }
  return _inflight
}

/** Tests and hot reload. */
export function _resetTransitionCatalogCache(): void { _cache = null; _inflight = null }
