// The rail, the tool panel and the right panel rendered to static markup
// (vitest runs in node; react-dom/server is the render path, effects do not
// run). What a screen reader and the Playwright helpers rely on is markup:
// exact tab names, no `title`, roving tabindex, aria-expanded on the selected
// tab only, and every tabpanel mounted with the inactive ones `hidden`.
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { beforeEach, describe, expect, it, vi } from 'vitest'

// store.ts (pulled in by the panels) reads persisted sizes at import time and
// Node's `localStorage` global is a stub without getItem (see TopBar.test).
vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {}, removeItem: () => {} })
const { ToolRail } = await import('./ToolRail')
const { ToolPanel } = await import('./ToolPanel')
const { RightPanel } = await import('../RightPanel')
const { useLayoutStore } = await import('../../lib/layoutStore')
const { useKeymapStore } = await import('../../keymap/engine')
const { useAiRuns } = await import('../../lib/aiRuns')
const { usePromptStore } = await import('../../lib/promptStore')
const { RAIL_ITEMS } = await import('./railModel')

const html = (c: Parameters<typeof createElement>[0]) => renderToStaticMarkup(createElement(c))
/** Every opening tag with role=tab, as attribute maps. */
function tabs(markup: string): Record<string, string>[] {
  return [...markup.matchAll(/<button[^>]*role="tab"[^>]*>/g)].map((m) =>
    Object.fromEntries([...m[0].matchAll(/([\w-]+)="([^"]*)"/g)].map((a) => [a[1], a[2]])))
}

// Server rendering reads a Zustand store's INITIAL state (its server
// snapshot), so a test seeds that object — the pattern OpsLog.test uses.
type Seedable<T> = { getInitialState(): T }
function seed<T extends object>(store: Seedable<T>, patch: Partial<T>) {
  Object.assign(store.getInitialState(), patch)
}

beforeEach(() => {
  seed(useLayoutStore, { leftTab: 'media', leftOpen: true, rightOpen: true, rightTab: 'inspect' })
  seed(useKeymapStore, { overrides: {} })
  seed(useAiRuns, { runs: {} })
  seed(usePromptStore, { status: 'idle' })
})

describe('ToolRail', () => {
  it('is a vertical tablist named "Tool panels" inside nav "Tools"', () => {
    const m = html(ToolRail)
    expect(m).toMatch(/<nav class="rail" aria-label="Tools">/)
    expect(m).toMatch(/role="tablist" aria-orientation="vertical" aria-label="Tool panels"/)
  })

  it('renders one tab per rail item whose text is exactly its label', () => {
    const m = html(ToolRail)
    expect(tabs(m).map((t) => t.id)).toEqual(RAIL_ITEMS.map((r) => `rail-tab-${r.id}`))
    for (const r of RAIL_ITEMS) {
      // The label is the only text in the tab (the icon is aria-hidden).
      const tab = m.slice(m.indexOf(`id="rail-tab-${r.id}"`))
      const inner = tab.slice(tab.indexOf('>') + 1, tab.indexOf('</button>'))
      expect(inner.replace(/<svg[\s\S]*?<\/svg>/g, '').replace(/<[^>]+>/g, '')).toBe(r.label)
    }
  })

  it('puts no title on any tab (the tooltip is data-tip, never part of the name)', () => {
    for (const t of tabs(html(ToolRail))) {
      expect(t.title).toBeUndefined()
      expect(t['data-tip']).toMatch(/ — /)
    }
  })

  it('is one tab stop: only the selected tab is tabbable and carries aria-expanded', () => {
    seed(useLayoutStore, { leftTab: 'effects', leftOpen: false })
    const t = tabs(html(ToolRail))
    const sel = t.filter((x) => x['aria-selected'] === 'true')
    expect(sel.map((x) => x.id)).toEqual(['rail-tab-effects'])
    expect(sel[0].tabindex).toBe('0')
    expect(sel[0]['aria-expanded']).toBe('false')
    for (const x of t.filter((y) => y['aria-selected'] !== 'true')) {
      expect(x.tabindex).toBe('-1')
      expect(x['aria-expanded']).toBeUndefined()
    }
  })

  it('draws every rail icon through the lucide map', () => {
    const m = html(ToolRail)
    for (const r of RAIL_ITEMS) expect(m).toContain(`data-icon="${r.icon}"`)
    expect(m).not.toMatch(/[⌨✨🎵]/u)
  })

  it('names no chord while the keymap binds none (R1): no aria-keyshortcuts, no data-kbd', () => {
    const m = html(ToolRail)
    expect(m).not.toContain('aria-keyshortcuts')
    expect(m).not.toContain('data-kbd')
  })

  it('shows a chord the moment the live keymap binds one (R4, or a user rebind)', () => {
    seed(useKeymapStore, { overrides: { panelAudio: ['Alt+Digit2'] } })
    const audio = tabs(html(ToolRail)).find((t) => t.id === 'rail-tab-audio')!
    expect(audio['aria-keyshortcuts']).toBe('Alt+2')
    expect(audio['data-kbd']).toBeTruthy()
  })

  it('marks AI busy while a tool runs, described (not named) by sr-only text', () => {
    expect(html(ToolRail)).not.toContain('rail-dot')
    seed(useAiRuns, { runs: { remove_silences: { status: 'running', progress: 0, startedAt: 0, cancelling: false, reportsProgress: false, cancellable: false } } })
    const m = html(ToolRail)
    const ai = tabs(m).find((t) => t.id === 'rail-tab-ai')!
    expect(ai['aria-describedby']).toBe('rail-desc-ai')
    expect(m).toMatch(/id="rail-desc-ai"[^>]*>An AI tool is running</)
    expect(m).toContain('rail-dot rail-dot-busy')
  })

  it('keeps the busy description out of the reading order (hidden, still a describedby target)', () => {
    // Idle: the text is in the page (so aria-describedby can point at it the
    // moment a run starts) but `hidden`, so a screen reader walking the Tools
    // nav never reads "An AI tool is running" when nothing is (review RD1).
    expect(html(ToolRail)).toMatch(/<span id="rail-desc-ai"[^>]*\shidden=""[^>]*>An AI tool is running<\/span>/)
    seed(useAiRuns, { runs: { x: { status: 'running', progress: 0, startedAt: 0, cancelling: false, reportsProgress: false, cancellable: false } } })
    expect(html(ToolRail)).toMatch(/<span id="rail-desc-ai"[^>]*\shidden=""/)
  })

  it.each(['planning', 'running', 'verifying'] as const)(
    'lights the AI dot while a Prompt-bar run holds the session lock (%s)', (status) => {
      // §2.3: the dot shows while a tool run holds the session lock — the
      // Prompt bar's run (lib/promptStore), which makes /dispatch answer 409.
      seed(usePromptStore, { status })
      const m = html(ToolRail)
      expect(tabs(m).find((t) => t.id === 'rail-tab-ai')!['aria-describedby']).toBe('rail-desc-ai')
      expect(m).toContain('rail-dot rail-dot-busy')
    })

  it.each(['idle', 'clarify', 'done', 'error', 'cancelled'] as const)(
    'shows no AI dot for a settled Prompt-bar state (%s)', (status) => {
      seed(usePromptStore, { status } as never)
      expect(html(ToolRail)).not.toContain('rail-dot')
    })
})

describe('ToolPanel', () => {
  it('mounts every panel and hides all but the selected one', () => {
    seed(useLayoutStore, { leftTab: 'stickers' })
    const m = html(ToolPanel)
    const panels = [...m.matchAll(/<div[^>]*role="tabpanel"[^>]*>/g)].map((x) => x[0])
    expect(panels).toHaveLength(RAIL_ITEMS.length)
    const shown = panels.filter((p) => !/\shidden=""/.test(p))
    expect(shown).toHaveLength(1)
    expect(shown[0]).toContain('id="tool-panel-stickers"')
    expect(shown[0]).toContain('aria-labelledby="rail-tab-stickers"')
  })

  it('titles the panel with an h2 of the selected label and names the collapse button', () => {
    seed(useLayoutStore, { leftTab: 'audio' })
    const m = html(ToolPanel)
    expect(m).toMatch(/<section class="tool-panel" id="tool-panel" aria-labelledby="tool-panel-title">/)
    expect(m).toMatch(/<h2 id="tool-panel-title">Audio<\/h2>/)
    expect(m).toMatch(/aria-label="Hide the tool panel"/)
    expect(m).toContain('data-icon="panelLeftClose"')
  })

  it('hides the whole section when collapsed, keeping the panels mounted', () => {
    seed(useLayoutStore, { leftTab: 'audio', leftOpen: false })
    const m = html(ToolPanel)
    expect(m).toMatch(/<section[^>]*id="tool-panel"[^>]*hidden=""/)
    // The Audio panel (VoRecorder) is still in the tree.
    expect(m).toContain('Record voiceover')
  })

  it('moved the audio tools out of Media and the disclosures are gone', () => {
    const m = html(ToolPanel)
    const media = m.slice(m.indexOf('id="tool-panel-media"'), m.indexOf('id="tool-panel-audio"'))
    const audio = m.slice(m.indexOf('id="tool-panel-audio"'), m.indexOf('id="tool-panel-stickers"'))
    expect(media).toContain('Drop video, audio or photos')
    expect(media).not.toMatch(/Add music|Record voiceover|<h2>Media<\/h2>/)
    expect(audio).toMatch(/Add music…[\s\S]*Record voiceover[\s\S]*Import audio file as voiceover/)
    expect(m).not.toContain('panel-disclosure')
    expect(m).not.toMatch(/Emoji &amp; sticker picker|Filters, effects &amp; LUT looks/)
    expect(m).toContain('Filters · LUT looks')
  })

  it('carries no phone or pairing wording (the feature is gated off)', () => {
    expect(html(ToolPanel)).not.toMatch(/phone|pair|\bQR\b/i)
  })
})

describe('RightPanel', () => {
  it('is the "Inspector and Chat" landmark with icon tabs and an icon toggle', () => {
    const m = html(RightPanel)
    expect(m).toMatch(/<aside id="right-panel"[^>]*aria-label="Inspector and Chat"/)
    const t = tabs(m)
    expect(t.map((x) => x.id)).toEqual(['right-tab-inspect', 'right-tab-chat'])
    expect(m).toMatch(/data-icon="inspector"[^]*?<\/svg>Inspector<\/button>/)
    expect(m).toMatch(/data-icon="chat"[^]*?<\/svg>Chat<\/button>/)
    const toggle = m.match(/<button[^>]*aria-label="Hide the Inspector and Chat panel"[^>]*>([\s\S]*?)<\/button>/)!
    expect(toggle[0]).toContain('aria-expanded="true"')
    expect(toggle[1].match(/<svg/g)).toHaveLength(1)
  })

  it('collapses to a rail of Show the panel / Inspector / Chat, keeping both tab panels mounted', () => {
    seed(useLayoutStore, { rightOpen: false, rightTab: 'chat' })
    const m = html(RightPanel)
    expect(m).toMatch(/class="right-head" hidden=""/)
    const rail = m.slice(m.indexOf('class="right-rail"'))
    const names = [...rail.matchAll(/aria-label="([^"]+)"/g)].map((x) => x[1])
    expect(names).toEqual(['Show the Inspector and Chat panel', 'Show the Inspector', 'Show the Chat'])
    expect(rail).toMatch(/aria-label="Show the Inspector and Chat panel" aria-expanded="false"/)
    expect(m).toMatch(/id="right-panel-inspect"[^>]*hidden=""/)
    expect(m).toMatch(/id="right-panel-chat"[^>]*hidden=""/)
  })
})
