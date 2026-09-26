// The tool panels' AI deep links (LEFT_RAIL_SPEC §2.5 M5, §8.2; R5): every
// row names a real catalogue tool and reads its label from the catalogue, the
// "All ‹group› tools (n)" counts are the catalogue's, a row mirrors its card's
// status from the same sources, no module but AiPanel mounts an AiToolCard,
// and the markup the panels render (react-dom/server; effects do not run)
// carries the rows, their names, the back chip and the jump targets.
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { FeatureReport, ToolSchema } from '../../api'
import type { RunState } from '../../lib/aiRuns'

// store.ts (pulled in by the panels) reads persisted sizes at import time and
// Node's `localStorage` global is a stub without getItem (see ToolRail.test).
vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {}, removeItem: () => {} })
const { AI_CATALOG, GROUP_ORDER } = await import('../../lib/aiCatalog')
const {
  DEEP_LINKS, aiCardToggleSelector, aiGroupId, allToolsLabel, catalogEntry, deepLinkKey, deepLinkStatus,
  groupCount, rememberReturn, scrollDeltaToTop, takeReturn, forgetReturn,
} = await import('./deepLinks')
const { ToolPanel } = await import('./ToolPanel')
const { AiPanel } = await import('../AiPanel')
const { AiToolCard } = await import('../AiToolCard')
const { useLayoutStore } = await import('../../lib/layoutStore')
const { useAiRuns } = await import('../../lib/aiRuns')
const { useKeymapStore } = await import('../../keymap/engine')
const { usePhonePairing } = await import('./phonePairing')
const { useCaptionRun } = await import('../../lib/captionRun')
const { useActivityStore } = await import('../../lib/activityStore')
const { useStore } = await import('../../store')

type Seedable<T> = { getInitialState(): T }
function seed<T extends object>(store: Seedable<T>, patch: Partial<T>) {
  Object.assign(store.getInitialState(), patch)
}

const schema = (name: string): ToolSchema => ({
  name, description: '', cancellable: false, reports_progress: false,
  input_schema: { type: 'object', properties: {}, required: [] },
})
const ALL_TOOLS = AI_CATALOG.map((e) => schema(e.tool))
const report = (unavailable: string[], packaged = false): FeatureReport => ({
  packaged_app: packaged, python: '3.13', anthropic_key_set: true, summary: '', available: [],
  unavailable: unavailable.map((key) => ({ key, feature: `Feature ${key}`, tools: [], fix: `uv sync --extra ${key}` })),
})
const running = (p: Partial<Extract<RunState, { status: 'running' }>> = {}): RunState => ({
  status: 'running', progress: 0, startedAt: 0, cancelling: false, reportsProgress: false, cancellable: false, ...p,
})
const PANELS = Object.keys(DEEP_LINKS) as (keyof typeof DEEP_LINKS)[]

beforeEach(() => {
  seed(useLayoutStore, { leftTab: 'media', leftOpen: true, rightOpen: true, rightTab: 'inspect', aiJump: null })
  seed(useKeymapStore, { overrides: {} })
  seed(useAiRuns, { runs: {}, tools: null, features: null, downloads: null, loadError: null, loading: false })
  seed(usePhonePairing, { enabled: false })
  seed(useActivityStore, { recording: null, captions: null, liveMessage: '' })
  seed(useCaptionRun, { busy: false, progress: 0, elapsed: 0, cancelling: false, jobId: null, consent: null, downloads: null })
  forgetReturn()
})

