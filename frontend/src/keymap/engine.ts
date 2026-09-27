import { useEffect } from 'react'
import { create } from 'zustand'
import { useStore } from '../store'
import { COMMAND_BY_ID, type CommandScope } from './commands'
import { PRESETS, DEFAULT_PRESET, type KeyMap, type PresetId } from './presets'

/**
 * Keymap engine: turns a KeyboardEvent into a normalised chord, resolves it
 * against the active preset (+ user overrides), and runs the matching command.
 * Preset choice and per-command overrides persist to localStorage.
 */

// Platform detection: prefer userAgentData.platform (Chromium, incl. Edge
// WebView2 — reports "Windows"/"macOS"), fall back to the legacy
// navigator.platform (WKWebView/Safari never shipped userAgentData; they
// report "MacIntel", WebView2's fallback is "Win32"). Exported so UI panels
// (Help) can label non-keymap gestures (e.g. "⌘+scroll" vs "Ctrl+scroll").
const _platform: string =
  typeof navigator === 'undefined'
    ? ''
    : ((navigator as unknown as { userAgentData?: { platform?: string } })
        .userAgentData?.platform || navigator.platform || '')
export const IS_MAC = /mac|iphone|ipad|ipod/i.test(_platform)

// ---- chord normalisation (layout-independent, from KeyboardEvent.code) ----

export function chordFromEvent(e: KeyboardEvent): string {
  const parts: string[] = []
  // 'Mod' is the PLATFORM's primary command modifier: ⌘ (metaKey) on macOS,
  // Ctrl on Windows/Linux — so every 'Mod+…' preset chord works with Ctrl on
  // a Windows keyboard and ⌘ on a Mac. The other of the two modifiers is
  // emitted as its own token ('Ctrl' on mac, 'Meta' = Win-key elsewhere),
  // NOT collapsed into 'Mod' (the old `metaKey || ctrlKey` rule made mac
  // Ctrl+B fire the ⌘B binding and ⌘⌃B indistinguishable from ⌘B) and NOT
  // dropped (dropping would make mac Ctrl+S look like a bare 'KeyS' and fire
  // CapCut's single-key split). Because the full modifier set is serialized
  // into the chord string and looked up exactly, Mod+KeyZ never fires on
  // Mod+Shift+KeyZ, and 'Ctrl+…'/'Meta+…' chords only match if a user
  // deliberately rebinds a command to them.
  if (IS_MAC ? e.metaKey : e.ctrlKey) parts.push('Mod')
  if (IS_MAC ? e.ctrlKey : e.metaKey) parts.push(IS_MAC ? 'Ctrl' : 'Meta')
  if (e.altKey) parts.push('Alt')
  if (e.shiftKey) parts.push('Shift')
  // The physical key. Ignore bare modifier presses.
  const code = e.code
  if (['MetaLeft', 'MetaRight', 'ControlLeft', 'ControlRight',
       'ShiftLeft', 'ShiftRight', 'AltLeft', 'AltRight'].includes(code)) {
    return ''
  }
  parts.push(code)
  return parts.join('+')
}

const KEY_LABELS: Record<string, string> = {
  Space: 'Space', ArrowLeft: '←', ArrowRight: '→', ArrowUp: '↑', ArrowDown: '↓',
  Comma: ',', Period: '.', Equal: '=', Minus: '−', Backslash: '\\',
  BracketLeft: '[', BracketRight: ']', Slash: '/', Semicolon: ';',
  Delete: 'Del', Backspace: '⌫', Enter: '↵', Escape: 'Esc', Home: 'Home', End: 'End',
}

/** Mac modifier glyphs in Apple's order (⌃ ⌥ ⇧ ⌘, as in every menu: ⇧⌘Z,
 *  ⌥⌘K), whatever order the chord string stores them in. */
const MAC_MOD_ORDER: Record<string, number> = { Ctrl: 0, Alt: 1, Shift: 2, Mod: 3 }

