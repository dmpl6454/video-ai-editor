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

/** Human-readable chord, e.g. "⌘B", "Shift+Del", "=". */
export function chordLabel(chord: string): string {
  if (!chord) return ''
  return chord.split('+').map((p) => {
    if (p === 'Mod') return IS_MAC ? '⌘' : 'Ctrl'
    if (p === 'Ctrl') return IS_MAC ? '⌃' : 'Ctrl'   // secondary modifier (mac only)
    if (p === 'Meta') return IS_MAC ? '⌘' : 'Win'    // secondary modifier (win/linux only)
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
