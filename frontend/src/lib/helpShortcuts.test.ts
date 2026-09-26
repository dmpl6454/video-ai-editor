// QA-110: Help is generated from the command registry and the live keymap, so
// every bound command is listed with the keys that really trigger it — for
// every preset, and after a user override.
import { describe, expect, it, vi } from 'vitest'

// commands.ts imports the store, which reads localStorage at module load.
vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {}, removeItem: () => {} })
const { COMMANDS, CATEGORIES } = await import('../keymap/commands')
const { PRESETS, PRESET_IDS } = await import('../keymap/presets')
const { helpGroups, gestureRows } = await import('./helpShortcuts')

const label = (c: string) => `<${c}>`

describe('helpGroups (QA-110)', () => {
  it.each(PRESET_IDS)('lists every registered command exactly once for the %s preset', (id) => {
    const groups = helpGroups(COMMANDS, CATEGORIES, PRESETS[id].map, label)
    const ids = groups.flatMap((g) => g.rows.map((r) => r.id))
    expect(new Set(ids).size).toBe(ids.length)
    expect([...ids].sort()).toEqual(COMMANDS.map((c) => c.id).sort())
  })

  it.each(PRESET_IDS)('shows exactly the chords the %s preset binds', (id) => {
    const map = PRESETS[id].map
    for (const row of helpGroups(COMMANDS, CATEGORIES, map, label).flatMap((g) => g.rows)) {
      expect(row.keys, row.id).toEqual((map[row.id] ?? []).map(label))
    }
  })

  it('lists the shortcuts the hand-written table was missing, with their CapCut keys', () => {
    const rows = helpGroups(COMMANDS, CATEGORIES, PRESETS.capcut.map, label).flatMap((g) => g.rows)
    const keysOf = (id: string) => rows.find((r) => r.id === id)?.keys
    expect(keysOf('toggleSnap')).toEqual(['<KeyN>'])
    expect(keysOf('zoomFit')).toEqual(['<Mod+Backslash>'])
    expect(keysOf('goToStart')).toEqual(['<Home>'])
    expect(keysOf('goToEnd')).toEqual(['<End>'])
    expect(keysOf('nudgeLeft')).toEqual(['<Alt+ArrowLeft>'])
    expect(keysOf('copy')).toEqual(['<Mod+KeyC>'])
    expect(keysOf('paste')).toEqual(['<Mod+KeyV>'])
    expect(keysOf('selectAll')).toEqual(['<Mod+KeyA>'])
    // Registered but unbound in this preset: listed, with no key.
    expect(keysOf('clearMarks')).toEqual([])
  })

  it('follows an override (the same merge as the engine: an override replaces the preset chords)', () => {
    const map = { ...PRESETS.capcut.map, toggleSnap: ['Shift+KeyG'] }
    const rows = helpGroups(COMMANDS, CATEGORIES, map, label).flatMap((g) => g.rows)
    expect(rows.find((r) => r.id === 'toggleSnap')?.keys).toEqual(['<Shift+KeyG>'])
  })

  it('groups in category order and keeps a category the order list does not know', () => {
    const cmds = [
      { id: 'a', label: 'A', category: 'History' },
      { id: 'b', label: 'B', category: 'Transport' },
      { id: 'c', label: 'C', category: 'Brand new' },
    ]
    const groups = helpGroups(cmds, ['Transport', 'History'], {}, label)
    expect(groups.map((g) => g.title)).toEqual(['Transport', 'History', 'Brand new'])
  })

  it('names no key in a command label (the key column says it, and a rebind would make it wrong)', () => {
    for (const c of COMMANDS) expect(c.label, c.id).not.toMatch(/\([A-Z]\)$/)
  })

  it('has platform-correct gesture rows', () => {
    expect(gestureRows(true).find((r) => r.id === 'gesture:zoomWheel')?.keys).toEqual(['⌘ + scroll'])
    expect(gestureRows(false).find((r) => r.id === 'gesture:zoomWheel')?.keys).toEqual(['Ctrl + scroll'])
  })
})