/** Human-readable chord for a platform, e.g. "⌘B", "⌥⌘K", "Shift+Del", "=". */
export function formatChord(chord: string, isMac: boolean): string {
  if (!chord) return ''
  const parts = chord.split('+')
  const key = parts.pop() as string
  const mods = isMac ? [...parts].sort((a, b) => (MAC_MOD_ORDER[a] ?? 9) - (MAC_MOD_ORDER[b] ?? 9)) : parts
  return [...mods, key].map((p) => {
    if (p === 'Mod') return isMac ? '⌘' : 'Ctrl'
    if (p === 'Ctrl') return isMac ? '⌃' : 'Ctrl'   // secondary modifier (mac only)
    if (p === 'Meta') return isMac ? '⌘' : 'Win'    // secondary modifier (win/linux only)
    if (p === 'Alt') return isMac ? '⌥' : 'Alt'
    if (p === 'Shift') return isMac ? '⇧' : 'Shift'
    if (p.startsWith('Key')) return p.slice(3)
    if (p.startsWith('Digit')) return p.slice(5)
    return KEY_LABELS[p] ?? p
  }).join(isMac ? '' : '+')
}

/** Human-readable chord on this platform. */
export function chordLabel(chord: string): string {
  return formatChord(chord, IS_MAC)
}

// ---- persistence ----

const LS_KEY = 'vae.keymap.v1'

interface Persisted { presetId: PresetId; overrides: KeyMap }

function load(): Persisted {
  try {
    const raw = localStorage.getItem(LS_KEY)
    if (raw) {
      const p = JSON.parse(raw)
      if (p && PRESETS[p.presetId as PresetId]) {
        return { presetId: p.presetId, overrides: p.overrides || {} }
      }
    }
  } catch { /* ignore */ }
  return { presetId: DEFAULT_PRESET, overrides: {} }
}

function save(p: Persisted) {
  try { localStorage.setItem(LS_KEY, JSON.stringify(p)) } catch { /* ignore */ }
}

// ---- keymap store ----

interface KeymapState {
  presetId: PresetId
  overrides: KeyMap                       // commandId → chords (replaces preset)
  effectiveMap(): KeyMap                   // preset merged with overrides
  chordToCommand(): Record<string, string> // chord → commandId (for lookup)
  setPreset(id: PresetId): void
  rebind(commandId: string, chords: string[]): void
  resetCommand(commandId: string): void
  resetAll(): void
}

export const useKeymapStore = create<KeymapState>((set, get) => {
  const init = load()
  return {
    presetId: init.presetId,
    overrides: init.overrides,
    effectiveMap: () => {
      const base = PRESETS[get().presetId].map
      return { ...base, ...get().overrides }
    },
    chordToCommand: () => {
      const map = get().effectiveMap()
      const out: Record<string, string> = {}
      for (const [cmd, chords] of Object.entries(map)) {
        for (const ch of chords) out[ch] = cmd  // last write wins on conflict
      }
      return out
    },
    setPreset: (id) => {
      set({ presetId: id })
      save({ presetId: id, overrides: get().overrides })
    },
    rebind: (commandId, chords) => {
      const overrides = { ...get().overrides, [commandId]: chords }
      set({ overrides })
      save({ presetId: get().presetId, overrides })
    },
    resetCommand: (commandId) => {
      const overrides = { ...get().overrides }
      delete overrides[commandId]
      set({ overrides })
      save({ presetId: get().presetId, overrides })
    },
    resetAll: () => {
      set({ overrides: {} })
      save({ presetId: get().presetId, overrides: {} })
    },
  }
})

// ---- the global listener hook ----

let _captureMode = false
/** While true, the keymap listener is suspended (the rebind UI is capturing). */
export function setCaptureMode(on: boolean) { _captureMode = on }

