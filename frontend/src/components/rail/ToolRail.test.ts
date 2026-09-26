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
const { ToolRail, RailFoot } = await import('./ToolRail')
const { ToolPanel } = await import('./ToolPanel')
const { RightPanel } = await import('../RightPanel')
const { useLayoutStore } = await import('../../lib/layoutStore')
const { useKeymapStore } = await import('../../keymap/engine')
const { useAiRuns } = await import('../../lib/aiRuns')
const { usePromptStore } = await import('../../lib/promptStore')
const { useActivityStore } = await import('../../lib/activityStore')
const { useCaptionRun } = await import('../../lib/captionRun')
const { RAIL_ITEMS } = await import('./railModel')
const { usePhonePairing } = await import('./phonePairing')

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
  seed(useActivityStore, { recording: null, captions: null, liveMessage: '' })
  seed(usePhonePairing, { enabled: false })
  seed(useCaptionRun, { busy: false, progress: 0, elapsed: 0, cancelling: false, jobId: null, consent: null, downloads: null })
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

  it('carries the preset chords (R4): aria-keyshortcuts Alt+n and a ⌥n key cap per tab', () => {
    const want: Record<string, string> = { media: '1', audio: '2', text: '3', stickers: '4', effects: '5', transitions: '6', captions: '7', ai: '8' }
    for (const t of tabs(html(ToolRail))) {
      const id = t.id.replace('rail-tab-', '')
      expect(t['aria-keyshortcuts'], id).toBe(`Alt+${want[id]}`)
      expect(t['data-kbd'], id).toMatch(new RegExp(`^(⌥|Alt\\+)${want[id]}$`))
    }
  })

  it('names no chord for a command the keymap does not bind (a user unbinds it)', () => {
    seed(useKeymapStore, { overrides: { panelMedia: [] } })
    const media = tabs(html(ToolRail)).find((t) => t.id === 'rail-tab-media')!
    expect(media['aria-keyshortcuts']).toBeUndefined()
    expect(media['data-kbd']).toBeUndefined()
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

  // R2, carried from R1: the Audio dot while a voiceover records, the
  // Captions dot while captions transcribe — both from lib/activityStore, the
  // run the top-bar activity chip shows (§2.3, §2.8).
  it('wears the recording dot on Audio while a take records, described not named', () => {
    const idle = html(ToolRail)
    expect(tabs(idle).find((t) => t.id === 'rail-tab-audio')!['aria-describedby']).toBeUndefined()
    expect(idle).toMatch(/<span id="rail-desc-audio"[^>]*\shidden=""[^>]*>Recording in progress<\/span>/)
    seed(useActivityStore, { recording: { startedAt: 0, stop: () => {} } })
    const m = html(ToolRail)
    const audio = tabs(m).find((t) => t.id === 'rail-tab-audio')!
    expect(audio['aria-describedby']).toBe('rail-desc-audio')
    expect(m).toContain('rail-dot rail-dot-rec')
    expect(m).not.toContain('rail-dot-busy')
    // The name stays exactly the label.
    const inner = m.slice(m.indexOf('id="rail-tab-audio"'))
    expect(inner.slice(inner.indexOf('>') + 1, inner.indexOf('</button>')).replace(/<svg[\s\S]*?<\/svg>/g, '').replace(/<[^>]+>/g, '')).toBe('Audio')
  })

  it('wears the busy dot on Captions while captions transcribe', () => {
    seed(useActivityStore, { captions: { progress: 0.4, etaS: 30, elapsedS: 20, cancelling: false, cancel: () => {} } })
    const m = html(ToolRail)
    expect(tabs(m).find((t) => t.id === 'rail-tab-captions')!['aria-describedby']).toBe('rail-desc-captions')
    expect(m).toMatch(/id="rail-desc-captions"[^>]*>Captions are being generated</)
    expect(m).toContain('rail-dot rail-dot-busy')
    expect(tabs(m).find((t) => t.id === 'rail-tab-audio')!['aria-describedby']).toBeUndefined()
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

  // R3 (LEFT_RAIL_SPEC §2.5, §8.1: "no phone wording" moved here from
  // TopBar.test): with the flag off the header action is absent from the
  // markup — not hidden — and nothing names the phone.
  it('carries no phone or pairing wording while phone_pairing is off', () => {
    const m = html(ToolPanel)
    expect(m).not.toMatch(/phone|pair|\bQR\b/i)
    expect(m).not.toContain('tool-panel-act')
  })

  it('shows "From iPhone" in the Media header, before the collapse button, when phone_pairing is on', () => {
    seed(usePhonePairing, { enabled: true })
    const m = html(ToolPanel)
    const head = m.slice(m.indexOf('tool-panel-head'), m.indexOf('role="tabpanel"'))
    expect(head).toMatch(/<button type="button" class="tool-panel-act" aria-label="From iPhone"[^>]*><svg[^>]*data-icon="phone"/)
    expect(head.indexOf('From iPhone')).toBeLessThan(head.indexOf('Hide the tool panel'))
    // The pairing panel itself is mounted only once opened.
    expect(m).not.toContain('phone-scrim')
  })

  it('keeps "From iPhone" to the Media panel', () => {
    seed(usePhonePairing, { enabled: true })
    seed(useLayoutStore, { leftTab: 'audio' })
    expect(html(ToolPanel)).not.toContain('From iPhone')
  })

  // R2: the top bar's Text tool and CC Captions, inline in their panels.
  const panel = (m: string, id: string) => {
    const at = m.indexOf(`id="tool-panel-${id}"`)
    return m.slice(at, m.indexOf('role="tabpanel"', at + 30))
  }

  it('Text: the add button leads [data-text-presets], then the field, 6 styles and 4 templates', () => {
    seed(useLayoutStore, { leftTab: 'text' })
    const t = panel(html(ToolPanel), 'text')
    // The a11y suite's `[data-text-presets] > button` helper: the FIRST button.
    const wrap = t.slice(t.indexOf('data-text-presets'))
    expect(wrap.slice(wrap.indexOf('<button'), wrap.indexOf('</button>'))).toMatch(/data-icon="plus"[\s\S]*Add text at playhead/)
    expect(t).toMatch(/<label[^>]*>[\s\S]*Text, #hashtag or @handle[\s\S]*<input type="text"/)
    const group = (name: string) => {
      const g = t.slice(t.indexOf(`aria-label="${name}"`))
      return g.slice(0, g.indexOf('</div>'))
    }
    const labels = (g: string) => [...g.matchAll(/<button\b([^>]*)>([\s\S]*?)<\/button>/g)]
      .map((x) => ({ attrs: x[1], text: x[2].replace(/<[^>]+>/g, '').trim() }))
    expect(labels(group('Text styles')).map((b) => b.text)).toEqual(
      ['AaTitle box', 'AaSubtitle band', 'AaYellow pop', 'AaSide label', 'AaQuote', 'AaNeon'])
    const tpl = labels(group('Text templates'))
    expect(tpl.map((b) => b.text)).toEqual(['3 · 2 · 1', 'Callout →', '#Hashtag', '@Handle'])
    // needsField: #Hashtag and @Handle wait for text; the others never do.
    expect(tpl.map((b) => /\sdisabled=""/.test(b.attrs))).toEqual([false, false, true, true])
    expect(tpl[2].attrs).toContain('title="Chunky hashtag near the bottom — type the hashtag above first"')
    // The ⌥T chord is named only while the live keymap binds addText.
    seed(useKeymapStore, { overrides: { addText: [] } })
    expect(panel(html(ToolPanel), 'text')).not.toMatch(/aria-keyshortcuts=|text-panel-kbd/)
    seed(useKeymapStore, { overrides: { addText: ['Alt+KeyT'] } })
    expect(panel(html(ToolPanel), 'text')).toMatch(/aria-keyshortcuts="Alt\+T"/)
  })

  it('Captions: Generate, the language and speed radios with hints, Caption style…', () => {
    seed(useLayoutStore, { leftTab: 'captions' })
    const c = panel(html(ToolPanel), 'captions')
    // No footage on v1 → disabled, and the reason is on screen, not only in a title.
    expect(c).toMatch(/<button[^>]*class="panel-btn cc-generate"[^>]*disabled=""[^>]*>[\s\S]*Generate captions/)
    expect(c).toContain('captions transcribe the main (v1) footage')
    const attrs = (tag: string) => Object.fromEntries([...tag.matchAll(/([\w-]+)="([^"]*)"/g)].map((a) => [a[1], a[2]]))
    const radios = [...c.matchAll(/<input type="radio"[^>]*>/g)].map((m) => attrs(m[0]))
    expect(radios.map((r) => `${r.name}:${r.value}`)).toEqual([
      'cc-target:as-spoken', 'cc-target:en', 'cc-target:hi', 'cc-target:hinglish', 'cc-target:es',
      'cc-speed:quality', 'cc-speed:fast'])
    // Each radio is NAMED by its label alone; the hint describes it.
    const nameOf = (id: string) => c.match(new RegExp(`id="${id}" class="cc-opt-label">([^<]+)<`))![1]
    expect(radios.map((r) => nameOf(r['aria-labelledby']))).toEqual(
      ['As spoken', 'English', 'हिंदी Hindi', 'Hinglish', 'Español', 'Best quality', 'Fastest'])
    expect(c).toMatch(/aria-describedby="cc-target-en-hint"/)
    expect(c).toContain('Translate to English (works from any language)')
    expect(c).toContain('Caption style…')
    expect(c).not.toContain('cc-progress')
  })

  it('Captions: Fastest wears its download size while its model is missing', () => {
    seed(useLayoutStore, { leftTab: 'captions' })
    seed(useCaptionRun, { downloads: {
      'captions:large-v3': { what: 'the accurate caption model', bytes: 3.1e9, cached: true },
      'captions:large-v3-turbo': { what: 'the fast caption model', bytes: 1.6e9, cached: false },
    } })
    const c = panel(html(ToolPanel), 'captions')
    expect(c).toMatch(/id="cc-speed-fast-dl" class="cc-opt-dl">Downloads 1.6 GB first</)
    expect(c).not.toContain('cc-speed-quality-dl')
    expect(c).toMatch(/aria-describedby="cc-speed-fast-hint cc-speed-fast-dl"/)
  })

  it('Captions: a live run shows %, ETA and Cancel; cancelling shows "Stopping…" and no Cancel', () => {
    seed(useLayoutStore, { leftTab: 'captions' })
    seed(useCaptionRun, { busy: true, progress: 0.42, elapsed: 21, jobId: 'j1' })
    let c = panel(html(ToolPanel), 'captions')
    expect(c).toMatch(/class="cc-progress"/)
    expect(c).toContain('Transcribing… 42%')
    expect(c).toContain('29s left')                 // 21 s for 42 % → 29 s more
    expect(c).toMatch(/<button[^>]*class="panel-btn cc-cancel"[^>]*>Cancel<\/button>/)
    expect(c).toMatch(/<fieldset class="cc-opts" disabled="">/)
    seed(useCaptionRun, { cancelling: true })
    c = panel(html(ToolPanel), 'captions')
    expect(c).toContain('Stopping…')
    expect(c).toContain('finishing the current chunk · 21s')
    expect(c).not.toContain('cc-cancel')
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

// R3 (LEFT_RAIL_SPEC §2.2, §8.1): Help, Customize shortcuts and Settings left
// the top bar for the rail foot — its own nav, the same names, lucide icons.
describe('RailFoot', () => {
  const buttons = (m: string) => [...m.matchAll(/<button[^>]*>/g)].map((b) =>
    Object.fromEntries([...b[0].matchAll(/([\w-]+)="([^"]*)"/g)].map((a) => [a[1], a[2]])))

  it('is the nav "Help and settings" with the three buttons in order, named as before', () => {
    const m = html(RailFoot)
    expect(m).toMatch(/^<nav class="rail-foot" aria-label="Help and settings">/)
    expect(buttons(m).map((b) => b['aria-label'])).toEqual(['Keyboard shortcuts', 'Customize keyboard shortcuts', 'Settings'])
  })

  it('draws the lucide help and keyboard icons, never the ⌨ glyph (moved from TopBar.test)', () => {
    const m = html(RailFoot)
    expect(m).toMatch(/aria-label="Keyboard shortcuts"[^>]*><svg[^>]*class="lucide[^"]*"[^>]*data-icon="help"/)
    expect(m).toMatch(/aria-label="Customize keyboard shortcuts"[^>]*><svg[^>]*class="lucide lucide-keyboard icon"/)
    expect(m).toMatch(/aria-label="Settings"[^>]*><svg[^>]*data-icon="settings"/)
    expect(m).not.toContain('⌨')
  })

  it('carries each live chord in aria-keyshortcuts and the tooltip, with no title', () => {
    const [help, custom, settings] = buttons(html(RailFoot))
    expect(help['aria-keyshortcuts']).toBe('?')
    expect(help['data-kbd']).toBe('?')
    expect(help['data-tip']).toBe('Help and keyboard shortcuts')
    expect(custom['aria-keyshortcuts']).toMatch(/^(Meta|Control)\+Alt\+K$/)
    expect(custom['data-kbd']).toBeTruthy()
    expect(settings['aria-keyshortcuts']).toMatch(/^(Meta|Control)\+,$/)
    for (const b of [help, custom, settings]) expect(b.title).toBeUndefined()
  })

  it('names no chord once the user unbinds it', () => {
    seed(useKeymapStore, { overrides: { openShortcuts: [] } })
    const [, custom] = buttons(html(RailFoot))
    expect(custom['aria-keyshortcuts']).toBeUndefined()
    expect(custom['data-kbd']).toBeUndefined()
  })
})
