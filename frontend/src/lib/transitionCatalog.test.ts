// The catalog normaliser, driven by the RECORDED `list_transitions` result
// (__fixtures__/list_transitions.json — the dispatch handler's own return
// value on this backend): every one of the 72 looks lands in exactly one of
// the eight CapCut-style families with a display name, a default duration
// inside add_transition's bounds and a hover preview; every alias resolves;
// and the derivation path for an older backend groups the same names the
// same way.
//
// The fixture is a verbatim recording, not hand-edited: regenerate it with
//   .venv/bin/python -c "import json,tempfile,pathlib; \
//     from video_ai_editor.edl.snapshot import EDLStore; \
//     from video_ai_editor.agent.dispatch import dispatch; \
//     s=EDLStore(pathlib.Path(tempfile.mkdtemp())/'s'); \
//     print(json.dumps(dispatch(s,'list_transitions',{}),indent=1,sort_keys=True))"
// whenever render/transitions.py changes. It once drifted silently (the
// descriptions were rewritten upstream while names/families/defaults stayed
// put, so every assertion here still passed against stale copy); the backend
// now pins it in tests/test_transition_defaults.py
// (test_frontend_fixture_matches_the_live_list_transitions_output), which
// fails the moment the recording and the handler disagree on any field.
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it, vi } from 'vitest'

vi.mock('../api', () => ({ api: { dispatch: vi.fn() } }))

const {
  FALLBACK_CATALOG, FAMILY_ORDER, MAX_DURATION_S, MIN_DURATION_S, _resetTransitionCatalogCache, clampDuration,
  displayNameFor, everyCutPrompt, loadTransitionCatalog, lookupTransition, normalizeCatalog, previewFor,
} = await import('./transitionCatalog')

const RAW = JSON.parse(readFileSync(fileURLToPath(new URL('./__fixtures__/list_transitions.json', import.meta.url)), 'utf-8')) as {
  transitions: string[]; count: number; looks: number; alias_count: number
  catalog: { entries: unknown[]; categories: Record<string, string[]>; aliases: Record<string, string>
             descriptions: Record<string, string>; defaults: Record<string, number>; families: Record<string, string[]> }
}

describe('the recorded catalog', () => {
  const cat = normalizeCatalog(RAW)

  it('takes the backend product records verbatim', () => {
    expect(cat.source).toBe('entries')
    expect(cat.entries).toHaveLength(RAW.looks)
    expect(cat.looks).toBe(72)
    expect(cat.count).toBe(105)
    expect(cat.aliasCount).toBe(33)
    expect(cat.names).toEqual([...RAW.transitions].sort())
  })

  it('puts every look in one of the eight families, in tab order', () => {
    const seen = new Set<string>()
    for (const e of cat.entries) {
      expect(FAMILY_ORDER).toContain(e.family)
      expect(seen.has(e.name)).toBe(false)
      seen.add(e.name)
    }
    for (const f of FAMILY_ORDER) {
      expect(cat.families.get(f)!.map((e) => e.name).sort()).toEqual([...RAW.catalog.families[f]].sort())
    }
    const order = cat.entries.map((e) => FAMILY_ORDER.indexOf(e.family))
    expect(order).toEqual([...order].sort((a, b) => a - b))
  })

  it('gives every look a CapCut-style name, a bounded default duration and a description slot', () => {
    for (const e of cat.entries) {
      expect(e.display).toMatch(/^[A-Z]/)
      expect(e.display).not.toBe(e.name)          // never the raw ffmpeg token
      expect(e.duration).toBeGreaterThanOrEqual(MIN_DURATION_S)
      expect(e.duration).toBeLessThanOrEqual(MAX_DURATION_S)
      expect(typeof e.description).toBe('string')
    }
    expect(lookupTransition(cat, 'fadeblack')?.display).toBe('Fade to Black')
    expect(lookupTransition(cat, 'whipup')?.display).toBe('Whip Pan Up')
    expect(lookupTransition(cat, 'whip')?.duration).toBe(0.25)
    expect(lookupTransition(cat, 'fadeslow')?.duration).toBe(0.8)
  })

  it('resolves every alias to its look and rejects an unknown name', () => {
    for (const [alias, target] of Object.entries(RAW.catalog.aliases)) {
      expect(lookupTransition(cat, alias)?.name).toBe(target)
    }
    expect(lookupTransition(cat, 'spin')?.name).toBe('spiral')
    expect(lookupTransition(cat, ' CROSSFADE ')?.name).toBe('fade')
    expect(lookupTransition(cat, 'starwipe')).toBeNull()
  })

  it('gives every look a hover preview that is not the generic crossfade unless it IS one', () => {
    const generic = new Set(['fade', 'fadefast', 'fadeslow', 'dissolve'])
    for (const e of cat.entries) {
      if (generic.has(e.name)) expect(e.preview.kind).toBe('fade')
      else expect(e.preview.kind).not.toBe('fade')
    }
    expect(previewFor('slideleft')).toEqual({ kind: 'slide', dir: 'left' })
    expect(previewFor('wipebr')).toEqual({ kind: 'wipe', dir: 'br' })
    expect(previewFor('smoothup')).toEqual({ kind: 'slide', dir: 'up' })
    expect(previewFor('whipdown')).toEqual({ kind: 'whip', dir: 'down' })
    expect(previewFor('circleclose')).toEqual({ kind: 'iris-close', dir: null })
    expect(previewFor('nonsense')).toEqual({ kind: 'fade', dir: null })
  })
})