// Input types where the user is genuinely typing — keep every key for them.
// (number/date/etc. included: you type + arrow-step values in those.)
const TEXT_INPUT_TYPES = new Set([
  'text', 'search', 'email', 'url', 'password', 'tel', 'number',
  'date', 'time', 'datetime-local', 'month', 'week',
])
// Keys a focused navigable control (slider/select/radio group/tab list/menu)
// needs for itself — arrows step a range slider, Home/End jump it, arrows
// move within a radio group or a menu. Don't hijack those. A PLAIN button or
// checkbox has no arrow behaviour, so Shift+→ and friends still run there
// (review RD3: focus on a toolbar button swallowed them, silently).
const CONTROL_NAV_KEYS = new Set([
  'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End', 'PageUp', 'PageDown',
])
const NAV_INPUT_TYPES = new Set(['range', 'radio'])
const NAV_ROLES = new Set([
  'slider', 'spinbutton', 'radio', 'tab', 'menuitem', 'menuitemradio', 'menuitemcheckbox', 'option',
  'treeitem', 'gridcell', 'listbox', 'scrollbar', 'combobox',
])
// Controls Space (and, where the platform does, Enter) ACTIVATES: a keyboard
// user toggles Play backwards, Keep pitch, Mute, Solo and Snapping with
// Space, as everywhere else (review RD3: Space started playback instead).
const ACTIVATE_INPUT_TYPES = new Set(['checkbox', 'radio', 'button', 'submit', 'reset', 'color', 'file'])
const ACTIVATE_ROLES = new Set([
  'button', 'switch', 'checkbox', 'radio', 'menuitem', 'menuitemcheckbox', 'menuitemradio', 'link', 'option',
])

/** A focused tab's own activation keys (WAI-ARIA APG tabs: Space and Enter
 *  activate the focused tab). */
const TAB_KEYS = new Set(['Space', 'Enter'])
/** Keys a text field keeps even under ⌘: select all, copy, paste, cut, undo /
 *  redo of the TEXT, and the caret/word moves. */
const NATIVE_TEXT_KEYS = new Set([
  'KeyA', 'KeyC', 'KeyV', 'KeyX', 'KeyZ', 'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Backspace', 'Delete',
])

/** What {@link shouldRun} reads from the keydown's target: a DOM element in
 *  the app, a plain object in tests. */
export interface KeyTarget {
  tagName?: string
  type?: string
  isContentEditable?: boolean
  closest?(selector: string): unknown
  getAttribute?(name: string): string | null
}

/** A focused control whose own keys are arrows/Home/End (see NAV_ROLES). */
function ownsNavKeys(t: KeyTarget | null | undefined): boolean {
  const tag = t?.tagName
  if (tag === 'SELECT') return true
  if (tag === 'INPUT') return NAV_INPUT_TYPES.has(t?.type || '')
  const role = t?.getAttribute?.('role')
  return !!role && NAV_ROLES.has(role)
}

/** A control Space activates (a button, a checkbox, a switch…). */
function isActivatable(t: KeyTarget | null | undefined): boolean {
  const tag = t?.tagName
  if (tag === 'INPUT') return ACTIVATE_INPUT_TYPES.has(t?.type || '')
  const role = t?.getAttribute?.('role')
  if (role) return ACTIVATE_ROLES.has(role)
  return tag === 'BUTTON' || tag === 'SUMMARY'
}

/** Elements that took focus from a pointer press (a mouse click leaves
 *  focus on a clicked button or checkbox in Chromium; WebKit does not focus
 *  on click at all). `:focus-visible` cannot tell: the Space keydown itself
 *  turns it on before any listener runs (measured in Chromium). */
const pointerFocused = new WeakSet<object>()
let pointerDownAt = -Infinity
const POINTER_FOCUS_MS = 800

/** Record that `target` took focus from a pointer (the listeners below; a
 *  test calls it on a stand-in). */
export function notePointerFocus(target: object): void {
  pointerFocused.add(target)
}

let pointerTarget: Node | null = null

