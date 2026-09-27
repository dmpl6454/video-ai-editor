// LEFT_RAIL_SPEC §4.1 / §8.2: the "Panels" commands (⌥1…⌥8, ⌥\, ⌥9/⌥0,
// F6), ⌘E, ⌥⌘K and ⌥T — registered from the rail's own list, bound to the
// same chords in all three presets, colliding with nothing, and driving the
// real layout store.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Store } from './commands'

vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {}, removeItem: () => {} })
const info = vi.fn()
vi.mock('../toast', () => ({ toast: { info, error: vi.fn(), success: vi.fn() } }))
const { COMMANDS, COMMAND_BY_ID, CATEGORIES } = await import('./commands')
const { PRESETS, PRESET_IDS, PANEL_KEYS } = await import('./presets')
const { RAIL_ITEMS } = await import('../components/rail/railModel')
const { useLayoutStore } = await import('../lib/layoutStore')
const { helpGroups } = await import('../lib/helpShortcuts')
const { UI_TARGETS } = await import('./uiTargets')

// The spec's chord per rail id (§2.2): the rail's order, Media … AI.
const DIGIT: Record<string, number> = {
  media: 1, audio: 2, text: 3, stickers: 4, effects: 5, transitions: 6, captions: 7, ai: 8,
}
const SPEC_COMMAND: Record<string, string> = {
  media: 'panelMedia', audio: 'panelAudio', text: 'panelText', stickers: 'panelStickers',
  effects: 'panelEffects', transitions: 'panelTransitions', captions: 'panelCaptions', ai: 'panelAI',
}
const NEW_IDS = ['toggleToolPanel', 'showInspector', 'showChat', 'openShortcuts', 'exportVideo', 'addText',
  'cycleRegion', 'cycleRegionBack']
const run = (id: string) => COMMAND_BY_ID[id].run({} as Store)

describe('the Panels commands come from the rail\'s own list', () => {
  it('registers one global command per rail item, named by the spec, in the Panels group', () => {
    for (const r of RAIL_ITEMS) {
      expect(r.command).toBe(SPEC_COMMAND[r.id])
      const c = COMMAND_BY_ID[r.command]
      expect(c, r.id).toBeTruthy()
      expect(c.category).toBe('Panels')
      expect(c.scope).toBe('global')
      expect(c.label).toBe(`Show or hide the ${r.label} panel`)
    }
    expect(COMMANDS.filter((c) => c.id.startsWith('panel')).length).toBe(RAIL_ITEMS.length)
  })

  it('binds ⌥1…⌥8 by rail id, ⌥\\, ⌥9/⌥0, ⌥⌘K, ⌘E, ⌥T and F6 / ⇧F6 in every preset', () => {
    for (const p of PRESET_IDS) {
      const map = PRESETS[p].map as Record<string, string[]>
      for (const [id, n] of Object.entries(DIGIT)) expect(map[SPEC_COMMAND[id]], `${p} ${id}`).toEqual([`Alt+Digit${n}`])
      expect(map.toggleToolPanel).toEqual(['Alt+Backslash'])
      expect(map.showInspector).toEqual(['Alt+Digit9'])
      expect(map.showChat).toEqual(['Alt+Digit0'])
      expect(map.openShortcuts).toEqual(['Mod+Alt+KeyK'])
      expect(map.exportVideo).toEqual(['Mod+KeyE'])
      expect(map.addText).toEqual(['Alt+KeyT'])
      expect(map.cycleRegion).toEqual(['F6'])
      expect(map.cycleRegionBack).toEqual(['Shift+F6'])
    }
  })

  it('collides with no other binding in any preset (one command per chord)', () => {
    for (const p of PRESET_IDS) {
      const owners: Record<string, string[]> = {}
      for (const [cmd, chords] of Object.entries(PRESETS[p].map as Record<string, string[]>)) {
        for (const ch of chords) (owners[ch] ||= []).push(cmd)
      }
      const dupes = Object.entries(owners).filter(([, cmds]) => cmds.length > 1)
      expect(dupes, p).toEqual([])
    }
  })

  it('binds only registered commands, except the panels the rail does not show yet', () => {
    const shown = new Set(RAIL_ITEMS.map((r) => r.id))
    const pending = Object.keys(DIGIT).filter((id) => !shown.has(id as never)).map((id) => SPEC_COMMAND[id])
    for (const p of PRESET_IDS) {
      const unregistered = Object.keys(PRESETS[p].map).filter((id) => !COMMAND_BY_ID[id])
      expect(unregistered.sort(), p).toEqual([...pending].sort())
    }
    expect(Object.keys(PANEL_KEYS)).toEqual(expect.arrayContaining(NEW_IDS))
  })

  it('keeps every pre-existing command on the default scope (⌘Z stays out of AI forms)', () => {
    const scoped = new Set([...RAIL_ITEMS.map((r) => r.command), ...NEW_IDS])
    for (const c of COMMANDS) {
      if (scoped.has(c.id)) continue
      expect(c.scope ?? 'default', c.id).toBe('default')
    }
    for (const id of NEW_IDS) {
      expect(COMMAND_BY_ID[id].scope, id).toBe(id.startsWith('cycleRegion') ? 'anywhere' : 'global')
    }
  })

  it('lists a Panels group in Help (and so in ShortcutsSettings, which walks the same CATEGORIES)', () => {
    expect(CATEGORIES).toContain('Panels')
    const map = PRESETS.capcut.map
    const panels = helpGroups(COMMANDS, CATEGORIES, map, (c) => c).find((g) => g.title === 'Panels')
    expect(panels?.rows.map((r) => r.id)).toEqual([
      ...RAIL_ITEMS.map((r) => r.command), 'toggleToolPanel', 'showInspector', 'showChat', 'cycleRegion', 'cycleRegionBack',
    ])
    expect(panels?.rows[0].keys).toEqual(['Alt+Digit1'])
  })
})