describe('an older backend without product records', () => {
  const legacy = {
    transitions: RAW.transitions, count: RAW.count, looks: RAW.looks, alias_count: RAW.alias_count,
    catalog: { categories: RAW.catalog.categories, aliases: RAW.catalog.aliases, descriptions: RAW.catalog.descriptions },
  }
  const cat = normalizeCatalog(legacy)

  it('derives the same 72 looks, the same families and the same display names', () => {
    expect(cat.source).toBe('derived')
    const fresh = normalizeCatalog(RAW)
    expect(cat.entries.map((e) => e.name)).toEqual(fresh.entries.map((e) => e.name))
    for (const e of fresh.entries) {
      const d = lookupTransition(cat, e.name)!
      expect(d.family).toBe(e.family)
      expect(d.display).toBe(e.display)
      expect(d.aliases).toEqual(e.aliases)
    }
  })

  it('falls back to a never-empty built-in list when the payload is unusable', () => {
    expect(normalizeCatalog(null)).toBe(FALLBACK_CATALOG)
    expect(normalizeCatalog({ transitions: [] })).toBe(FALLBACK_CATALOG)
    expect(FALLBACK_CATALOG.entries.length).toBeGreaterThan(8)
    expect(FALLBACK_CATALOG.source).toBe('fallback')
    for (const e of FALLBACK_CATALOG.entries) expect(RAW.transitions).toContain(e.name)
  })

  it('display names follow the backend rule for directional looks', () => {
    expect(displayNameFor('coverdown')).toBe('Cover Down')
    expect(displayNameFor('revealright')).toBe('Reveal Right')
    expect(displayNameFor('radial')).toBe('Clock Wipe')
    expect(displayNameFor('newthing')).toBe('Newthing')
  })

  it('clamps durations to what add_transition accepts', () => {
    expect(clampDuration(0)).toBe(MIN_DURATION_S)
    expect(clampDuration(9)).toBe(MAX_DURATION_S)
    expect(clampDuration(Number.NaN)).toBe(0.5)
    expect(clampDuration(0.35)).toBe(0.35)
  })
})

describe('loadTransitionCatalog', () => {
  it('caches a real catalog per backend, retries after a failure, never caches the fallback', async () => {
    _resetTransitionCatalogCache()
    const load = vi.fn<(sid: string) => Promise<unknown>>()
    load.mockRejectedValueOnce(new Error('offline'))
    await expect(loadTransitionCatalog('s', load)).rejects.toThrow('offline')
    load.mockResolvedValueOnce({})
    expect((await loadTransitionCatalog('s', load)).source).toBe('fallback')
    load.mockResolvedValueOnce(RAW)
    const a = await loadTransitionCatalog('s', load)
    const b = await loadTransitionCatalog('s', load)
    expect(a.source).toBe('entries')
    expect(b).toBe(a)
    expect(load).toHaveBeenCalledTimes(3)
    _resetTransitionCatalogCache()
  })

  it('coalesces concurrent loads into one request', async () => {
    _resetTransitionCatalogCache()
    const load = vi.fn(async () => RAW)
    const [a, b] = await Promise.all([loadTransitionCatalog('s', load), loadTransitionCatalog('s', load)])
    expect(a).toBe(b)
    expect(load).toHaveBeenCalledTimes(1)
    _resetTransitionCatalogCache()
  })
})

describe('the "Every cut" sentence', () => {
  it('carries the canonical name and the chosen duration, never the display label', () => {
    const cat = normalizeCatalog(RAW)
    for (const e of cat.entries) {
      const sentence = everyCutPrompt(e, e.duration)
      expect(sentence).toBe(`add a ${e.name} transition between every clip lasting ${e.duration} seconds`)
      expect(sentence).not.toContain(e.display.toLowerCase() === e.name ? '\u0000' : e.display.toLowerCase())
    }
    expect(everyCutPrompt({ name: 'vertopen' }, 9)).toBe('add a vertopen transition between every clip lasting 2 seconds')
    expect(everyCutPrompt({ name: 'whip' }, 0)).toBe('add a whip transition between every clip lasting 0.1 seconds')
  })
})