if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') {
  document.addEventListener('pointerdown', (e) => {
    pointerDownAt = performance.now()
    pointerTarget = e.target instanceof Node ? e.target : null
  }, true)
  // any key press after it: the next focus move is the keyboard's (Tab)
  document.addEventListener('keydown', () => { pointerDownAt = -Infinity }, true)
  document.addEventListener('focusin', (e) => {
    const t = e.target
    if (!(t instanceof Node)) return
    // focus the press itself gave: the pressed element or the control
    // around it (a click on a label's text focuses its checkbox)
    const fromPress = performance.now() - pointerDownAt < POINTER_FOCUS_MS && !!pointerTarget
      && (t === pointerTarget || t.contains(pointerTarget) || pointerTarget.contains(t)
        || (t instanceof HTMLElement && !!pointerTarget.parentElement?.closest('label')?.contains(t)))
    if (fromPress) pointerFocused.add(t)
    else pointerFocused.delete(t)
  }, true)
}

/** Focus the user (or a script acting for them) moved there WITHOUT the
 *  pointer: Space activates such a control; after a mouse click on it Space
 *  still plays (the NLE convention). */
function keyboardFocused(t: KeyTarget | null | undefined): boolean {
  return !(t && pointerFocused.has(t as object))
}

/** A field the user is typing in: it keeps every key. */
export function isTextEntry(t: KeyTarget | null | undefined): boolean {
  const tag = t?.tagName
  return tag === 'TEXTAREA' || !!t?.isContentEditable ||
    (tag === 'INPUT' && TEXT_INPUT_TYPES.has(t?.type || 'text'))
}

/**
 * Whether a command bound to `chord` runs for a keydown on `target`
 * (LEFT_RAIL_SPEC §4.2). The chord is resolved to a command FIRST, because
 * the answer depends on the command's scope:
 *
 * 1. While a modal dialog is open (`modalOpen`, or the target is inside an
 *    `aria-modal` one) nothing runs: the editor behind it is inert, and a
 *    panel switch, F6 or ⌘E must not act behind the modal's back.
 * 2. Text entry keeps every key, except for an `'anywhere'` command (F6),
 *    a `'global'` command on a ⌘ chord (⌘E, ⌥⌘K: they type nothing) that is
 *    not a native text chord (⌘A/C/V/X/Z, ⌘-arrows, ⌘⌫), and a command whose
 *    `alsoInText` region holds the field (⌥9/⌥0 from the Chat box: a keyboard
 *    user can go back to the Inspector without leaving the panel first).
 * 3. A `[data-keymap-ignore]` scope (the AI panel's forms, the media rows,
 *    the Prompt bar…) keeps its keys from `'default'`
 *    commands; `'global'` ones (⌥1…⌥8, ⌘E…) still run there (critique H1).
 *    Per-command scope rather than "the scope swallows only unmodified keys":
 *    ⌘Z inside an AI form must not undo the timeline behind the user's back.
 * 4. A focused NAVIGABLE control (a slider, a select, a radio group, a tab
 *    list, a menu) keeps its navigation keys (arrows step a slider, Home/End
 *    jump it). A plain button or checkbox has none, so arrows still run the
 *    editor's commands there (review RD3). Space still plays from a slider.
 * 4c. A KEYBOARD-focused button, checkbox, switch or radio keeps Space (it
 *    activates the control), and a native button Enter; after a MOUSE click
 *    on it (focus that followed a pointer press) Space still plays, so a click
 *    on a toolbar button never turns Space into "press it again" (RD3).
 * 4b. A `[data-keymap-own="Delete Backspace"]` target keeps exactly the keys
 *    it names, with any modifiers, from every command: a focused curve point
 *    removes itself on Delete, and ripple delete never fires there, while ⌘Z,
 *    Space, J/K/L and N still do (review RD2: an ignore scope silenced them).
 * 5. A focused tab keeps Space and Enter: they activate the tab (APG). This
 *    is what makes Space on the selected rail tab collapse / re-open the tool
 *    panel (§2.4) while every other global shortcut — ⌘Z, J/K/L, N — still
 *    works with focus on the rail (the review RD1 regression an ignore scope
 *    on the rail caused). A mouse click does not focus a rail tab, so after
 *    a click Space still plays.
 */
