// LEFT_RAIL_SPEC §4.2 / §8.2: the keymap engine's scope rule, `shouldRun`,
// on stand-in targets (the rule reads tagName, type, isContentEditable,
// closest() and the role attribute, nothing else).
import { describe, expect, it, vi } from 'vitest'
import type { KeyTarget } from './engine'

// engine.ts imports the store, which reads localStorage at module load.
vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {}, removeItem: () => {} })
const { shouldRun, isTextEntry, notePointerFocus } = await import('./engine')

function el(tagName: string, o: { type?: string; role?: string; editable?: boolean; inside?: string[]; own?: string } = {}): KeyTarget {
  // `own`: the value of the nearest [data-keymap-own] (this element or an ancestor)
  const owner: KeyTarget | null = o.own === undefined ? null : { getAttribute: (n: string) => (n === 'data-keymap-own' ? o.own ?? null : null) }
  return {
    tagName,
    type: o.type,
    isContentEditable: !!o.editable,
    closest: (sel: string) => {
      if (sel.includes('data-keymap-own')) return owner
      return (o.inside ?? []).some((w) => sel.includes(w)) ? {} : null
    },
    getAttribute: (n: string) => (n === 'role' ? o.role ?? null : null),
  }
}
/** A stand-in focused by a mouse click (Chromium leaves focus there). */
function clicked(tagName: string, o: Parameters<typeof el>[1] = {}): KeyTarget {
  const t = el(tagName, o)
  notePointerFocus(t)
  return t
}

const canvas = el('CANVAS')
const button = el('BUTTON')
const slider = el('INPUT', { type: 'range' })
const textarea = el('TEXTAREA')
const textInput = el('INPUT', { type: 'text' })
const untypedInput = el('INPUT')
const editable = el('DIV', { editable: true })
const mediaRow = el('DIV', { inside: ['data-keymap-ignore'] })
const aiCheckbox = el('INPUT', { type: 'checkbox', inside: ['data-keymap-ignore'] })
const railTab = el('BUTTON', { role: 'tab' })
const inModal = el('BUTTON', { inside: ['aria-modal', 'data-keymap-ignore'] })
// The Inspector's Speed section (review RD2): its radios, a curve point, and
// the right panel's tabs are ordinary targets now, not ignore scopes.
const speedPreset = el('BUTTON', { role: 'radio' })
const speedPoint = el('BUTTON', { own: 'Delete Backspace' })
const inspectorTab = el('BUTTON', { role: 'tab' })
const chatBox = el('TEXTAREA', { inside: ['#right-panel'] })
const promptBox = el('TEXTAREA', { inside: ['.prompt-bar'] })

