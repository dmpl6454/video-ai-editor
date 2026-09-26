import { describe, expect, it } from 'vitest'
import {
  CENTRE_MIN, LEFT_OPEN_KEY, LEFT_TAB_KEY, LEFT_W_KEY, PANEL_MAX, PANEL_MIN, RIGHT_OPEN_KEY, RIGHT_W_KEY,
  clampPanelWidth, createLayoutStore, defaultWidths, readBool, readLeftTab, readWidth,
} from './layoutStore'
import { RIGHT_TAB_KEY } from './rightTab'

// A Map-backed localStorage, so the persistence contract is measured on the
// exact keys the app writes.
function kv(init: Record<string, string> = {}) {
  const m = new Map(Object.entries(init))
  return {
    map: m,
    getItem: (k: string) => m.get(k) ?? null,
    setItem: (k: string, v: string) => { m.set(k, v) },
    removeItem: (k: string) => { m.delete(k) },
  }
}
const make = (init: Record<string, string> = {}, width = 1440) => {
  const s = kv(init)
  return { s, store: createLayoutStore(s, () => width) }
}

describe('the remembered tool panel (vai.leftTab)', () => {
  it('loads the pre-rail values unchanged — no migration', () => {
    for (const v of ['media', 'transitions', 'ai']) expect(readLeftTab(kv({ [LEFT_TAB_KEY]: v }))).toBe(v)
  })
  it('loads the panels the rail added', () => {
    for (const v of ['audio', 'stickers', 'effects', 'text', 'captions']) expect(readLeftTab(kv({ [LEFT_TAB_KEY]: v }))).toBe(v)
  })
  it('falls back to Media for an unknown value, an id the rail never shows, or nothing', () => {
    expect(readLeftTab(kv({ [LEFT_TAB_KEY]: 'bogus' }))).toBe('media')
    expect(readLeftTab(kv({ [LEFT_TAB_KEY]: 'phone' }))).toBe('media')
    expect(readLeftTab(kv())).toBe('media')
    expect(readLeftTab(null)).toBe('media')
  })
  it('survives a storage that throws (blocked site data)', () => {
    const boom = { getItem: () => { throw new Error('denied') }, setItem: () => { throw new Error('denied') } }
    expect(readLeftTab(boom)).toBe('media')
    const store = createLayoutStore(boom, () => 1440)
    store.getState().showTab('ai')
    expect(store.getState().leftTab).toBe('ai')
  })
})

describe('showTab: another id opens it, the same id toggles', () => {
  it('selects and opens another panel', () => {
    const { s, store } = make({ [LEFT_OPEN_KEY]: 'false' })
    expect(store.getState().leftOpen).toBe(false)
    store.getState().showTab('effects', { toggle: true })
    expect(store.getState()).toMatchObject({ leftTab: 'effects', leftOpen: true })
    expect(s.map.get(LEFT_TAB_KEY)).toBe('effects')
    expect(s.map.get(LEFT_OPEN_KEY)).toBe('true')
  })
  it('collapses and re-opens on the active panel with toggle', () => {
    const { s, store } = make()
    store.getState().showTab('media', { toggle: true })
    expect(store.getState()).toMatchObject({ leftTab: 'media', leftOpen: false })
    expect(s.map.get(LEFT_OPEN_KEY)).toBe('false')
    store.getState().showTab('media', { toggle: true })
    expect(store.getState().leftOpen).toBe(true)
  })
  it('without toggle only ever opens (arrow keys in the rail)', () => {
    const { store } = make({ [LEFT_OPEN_KEY]: 'false' })
    store.getState().showTab('media')
    expect(store.getState().leftOpen).toBe(true)
    store.getState().showTab('media')
    expect(store.getState().leftOpen).toBe(true)
  })
  it('ignores an id the rail does not show', () => {
    const { store } = make()
    store.getState().showTab('phone' as never)
    expect(store.getState().leftTab).toBe('media')
  })
  it('vai.leftOpen defaults to open and reads only true/false', () => {
    expect(make().store.getState().leftOpen).toBe(true)
    expect(readBool(kv({ k: 'yes' }), 'k', true)).toBe(true)
    expect(readBool(kv({ k: 'false' }), 'k', true)).toBe(false)
  })
})