describe('the deep-link catalogue (§8.2 deepLinks.test.ts)', () => {
  it('names only tools that exist in lib/aiCatalog.ts, each once', () => {
    const all = PANELS.flatMap((p) => DEEP_LINKS[p].tools)
    for (const tool of all) expect(catalogEntry(tool), tool).toBeDefined()
    expect(new Set(all).size).toBe(all.length)
  })

  it('links each panel to tools of its own AI group, and its All link to that group', () => {
    const groupOf = (tool: string) => catalogEntry(tool)!.group
    expect(DEEP_LINKS.media.tools.map(groupOf)).toEqual(['Find & search', 'Find & search'])
    expect(DEEP_LINKS.audio.tools.every((t) => groupOf(t) === 'Audio')).toBe(true)
    for (const p of PANELS) {
      const g = DEEP_LINKS[p].group
      if (g) for (const t of DEEP_LINKS[p].tools) expect(groupOf(t), `${p}/${t}`).toBe(g)
    }
  })

  it('lists the rows the spec names, in its order', () => {
    expect(DEEP_LINKS.media.tools).toEqual(['search_media', 'find_broll'])
    expect(DEEP_LINKS.audio.tools).toEqual(['noise_reduce', 'vocal_isolate', 'instrumental_isolate', 'tts_voiceover'])
    expect(DEEP_LINKS.text.tools).toEqual(['add_hook_overlay', 'generate_hook', 'add_lower_third', 'apply_brand_kit'])
    expect(DEEP_LINKS.effects.tools).toEqual(['remove_background', 'chroma_key'])
    expect(DEEP_LINKS.captions.tools).toEqual(['translate_captions', 'diarize', 'name_speakers', 'import_srt', 'export_srt'])
  })

  it('counts the groups from the catalogue: 5, 7, 4 and 10, of 42 tools', () => {
    expect(groupCount('Find & search')).toBe(5)
    expect(groupCount('Text & brand')).toBe(7)
    expect(groupCount('Cutout & effects')).toBe(4)
    expect(groupCount('Captions & speech')).toBe(10)
    expect(AI_CATALOG.length).toBe(42)
    expect(GROUP_ORDER.reduce((n, g) => n + groupCount(g), 0)).toBe(42)
    expect(allToolsLabel('Captions & speech')).toBe('All Captions & speech tools (10)')
  })

  it('never lets a module other than AiPanel mount an AiToolCard (spec risk 12)', () => {
    const root = new URL('../../', import.meta.url).pathname
    const importers: string[] = []
    const walk = (dir: string) => {
      for (const name of readdirSync(dir)) {
        const p = join(dir, name)
        if (statSync(p).isDirectory()) { walk(p); continue }
        if (!/\.tsx?$/.test(name) || /\.test\.tsx?$/.test(name)) continue
        if (/from\s+['"][./]*(?:components\/)?AiToolCard['"]/.test(readFileSync(p, 'utf8'))) importers.push(p.slice(root.length))
      }
    }
    walk(root)
    expect(importers).toEqual(['components/AiPanel.tsx'])
  })
})

describe('deepLinkStatus: the card status a row mirrors', () => {
  const e = (tool: string) => catalogEntry(tool)!
  const base = { run: undefined, features: report([]), downloads: null, tools: ALL_TOOLS }

  it('says nothing for a ready tool, or while the feature report is still loading', () => {
    expect(deepLinkStatus(e('diarize'), base)).toBeNull()
    expect(deepLinkStatus(e('vocal_isolate'), { ...base, features: null, tools: null })).toBeNull()
  })
  it('shows a live run, its progress, and "Stopping…" while it cancels', () => {
    expect(deepLinkStatus(e('diarize'), { ...base, run: running() })).toEqual({ text: 'Running…', tone: 'run' })
    expect(deepLinkStatus(e('diarize'), { ...base, run: running({ reportsProgress: true, progress: 0.42 }) }))
      .toEqual({ text: 'Running 42%', tone: 'run' })
    expect(deepLinkStatus(e('diarize'), { ...base, run: running({ cancelling: true }) })?.text).toBe('Stopping…')
  })
  it('shows a failed, cancelled or finished run', () => {
    expect(deepLinkStatus(e('diarize'), { ...base, run: { status: 'error', message: 'x', cancelled: false } }))
      .toEqual({ text: 'Failed', tone: 'error' })
    expect(deepLinkStatus(e('diarize'), { ...base, run: { status: 'error', message: 'x', cancelled: true } })?.text)
      .toBe('Cancelled')
    expect(deepLinkStatus(e('diarize'), { ...base, run: { status: 'done', result: {}, at: 0 } }))
      .toEqual({ text: 'Done', tone: 'done' })
  })
  it("uses the card's own badge for a missing feature (featureCopy)", () => {
    expect(deepLinkStatus(e('vocal_isolate'), { ...base, features: report(['stems']) }))
      .toEqual({ text: 'Not installed', tone: 'warn' })
    expect(deepLinkStatus(e('vocal_isolate'), { ...base, features: report(['stems'], true) })?.text).toBe('Not set up')
    // chroma_key has no gate: a missing stems feature says nothing about it.
    expect(deepLinkStatus(e('chroma_key'), { ...base, features: report(['stems']) })).toBeNull()
  })
  it("uses the card's own download badge for a first-run download", () => {
    const downloads = { stems: { what: 'the voice-separation model', bytes: 84_000_000, cached: false } }
    expect(deepLinkStatus(e('instrumental_isolate'), { ...base, downloads }))
      .toEqual({ text: 'Downloads 84 MB first', tone: 'warn' })
    expect(deepLinkStatus(e('instrumental_isolate'), { ...base, downloads: { stems: { ...downloads.stems, cached: true } } }))
      .toBeNull()
  })
  it('says "Not available" for a tool this backend does not advertise', () => {
    expect(deepLinkStatus(e('find_broll'), { ...base, tools: ALL_TOOLS.filter((t) => t.name !== 'find_broll') }))
      .toEqual({ text: 'Not available', tone: 'dim' })
  })
})

describe('the jump helpers', () => {
  it('scrolls a card to 6 px under the sticky head', () => {
    // The head ends at 154: the card must end at 160; one at 700 scrolls down 540.
    expect(scrollDeltaToTop(700, 154)).toBe(540)
    expect(scrollDeltaToTop(160, 154)).toBe(0)
    expect(scrollDeltaToTop(120, 154)).toBe(-40)
    expect(scrollDeltaToTop(300, 154, 0)).toBe(146)
  })
  it('hands the return point back once, and only to the panel it came from', () => {
    rememberReturn({ from: 'captions', key: deepLinkKey({ tool: 'diarize' }), scrollTop: 240 })
    expect(takeReturn('audio')).toBeNull()                 // a jump from elsewhere
    rememberReturn({ from: 'captions', key: 'tool:diarize', scrollTop: 240 })
    expect(takeReturn('captions')).toEqual({ from: 'captions', key: 'tool:diarize', scrollTop: 240 })
    expect(takeReturn('captions')).toBeNull()
  })
  it('keys rows by tool and group links by group', () => {
    expect(deepLinkKey({ tool: 'diarize' })).toBe('tool:diarize')
    expect(deepLinkKey({ group: 'Captions & speech' })).toBe('group:Captions & speech')
  })
  it("targets AiToolCard's own toggle (the selector matches its markup)", () => {
    const m = renderToStaticMarkup(createElement(AiToolCard, {
      entry: catalogEntry('diarize')!, schema: schema('diarize'), onRun: async () => {},
    }))
    const sel = aiCardToggleSelector('diarize')    // button.ai-card-head[aria-controls="ai-body-diarize"]
    expect(sel).toBe('button.ai-card-head[aria-controls="ai-body-diarize"]')
    expect(m).toMatch(/<button type="button" class="ai-card-head" aria-expanded="false" aria-controls="ai-body-diarize">/)
  })
})

// ---- the panels' markup ------------------------------------------------------

const panelHtml = (tab: 'media' | 'audio' | 'text' | 'effects' | 'captions') => {
  seed(useLayoutStore, { leftTab: tab, leftOpen: true })
  const m = renderToStaticMarkup(createElement(ToolPanel))
  // The tabpanel of `tab` only.
  const start = m.indexOf(`id="tool-panel-${tab}"`)
  const next = m.indexOf('role="tabpanel"', start + 1)
  return m.slice(start, next < 0 ? undefined : next)
}
/** Every deep-link row's opening tag and its visible label. */
function rows(markup: string): { key: string; label: string; describedby?: string; status?: string }[] {
  return [...markup.matchAll(/<button type="button" class="deep-link"([^>]*)>([\s\S]*?)<\/button>/g)].map((m) => {
    const attrs = Object.fromEntries([...m[1].matchAll(/([\w-]+)="([^"]*)"/g)].map((a) => [a[1], a[2]]))
    const label = m[2].match(/class="deep-link-label">([^<]*)</)?.[1] ?? ''
    const status = m[2].match(/class="deep-link-status[^"]*">([^<]*)</)?.[1]
    return { key: attrs['data-deep-link'], label, describedby: attrs['aria-describedby'], status }
  })
}
const decode = (s: string) => s.replace(/&amp;/g, '&')

describe('each panel renders its deep links (§2.5)', () => {
  it.each(PANELS)('%s: a heading, one row per tool with the catalogue label, and the All link', (p) => {
    const m = panelHtml(p)
    const spec = DEEP_LINKS[p]
    expect(decode(m)).toContain(`>${spec.heading}</h3>`)
    expect(rows(m).map((r) => r.key)).toEqual(spec.tools.map((t) => `tool:${t}`))
    expect(rows(m).map((r) => decode(r.label))).toEqual(spec.tools.map((t) => catalogEntry(t)!.label))
    if (spec.group) {
      expect(decode(m)).toContain(`data-deep-link="group:${spec.group}">${allToolsLabel(spec.group)}<svg`)
    } else {
      expect(m).not.toContain('deep-group-link')
    }
  })

  it('names a row by its label only; its status is the description', () => {
    seed(useAiRuns, { tools: ALL_TOOLS, features: report(['stems']) })
    const m = panelHtml('audio')
    const vocal = rows(m).find((r) => r.key === 'tool:vocal_isolate')!
    expect(vocal.status).toBe('Not installed')
    expect(vocal.describedby).toMatch(/-s$/)
    expect(m).toMatch(/aria-labelledby="[^"]+-l"[^>]*>/)
    const noise = rows(m).find((r) => r.key === 'tool:noise_reduce')!
    expect(noise.status).toBeUndefined()
    expect(noise.describedby).toBeUndefined()
  })

  it("mirrors a running card's status on its row", () => {
    seed(useAiRuns, { runs: { diarize: running() } })
    expect(rows(panelHtml('captions')).find((r) => r.key === 'tool:diarize')!.status).toBe('Running…')
  })

  it('puts Find & search last in the Media panel and Cutout rows under EffectsPanel', () => {
    seed(useStore, { edl: null, sessionId: null } as never)
    const media = panelHtml('media')
    expect(media.indexOf('Find &amp; search')).toBeGreaterThan(media.indexOf('class="dropzone'))
    const fx = panelHtml('effects')
    expect(fx.indexOf('Cutout &amp; effects (AI)')).toBeGreaterThan(0)
  })
})

describe('the AI panel side of a jump', () => {
  it('shows a back chip named "Back to ‹origin›" only while a jump from another panel stands', () => {
    seed(useAiRuns, { tools: ALL_TOOLS })
    expect(renderToStaticMarkup(createElement(AiPanel))).not.toContain('ai-back-chip')
    seed(useLayoutStore, { leftTab: 'ai', aiJump: { tool: 'diarize', from: 'captions', nonce: 1 } })
    const m = renderToStaticMarkup(createElement(AiPanel))
    expect(m).toMatch(/<button type="button" class="ai-back-chip">.*<span class="deep-sr-only">Back to <\/span>Captions<\/button>/)
    // It sits in the sticky head, above the search box.
    expect(m.indexOf('ai-back-chip')).toBeLessThan(m.indexOf('class="ai-search"'))
    seed(useLayoutStore, { aiJump: { tool: 'diarize', from: 'ai', nonce: 2 } })
    expect(renderToStaticMarkup(createElement(AiPanel))).not.toContain('ai-back-chip')
  })

  it('gives every group heading the id a group link targets, focusable by script only', () => {
    seed(useAiRuns, { tools: ALL_TOOLS })
    const m = decode(renderToStaticMarkup(createElement(AiPanel)))
    for (const g of GROUP_ORDER) expect(m).toContain(`<h3 id="${aiGroupId(g)}" class="section-label" tabindex="-1">${g}</h3>`)
  })
})