describe('panel commands drive the layout store', () => {
  beforeEach(() => useLayoutStore.setState({ leftTab: 'media', leftOpen: true, rightOpen: true, rightTab: 'inspect' }))

  it('each chord shows its panel (opening a collapsed one); the same chord again collapses it', () => {
    for (const r of RAIL_ITEMS) {
      const other = RAIL_ITEMS.find((o) => o.id !== r.id)!
      useLayoutStore.setState({ leftTab: other.id, leftOpen: false })
      run(r.command)
      expect(useLayoutStore.getState(), r.id).toMatchObject({ leftTab: r.id, leftOpen: true })
      run(r.command)
      expect(useLayoutStore.getState(), r.id).toMatchObject({ leftTab: r.id, leftOpen: false })
      run(r.command)
      expect(useLayoutStore.getState().leftOpen, r.id).toBe(true)
    }
  })

  it('showing a panel puts focus on it, hiding it does not (review RD3)', () => {
    const focused: string[] = []
    vi.stubGlobal('requestAnimationFrame', (cb: () => void) => { cb(); return 0 })
    vi.stubGlobal('document', {
      activeElement: null,
      getElementById: (id: string) => ({ id, hidden: false, contains: () => false,
        focus: (o?: { preventScroll?: boolean }) => focused.push(`${id}:${o?.preventScroll}`) }),
    })
    try {
      useLayoutStore.setState({ leftTab: 'media', leftOpen: true })
      run('panelCaptions')
      expect(focused).toEqual(['tool-panel-captions:true'])
      run('panelCaptions')                       // the same chord hides it: focus is left alone
      expect(focused).toHaveLength(1)
    } finally {
      vi.unstubAllGlobals()
      vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {}, removeItem: () => {} })
    }
  })

  it('⌥\\ toggles the tool panel and keeps the tab', () => {
    useLayoutStore.setState({ leftTab: 'effects', leftOpen: true })
    run('toggleToolPanel')
    expect(useLayoutStore.getState()).toMatchObject({ leftTab: 'effects', leftOpen: false })
    run('toggleToolPanel')
    expect(useLayoutStore.getState()).toMatchObject({ leftTab: 'effects', leftOpen: true })
  })

  describe('⌥9 / ⌥0', () => {
    const focus = vi.fn()
    beforeEach(() => {
      vi.stubGlobal('requestAnimationFrame', (cb: () => void) => { cb(); return 0 })
      vi.stubGlobal('document', { querySelector: (sel: string) => (sel.includes('#right-panel-chat') ? { focus } : null) })
    })
    afterEach(() => { vi.unstubAllGlobals(); vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {}, removeItem: () => {} }) })

    it('expand the right panel on the Inspector without moving focus', () => {
      useLayoutStore.setState({ rightOpen: false, rightTab: 'chat' })
      run('showInspector')
      expect(useLayoutStore.getState()).toMatchObject({ rightOpen: true, rightTab: 'inspect' })
      expect(focus).not.toHaveBeenCalled()
    })

    it('expand it on the Chat and focus the message box', () => {
      useLayoutStore.setState({ rightOpen: false, rightTab: 'inspect' })
      run('showChat')
      expect(useLayoutStore.getState()).toMatchObject({ rightOpen: true, rightTab: 'chat' })
      expect(focus).toHaveBeenCalledTimes(1)
    })
  })
})

describe('⌘E, ⌥⌘K and ⌥T press the control they stand for', () => {
  afterEach(() => { vi.unstubAllGlobals(); vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {}, removeItem: () => {} }); info.mockClear() })

  function page(controls: Record<string, { disabled?: boolean; title?: string }>) {
    const clicks: string[] = []
    vi.stubGlobal('document', {
      querySelector: (sel: string) => {
        const c = controls[sel]
        return c ? { ...c, click: () => clicks.push(sel), getAttribute: () => null } : null
      },
    })
    return clicks
  }

  it.each([
    ['exportVideo', UI_TARGETS.exportDialog],
    ['openShortcuts', UI_TARGETS.shortcutsDialog],
    ['addText', UI_TARGETS.addText],
  ])('%s clicks %s', (id, sel) => {
    const clicks = page({ [sel]: {} })
    run(id)
    expect(clicks).toEqual([sel])
    expect(info).not.toHaveBeenCalled()
  })

  it('a disabled Export says why instead of doing nothing', () => {
    const reason = 'Nothing to export yet — add a video to the timeline first'
    const clicks = page({ [UI_TARGETS.exportDialog]: { disabled: true, title: reason } })
    run('exportVideo')
    expect(clicks).toEqual([])
    expect(info).toHaveBeenCalledWith(reason)
  })

  it('a missing control is a no-op, not a crash', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const clicks = page({})
    expect(() => run('exportVideo')).not.toThrow()
    expect(clicks).toEqual([])
    expect(warn).toHaveBeenCalled()
    warn.mockRestore()
  })
})
