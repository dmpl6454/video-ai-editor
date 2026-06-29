/**
 * Keymap presets modelled on the real defaults of CapCut, Premiere Pro, and
 * Final Cut Pro. A keymap maps a command id → one or more key chords.
 *
 * Chord format is layout-independent, built from KeyboardEvent.code with a
 * "Mod" alias for Cmd (mac) / Ctrl (win):
 *     "Mod+KeyB"  "Shift+Delete"  "Space"  "ArrowLeft"  "BracketLeft"
 *     "Comma"  "Equal"  "KeyI"  "Mod+Shift+KeyZ"
 *
 * Where the three apps genuinely differ (split, marks, zoom, snap, nudge) the
 * presets diverge; the universal NLE conventions (Space, J/K/L, undo) are the
 * same everywhere. Everything is rebindable, so these are just starting points.
 */
export type KeyMap = Record<string, string[]>

export const PRESETS = {
  capcut: {
    label: 'CapCut',
    map: {
      playPause: ['Space'],
      shuttleReverse: ['KeyJ'],
      shuttleStop: ['KeyK'],
      shuttleForward: ['KeyL'],
      frameBack: ['ArrowLeft', 'Comma'],
      frameForward: ['ArrowRight', 'Period'],
      secondBack: ['Shift+ArrowLeft'],
      secondForward: ['Shift+ArrowRight'],
      goToStart: ['Home'],
      goToEnd: ['End'],
      split: ['Mod+KeyB'],
      rippleDelete: ['Delete', 'Backspace'],
      duplicate: ['Mod+KeyD'],
      copy: ['Mod+KeyC'],
      paste: ['Mod+KeyV'],
      markIn: ['BracketLeft'],
      markOut: ['BracketRight'],
      clearMarks: [],
      addMarker: ['KeyM'],
      zoomIn: ['Mod+Equal'],
      zoomOut: ['Mod+Minus'],
      zoomFit: ['Mod+Backslash'],
      toggleSnap: ['KeyN'],
      selectAll: ['Mod+KeyA'],
      deselect: ['Escape'],
      undo: ['Mod+KeyZ'],
      redo: ['Mod+Shift+KeyZ'],
    } as KeyMap,
  },

  premiere: {
    label: 'Premiere Pro',
    map: {
      playPause: ['Space'],
      shuttleReverse: ['KeyJ'],
      shuttleStop: ['KeyK'],
      shuttleForward: ['KeyL'],
      frameBack: ['ArrowLeft'],
      frameForward: ['ArrowRight'],
      secondBack: ['Shift+ArrowLeft'],
      secondForward: ['Shift+ArrowRight'],
      goToStart: ['Home'],
      goToEnd: ['End'],
      split: ['Mod+KeyK'],                  // Add Edit at playhead
      rippleDelete: ['Shift+Delete', 'Delete', 'Backspace'],
      duplicate: ['Mod+KeyD'],
      copy: ['Mod+KeyC'],
      paste: ['Mod+KeyV'],
      nudgeLeft: ['Comma'],
      nudgeRight: ['Period'],
      markIn: ['KeyI'],
      markOut: ['KeyO'],
      clearMarks: ['Mod+Shift+KeyX'],
      addMarker: ['KeyM'],
      zoomIn: ['Equal'],                    // = zoom in (timeline)
      zoomOut: ['Minus'],                   // - zoom out
      zoomFit: ['Backslash'],               // \ zoom to sequence
      toggleSnap: ['KeyS'],                 // S toggles snapping
      selectAll: ['Mod+KeyA'],
      deselect: ['Escape'],
      undo: ['Mod+KeyZ'],
      redo: ['Mod+Shift+KeyZ'],
    } as KeyMap,
  },

  finalcut: {
    label: 'Final Cut Pro',
    map: {
      playPause: ['Space'],
      shuttleReverse: ['KeyJ'],
      shuttleStop: ['KeyK'],
      shuttleForward: ['KeyL'],
      frameBack: ['ArrowLeft'],
      frameForward: ['ArrowRight'],
      secondBack: ['Shift+ArrowLeft'],
      secondForward: ['Shift+ArrowRight'],
      goToStart: ['Home'],
      goToEnd: ['End'],
      split: ['Mod+KeyB'],                  // Blade at playhead
      rippleDelete: ['Delete', 'Backspace'],
      duplicate: ['Mod+KeyD'],
      copy: ['Mod+KeyC'],
      paste: ['Mod+KeyV'],
      nudgeLeft: ['Comma'],                 // FCP nudges with , and .
      nudgeRight: ['Period'],
      markIn: ['KeyI'],
      markOut: ['KeyO'],
      clearMarks: ['Mod+Shift+KeyX'],
      addMarker: ['KeyM'],
      zoomIn: ['Mod+Equal'],                // Cmd+= zoom in
      zoomOut: ['Mod+Minus'],               // Cmd+- zoom out
      zoomFit: ['Shift+KeyZ'],              // Shift+Z zoom to fit
      toggleSnap: ['KeyN'],                 // N toggles snapping
      selectAll: ['Mod+KeyA'],
      deselect: ['Escape'],
      undo: ['Mod+KeyZ'],
      redo: ['Mod+Shift+KeyZ'],
    } as KeyMap,
  },
} as const

export type PresetId = keyof typeof PRESETS
export const PRESET_IDS = Object.keys(PRESETS) as PresetId[]
export const DEFAULT_PRESET: PresetId = 'capcut'
