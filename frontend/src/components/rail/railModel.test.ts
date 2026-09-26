import { describe, expect, it } from 'vitest'
import { ICONS } from '../../lib/icons'
import { AI_CATALOG } from '../../lib/aiCatalog'
import { PRESETS } from '../../keymap/presets'
import {
  RAIL_ITEMS, ariaKeyshortcuts, asRailId, railItem, railKeyStep, railPanelId, railTabId,
} from './railModel'

describe('the rail model (LEFT_RAIL_SPEC §2.2)', () => {
  it('holds R1\'s six items in CapCut order', () => {
    expect(RAIL_ITEMS.map((r) => r.id)).toEqual(['media', 'audio', 'stickers', 'effects', 'transitions', 'ai'])
    expect(RAIL_ITEMS.map((r) => r.label)).toEqual(['Media', 'Audio', 'Stickers', 'Effects', 'Transitions', 'AI'])
  })
  it('has unique ids, labels and commands', () => {
    for (const k of ['id', 'label', 'command'] as const) {
      expect(new Set(RAIL_ITEMS.map((r) => r[k])).size, k).toBe(RAIL_ITEMS.length)
    }
  })
  it('draws every item with an icon from the app set (lucide, lib/icons)', () => {
    for (const r of RAIL_ITEMS) expect(ICONS[r.icon], r.id).toBeTruthy()
  })
  it('keeps the tooltip text free of the label and of a chord (both are added live)', () => {
    for (const r of RAIL_ITEMS) {
      expect(r.tip.length).toBeGreaterThan(0)
      expect(r.tip).not.toMatch(/⌥|Alt\+/)
    }
  })
  it('counts the AI tools from the catalogue, not a hard-coded number', () => {
    expect(railItem('ai').tip).toBe(`Every AI tool (${AI_CATALOG.length})`)
  })
  it('binds no chord yet: the panel commands arrive with the keymap phase (R4)', () => {
    for (const preset of Object.values(PRESETS)) {
      const map = preset.map as Record<string, string[]>
      for (const r of RAIL_ITEMS) expect(map[r.command], `${r.command}`).toBeUndefined()
    }
  })
  it('ties each tab to its panel by id', () => {
    expect(railTabId('audio')).toBe('rail-tab-audio')
    expect(railPanelId('audio')).toBe('tool-panel-audio')
  })
  it('accepts only ids the rail shows', () => {
    expect(asRailId('effects')).toBe('effects')
    expect(asRailId('captions')).toBeNull()
    expect(asRailId(undefined)).toBeNull()
    expect(railItem('captions').id).toBe('media')
  })
})

describe('railKeyStep: vertical roving tabindex', () => {
  it('moves down and up with wrap-around', () => {
    expect(railKeyStep('ArrowDown', 0, 6)).toBe(1)
    expect(railKeyStep('ArrowDown', 5, 6)).toBe(0)
    expect(railKeyStep('ArrowUp', 0, 6)).toBe(5)
    expect(railKeyStep('ArrowUp', 3, 6)).toBe(2)
  })
  it('jumps with Home and End', () => {
    expect(railKeyStep('Home', 4, 6)).toBe(0)
    expect(railKeyStep('End', 1, 6)).toBe(5)
  })
  it('leaves every other key alone (←/→ are not the rail\'s; Tab goes into the panel)', () => {
    for (const k of ['ArrowLeft', 'ArrowRight', 'Tab', 'Enter', ' ']) expect(railKeyStep(k, 2, 6)).toBe(-1)
    expect(railKeyStep('ArrowDown', 0, 0)).toBe(-1)
  })
})

describe('ariaKeyshortcuts: keymap chords in ARIA syntax', () => {
  it('names the modifiers and keys the way aria-keyshortcuts expects', () => {
    expect(ariaKeyshortcuts('Alt+Digit1', true)).toBe('Alt+1')
    expect(ariaKeyshortcuts('Mod+Alt+KeyK', true)).toBe('Meta+Alt+K')
    expect(ariaKeyshortcuts('Mod+Alt+KeyK', false)).toBe('Control+Alt+K')
    expect(ariaKeyshortcuts('Alt+Backslash', true)).toBe('Alt+\\')
    expect(ariaKeyshortcuts('', true)).toBe('')
  })
})