describe('panel widths are null until dragged (critique H7)', () => {
  it('is null by default and when the key is absent or garbage', () => {
    const { store } = make()
    expect(store.getState().leftW).toBeNull()
    expect(store.getState().rightW).toBeNull()
    expect(readWidth(kv({ [LEFT_W_KEY]: '' }), LEFT_W_KEY)).toBeNull()
    expect(readWidth(kv({ [LEFT_W_KEY]: 'wide' }), LEFT_W_KEY)).toBeNull()
  })
  it('loads a stored width, clamping a legacy value below the new floor', () => {
    expect(readWidth(kv({ [LEFT_W_KEY]: '300' }), LEFT_W_KEY)).toBe(300)
    expect(readWidth(kv({ [LEFT_W_KEY]: '160' }), LEFT_W_KEY)).toBe(PANEL_MIN)   // store.ts allowed 160
    expect(readWidth(kv({ [RIGHT_W_KEY]: '9000' }), RIGHT_W_KEY)).toBe(PANEL_MAX)
  })
  it('clamps a drag to [180, 640]', () => {
    const { s, store } = make({}, 2560)
    expect(store.getState().setPanelWidth('left', 90)).toBe(180)
    expect(store.getState().setPanelWidth('left', 5000)).toBe(640)
    expect(store.getState().leftW).toBe(640)
    expect(s.map.get(LEFT_W_KEY)).toBe('640')
  })
  it('never takes the centre below 440 px', () => {
    // 1024: rail 48, right 260, splitters 12 → the left may take 1024-48-260-12-440 = 264.
    const { store } = make({}, 1024)
    expect(store.getState().setPanelWidth('left', 600)).toBe(1024 - 48 - 260 - 12 - CENTRE_MIN)
    // A collapsed right panel (36 px rail) frees the rest.
    store.getState().setRightOpen(false)
    expect(store.getState().setPanelWidth('left', 600)).toBe(1024 - 48 - 36 - 12 - CENTRE_MIN)
  })
  it('null forgets the drag and removes the key (back to the CSS default)', () => {
    const { s, store } = make({ [RIGHT_W_KEY]: '300' })
    expect(store.getState().rightW).toBe(300)
    expect(store.getState().setPanelWidth('right', null)).toBeNull()
    expect(store.getState().rightW).toBeNull()
    expect(s.map.has(RIGHT_W_KEY)).toBe(false)
  })
  it('clampPanelWidth keeps the floor even when the budget is smaller', () => {
    expect(clampPanelWidth(300, 100)).toBe(PANEL_MIN)
    expect(clampPanelWidth(250.6)).toBe(251)
  })
  it('defaultWidths mirrors the §1.2 table', () => {
    expect(defaultWidths(1024)).toEqual({ rail: 48, left: 220, right: 260 })
    expect(defaultWidths(1280)).toEqual({ rail: 64, left: 240, right: 280 })
    expect(defaultWidths(1440)).toEqual({ rail: 64, left: 280, right: 280 })
    expect(defaultWidths(1920)).toEqual({ rail: 64, left: 320, right: 320 })
  })
})

describe('the right panel', () => {
  it('reads vai.rightPanelOpen and vai.rightTab (unchanged keys)', () => {
    const { store } = make({ [RIGHT_OPEN_KEY]: 'false', [RIGHT_TAB_KEY]: 'chat' })
    expect(store.getState()).toMatchObject({ rightOpen: false, rightTab: 'chat' })
  })
  it('showRight opens it on a tab and persists both', () => {
    const { s, store } = make({ [RIGHT_OPEN_KEY]: 'false' })
    store.getState().showRight('chat')
    expect(store.getState()).toMatchObject({ rightOpen: true, rightTab: 'chat' })
    expect(s.map.get(RIGHT_OPEN_KEY)).toBe('true')
    expect(s.map.get(RIGHT_TAB_KEY)).toBe('chat')
  })
  it('setRightOpen and setRightTab persist', () => {
    const { s, store } = make()
    store.getState().setRightOpen(false)
    store.getState().setRightTab('chat')
    expect(s.map.get(RIGHT_OPEN_KEY)).toBe('false')
    expect(s.map.get(RIGHT_TAB_KEY)).toBe('chat')
  })
})

describe('AI deep links (consumed in R5)', () => {
  it('jumpToAi selects AI, opens the panel and bumps the nonce', () => {
    const { store } = make({ [LEFT_OPEN_KEY]: 'false' })
    store.getState().jumpToAi({ tool: 'diarize', from: 'audio' })
    const a = store.getState().aiJump
    expect(store.getState()).toMatchObject({ leftTab: 'ai', leftOpen: true })
    expect(a).toMatchObject({ tool: 'diarize', from: 'audio', nonce: 1 })
    store.getState().jumpToAi({ tool: 'diarize', from: 'audio' })
    expect(store.getState().aiJump?.nonce).toBe(2)
    store.getState().clearAiJump()
    expect(store.getState().aiJump).toBeNull()
  })
})

describe('toggleLeftOpen (⌥\\, LEFT_RAIL_SPEC §4.1)', () => {
  it('collapses and re-opens the tool panel, keeps the tab, and persists the state', () => {
    const { s, store } = make({ [LEFT_TAB_KEY]: 'effects' })
    store.getState().toggleLeftOpen()
    expect(store.getState()).toMatchObject({ leftTab: 'effects', leftOpen: false })
    expect(s.map.get(LEFT_OPEN_KEY)).toBe('false')
    store.getState().toggleLeftOpen()
    expect(store.getState()).toMatchObject({ leftTab: 'effects', leftOpen: true })
    expect(s.map.get(LEFT_OPEN_KEY)).toBe('true')
  })
})