describe('shouldRun: the command scope rule', () => {
  it('runs a default command on ordinary targets, and Space from a slider or a mouse-focused button (global play)', () => {
    for (const t of [canvas, button, slider, null]) {
      expect(shouldRun('default', 'Mod+KeyZ', t)).toBe(true)
    }
    for (const t of [canvas, slider, null, clicked('BUTTON'), clicked('INPUT', { type: 'checkbox' })]) {
      expect(shouldRun(undefined, 'Space', t)).toBe(true)
    }
  })

  it('a KEYBOARD-focused checkbox, button or switch keeps Space; a button also Enter (review RD3)', () => {
    const checkbox = el('INPUT', { type: 'checkbox' })
    const toggle = el('BUTTON')
    const sw = el('DIV', { role: 'switch' })
    for (const t of [checkbox, toggle, sw, el('INPUT', { type: 'radio' })]) {
      expect(shouldRun(undefined, 'Space', t)).toBe(false)
    }
    expect(shouldRun(undefined, 'Enter', toggle)).toBe(false)
    expect(shouldRun(undefined, 'Enter', checkbox)).toBe(true)     // a checkbox has no Enter of its own
    // everything else still runs there
    for (const chord of ['Mod+KeyZ', 'KeyJ', 'KeyK', 'KeyL', 'Shift+Space']) expect(shouldRun('default', chord, checkbox), chord).toBe(true)
  })

  it('runs a global command inside [data-keymap-ignore]; a default one does not', () => {
    expect(shouldRun('global', 'Alt+Digit8', mediaRow)).toBe(true)
    expect(shouldRun('global', 'Alt+Digit1', aiCheckbox)).toBe(true)
    expect(shouldRun('default', 'Alt+Digit8', mediaRow)).toBe(false)
    expect(shouldRun(undefined, 'Space', aiCheckbox)).toBe(false)
    // ⌘Z inside an AI form must not undo the timeline (why scope is per command).
    expect(shouldRun('default', 'Mod+KeyZ', aiCheckbox)).toBe(false)
  })

  it('leaves text entry to typing for everything but an anywhere command', () => {
    for (const t of [textarea, textInput, untypedInput, editable]) {
      expect(isTextEntry(t)).toBe(true)
      expect(shouldRun('default', 'KeyJ', t)).toBe(false)
      expect(shouldRun('global', 'Alt+Digit1', t)).toBe(false)
      expect(shouldRun('anywhere', 'F6', t)).toBe(true)
    }
    expect(isTextEntry(slider)).toBe(false)
    expect(isTextEntry(el('INPUT', { type: 'checkbox' }))).toBe(false)
  })

  it('keeps a focused NAVIGABLE control\'s navigation keys, with or without modifiers; a plain button has none', () => {
    const select = el('SELECT')
    const radio = el('BUTTON', { role: 'radio' })
    const menuItem = el('BUTTON', { role: 'menuitem' })
    for (const code of ['ArrowLeft', 'ArrowUp', 'Home', 'End', 'PageDown']) {
      for (const t of [slider, select, radio, menuItem, railTab]) expect(shouldRun('global', code, t), code).toBe(false)
    }
    expect(shouldRun('default', 'Alt+ArrowLeft', slider)).toBe(false)
    expect(shouldRun('default', 'ArrowLeft', canvas)).toBe(true)
    // review RD3: Shift+→ with a toolbar button focused moved nothing
    for (const chord of ['Shift+ArrowRight', 'ArrowLeft', 'Home']) {
      expect(shouldRun('default', chord, button), chord).toBe(true)
      expect(shouldRun('default', chord, el('INPUT', { type: 'checkbox' })), chord).toBe(true)
    }
  })

  it('gives a focused tab its own Space and Enter, and nothing else', () => {
    expect(shouldRun('default', 'Space', railTab)).toBe(false)
    expect(shouldRun('default', 'Enter', railTab)).toBe(false)
    // Every other global shortcut still runs with focus on the rail (review RD1).
    for (const chord of ['Mod+KeyZ', 'KeyJ', 'KeyK', 'KeyL', 'KeyN']) {
      expect(shouldRun('default', chord, railTab), chord).toBe(true)
    }
    for (const chord of ['Alt+Digit2', 'Mod+KeyE']) expect(shouldRun('global', chord, railTab), chord).toBe(true)
    expect(shouldRun('default', 'Shift+Space', railTab)).toBe(true)
  })

  it('runs ⌘Z, J/K/L and N with focus on a Speed preset radio, a curve point or the Inspector tab', () => {
    for (const t of [speedPreset, speedPoint, inspectorTab]) {
      for (const chord of ['Mod+KeyZ', 'Mod+Shift+KeyZ', 'KeyJ', 'KeyK', 'KeyL', 'KeyN']) {
        expect(shouldRun('default', chord, t), chord).toBe(true)
      }
    }
    // Space CHOOSES a keyboard-focused Speed radio (and presses a curve
    // point) as on any control (review RD3); after a mouse click it plays;
    // the Inspector TAB keeps Space/Enter to activate itself, like the rail's
    expect(shouldRun('default', 'Space', speedPreset)).toBe(false)
    expect(shouldRun('default', 'Space', speedPoint)).toBe(false)
    expect(shouldRun('default', 'Space', clicked('BUTTON', { role: 'radio' }))).toBe(true)
    expect(shouldRun('default', 'Space', inspectorTab)).toBe(false)
  })

  it('a [data-keymap-own] target keeps exactly the keys it names (a curve point\'s Delete)', () => {
    for (const chord of ['Delete', 'Backspace', 'Shift+Delete', 'Mod+Backspace']) {
      expect(shouldRun('default', chord, speedPoint), chord).toBe(false)
    }
    expect(shouldRun('default', 'Delete', button)).toBe(true)
  })

  it('in text entry runs a global ⌘ chord (⌘E, ⌥⌘K) but never a native text chord', () => {
    for (const t of [textarea, textInput, chatBox, promptBox]) {
      expect(shouldRun('global', 'Mod+KeyE', t)).toBe(true)
      expect(shouldRun('global', 'Mod+Alt+KeyK', t)).toBe(true)
      for (const chord of ['Mod+KeyA', 'Mod+KeyC', 'Mod+KeyV', 'Mod+KeyX', 'Mod+KeyZ', 'Mod+Shift+KeyZ', 'Mod+ArrowLeft', 'Mod+Backspace']) {
        expect(shouldRun('global', chord, t), chord).toBe(false)
      }
      expect(shouldRun('default', 'Mod+KeyE', t)).toBe(false)
      expect(shouldRun('global', 'Alt+Digit3', t)).toBe(false)
    }
  })

  it('runs a command from a text field inside the region it names (⌥9/⌥0 from the Chat box)', () => {
    expect(shouldRun('global', 'Alt+Digit9', chatBox, { alsoInText: '#right-panel' })).toBe(true)
    expect(shouldRun('global', 'Alt+Digit9', promptBox, { alsoInText: '#right-panel' })).toBe(false)
    expect(shouldRun('global', 'Alt+Digit9', chatBox)).toBe(false)
  })

  it('runs nothing while a modal dialog is open, whatever the scope', () => {
    for (const scope of ['default', 'global', 'anywhere'] as const) {
      expect(shouldRun(scope, 'F6', inModal)).toBe(false)
      expect(shouldRun(scope, 'Mod+KeyE', canvas, { modalOpen: true })).toBe(false)
      expect(shouldRun(scope, 'Mod+KeyE', null, { modalOpen: true })).toBe(false)
    }
  })
})

describe('formatChord: key caps in the platform\'s order', () => {
  it('orders mac modifiers the Apple way (⌃ ⌥ ⇧ ⌘) whatever the chord string says', async () => {
    const { formatChord } = await import('./engine')
    expect(formatChord('Mod+Alt+KeyK', true)).toBe('⌥⌘K')
    expect(formatChord('Mod+Shift+KeyZ', true)).toBe('⇧⌘Z')
    expect(formatChord('Mod+Ctrl+Alt+Shift+KeyB', true)).toBe('⌃⌥⇧⌘B')
    expect(formatChord('Alt+Digit1', true)).toBe('⌥1')
    expect(formatChord('Alt+Backslash', true)).toBe('⌥\\')
    expect(formatChord('Shift+F6', true)).toBe('⇧F6')
    expect(formatChord('Mod+KeyE', true)).toBe('⌘E')
  })
  it('keeps the stored order with + elsewhere', async () => {
    const { formatChord } = await import('./engine')
    expect(formatChord('Mod+Alt+KeyK', false)).toBe('Ctrl+Alt+K')
    expect(formatChord('Mod+Shift+KeyZ', false)).toBe('Ctrl+Shift+Z')
    expect(formatChord('', true)).toBe('')
  })
})
