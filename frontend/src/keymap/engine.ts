import { useEffect } from 'react'
import { create } from 'zustand'
import { useStore } from '../store'
import { COMMAND_BY_ID } from './commands'
import { PRESETS, DEFAULT_PRESET, type KeyMap, type PresetId } from './presets'

/**
 * Keymap engine: turns a KeyboardEvent into a normalised chord, resolves it
 * against the active preset (+ user overrides), and runs the matching command.
 * Preset choice and per-command overrides persist to localStorage.
 */

const IS_MAC = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform)

// ---- chord normalisation (layout-independent, from KeyboardEvent.code) ----

export function chordFromEvent(e: KeyboardEvent): string {
  const parts: string[] = []
  if (e.metaKey || e.ctrlKey) parts.push('Mod')
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

/** Human-readable chord, e.g. "⌘B", "Shift+Del", "=". */
export function chordLabel(chord: string): string {
  if (!chord) return ''
  return chord.split('+').map((p) => {
    if (p === 'Mod') return IS_MAC ? '⌘' : 'Ctrl'
    if (p === 'Alt') return IS_MAC ? '⌥' : 'Alt'
    if (p === 'Shift') return IS_MAC ? '⇧' : 'Shift'
    if (p.startsWith('Key')) return p.slice(3)
    if (p.startsWith('Digit')) return p.slice(5)
    return KEY_LABELS[p] ?? p
  }).join(IS_MAC ? '' : '+')
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
// Keys a focused non-text control (slider/checkbox/select) needs for itself —
// arrows step a range slider, Home/End jump it. Don't hijack those.
const CONTROL_NAV_KEYS = new Set([
  'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End', 'PageUp', 'PageDown',
])

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
      const tgt = e.target as HTMLElement | null
      const tag = tgt?.tagName
      const isTextEntry =
        tag === 'TEXTAREA' ||
        !!tgt?.isContentEditable ||
        (tag === 'INPUT' && TEXT_INPUT_TYPES.has((tgt as HTMLInputElement).type || 'text'))
      // Genuine text fields keep every key for typing.
      if (isTextEntry) return

      // A focused non-text control (range slider, checkbox, button, select)
      // still keeps its own navigation keys, but global shortcuts — above all
      // Space → play/pause — must win so a quick slider tweak doesn't swallow
      // them. Capture phase + preventDefault below stop the control from also
      // reacting (e.g. a button "clicking" on Space).
      const onFormControl =
        tag === 'INPUT' || tag === 'BUTTON' || tag === 'SELECT'
      if (onFormControl && CONTROL_NAV_KEYS.has(e.code)) return

      const chord = chordFromEvent(e)
      if (!chord) return
      const cmdId = useKeymapStore.getState().chordToCommand()[chord]
      if (!cmdId) return
      const cmd = COMMAND_BY_ID[cmdId]
      if (!cmd) return
      e.preventDefault()
      void cmd.run(useStore.getState())
    }
    // Capture phase so this runs before the focused control's own key handling.
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [])
}