export function shouldRun(
  scope: CommandScope | undefined,
  chord: string,
  target: KeyTarget | null | undefined,
  ctx: { modalOpen?: boolean; alsoInText?: string } = {},
): boolean {
  if (ctx.modalOpen || target?.closest?.('[aria-modal="true"]')) return false
  const s = scope ?? 'default'
  const key = chord.slice(chord.lastIndexOf('+') + 1)
  if (isTextEntry(target)) {
    if (s === 'anywhere') return true
    if (ctx.alsoInText && target?.closest?.(ctx.alsoInText)) return true
    return s === 'global' && chord.split('+').includes('Mod') && !NATIVE_TEXT_KEYS.has(key)
  }
  if (s === 'default' && target?.closest?.('[data-keymap-ignore]')) return false
  const owner = target?.closest?.('[data-keymap-own]') as KeyTarget | null | undefined
  const owned = owner?.getAttribute?.('data-keymap-own')
  if (owned && owned.split(/\s+/).includes(key)) return false
  if (CONTROL_NAV_KEYS.has(key) && ownsNavKeys(target)) return false
  if (TAB_KEYS.has(chord) && target?.getAttribute?.('role') === 'tab') return false
  // Space on a keyboard-focused button / checkbox / switch activates it; a
  // native button also takes Enter (a checkbox has no Enter of its own).
  if (isActivatable(target) && keyboardFocused(target)) {
    if (chord === 'Space') return false
    if (chord === 'Enter' && !(target?.tagName === 'INPUT' && (target.type === 'checkbox' || target.type === 'radio'))) return false
  }
  return true
}

export function useKeymap() {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (_captureMode) return
      // A held key fires a stream of 'keydown' events (OS auto-repeat) with
      // e.repeat=true from the second one on. Every bound command here is a
      // one-shot action (add a marker, split, nudge, duplicate…), not a
      // hold-to-repeat one — without this guard, a slightly-long press of M
      // appends several markers at the exact same playhead position (issue
      // 25), and the same would apply to any other single-press shortcut.
      if (e.repeat) return
      const chord = chordFromEvent(e)
      if (!chord) return
      const cmdId = useKeymapStore.getState().chordToCommand()[chord]
      if (!cmdId) return
      const cmd = COMMAND_BY_ID[cmdId]
      if (!cmd) return
      // Text fields, ignore scopes, a control's own keys and open modals: the
      // scope rule above. Capture phase + preventDefault below stop the
      // control from also reacting when the command does run (e.g. a button
      // "clicking" on Space).
      const modalOpen = !!document.querySelector('[aria-modal="true"]')
      if (!shouldRun(cmd.scope, chord, e.target as HTMLElement | null, { modalOpen, alsoInText: cmd.alsoInText })) return
      // preventDefault suppresses the host's default for every interceptable
      // chord: page scroll on Space/arrows/Home/End, browser page-zoom on
      // Mod+=/Mod+-, bookmark dialog on Mod+D, history nav on Alt+←/→,
      // select-all on Mod+A, button "click" on Space (capture phase runs
      // before the focused control). Truly reserved combos (⌘Q/⌘W in the
      // mac app, Ctrl+W in browsers) never reach the page at all — no preset
      // binds those.
      e.preventDefault()
      // Stop other page-level handlers from double-acting on a handled chord —
      // EXCEPT Escape: 'deselect' is bound to Escape in every preset, but the
      // app's modal/popover close + drag-cancel handlers (Help,
      // ShortcutsSettings, TransitionPopover, Timeline context menu/drag) are
      // bubble-phase window listeners for the same key and must still see it.
      if (e.code !== 'Escape') e.stopPropagation()
      void cmd.run(useStore.getState())
    }
    // Capture phase so this runs before the focused control's own key handling.
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [])
}
