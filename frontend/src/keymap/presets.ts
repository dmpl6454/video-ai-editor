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

/**
 * The rail, panel and region chords (LEFT_RAIL_SPEC §4.1), the SAME in every
 * preset. ⌥1…⌥8 follow the rail's order (§2.2). The panel commands are made
 * from the rail's own list (commands.ts), so a chord for an item the rail
 * does not show has no command: it does nothing and is listed nowhere.
 * Collision check: CapCut's ⌘\ and Premiere's \ (zoom to fit) are other
 * chords than ⌥\; ⌘K (focus the Prompt / Premiere's Add Edit) is not ⌥⌘K;
 * the only other Alt chords are ⌥[ ⌥] and ⌥← ⌥→.
 * Matching is by KeyboardEvent.code, so ⌥1 works on every layout even though
 * it types "¡" in a text field (where the engine leaves it to typing).
 */
export const PANEL_KEYS: KeyMap = {
  panelMedia: ['Alt+Digit1'],
  panelAudio: ['Alt+Digit2'],
  panelText: ['Alt+Digit3'],
  panelStickers: ['Alt+Digit4'],
  panelEffects: ['Alt+Digit5'],
  panelTransitions: ['Alt+Digit6'],
  panelCaptions: ['Alt+Digit7'],
  panelAI: ['Alt+Digit8'],
  toggleToolPanel: ['Alt+Backslash'],
  showInspector: ['Alt+Digit9'],
  showChat: ['Alt+Digit0'],
  openShortcuts: ['Mod+Alt+KeyK'],   // Premiere's Keyboard Shortcuts chord
  exportVideo: ['Mod+KeyE'],         // CapCut and Final Cut export
  addText: ['Alt+KeyT'],
  cycleRegion: ['F6'],
  cycleRegionBack: ['Shift+F6'],
}

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
      // KeyS is free in this preset (only Premiere uses it, for toggleSnap),
      // so single-key S joins ⌘B as a split binding — the Help panel
      // advertised "S = split" while nothing actually bound it.
      split: ['Mod+KeyB', 'KeyS'],
      rippleDelete: ['Delete', 'Backspace'],
      // Main track is magnetic in CapCut, so plain Delete closes up; the lift
      // (gap-leaving delete) sits on Shift like Final Cut's (QA-115).
      lift: ['Shift+Delete', 'Shift+Backspace'],
      // CapCut's Q/W ("delete left/right of the playhead"); ⌥[ ⌥] as in
      // Final Cut, since [ and ] alone are the in/out marks here.
      trimStartToPlayhead: ['KeyQ', 'Alt+BracketLeft'],
      trimEndToPlayhead: ['KeyW', 'Alt+BracketRight'],
      duplicate: ['Mod+KeyD'],
      copy: ['Mod+KeyC'],
      paste: ['Mod+KeyV'],
      // Comma/Period are already frame-step here (CapCut's own convention),
      // unlike Premiere/FCP where they're free for nudge — so nudge gets its
      // own chord instead of silently having no binding at all (a registered
      // command with zero keys in the default preset is dead on arrival;
      // issue 55, "many shortcuts work but not completely").
      nudgeLeft: ['Alt+ArrowLeft'],
      nudgeRight: ['Alt+ArrowRight'],
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
      // `/` (the universal "go to the command line") and ⌘K (the universal
      // "command palette") both land in the Prompt bar. Neither key is bound
      // elsewhere in this preset; `?` (Shift+Slash) is a different chord and
      // still opens Help.
      focusPrompt: ['Slash', 'Mod+KeyK'],
      openSettings: ['Mod+Comma'],
      ...PANEL_KEYS,
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
      // Premiere's Delete is Clear (a lift: the gap stays); Shift+Delete is
      // Ripple Delete (QA-115 — Delete used to ripple here).
      rippleDelete: ['Shift+Delete', 'Shift+Backspace'],
      lift: ['Delete', 'Backspace'],
      // Q / W: ripple trim previous / next edit to the playhead.
      trimStartToPlayhead: ['KeyQ'],
      trimEndToPlayhead: ['KeyW'],
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
      // Only `/` here: ⌘K is Premiere's own Add Edit (split) above, and the
      // engine's chord map is last-write-wins, so binding it twice would
      // silently steal split. Rebind in Settings if you want ⌘K anyway.
      focusPrompt: ['Slash'],
      openSettings: ['Mod+Comma'],
      ...PANEL_KEYS,
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
      lift: ['Shift+Delete', 'Shift+Backspace'],   // Replace with gap
      trimStartToPlayhead: ['Alt+BracketLeft'],    // Trim Start
      trimEndToPlayhead: ['Alt+BracketRight'],     // Trim End
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
      focusPrompt: ['Slash', 'Mod+KeyK'],
      openSettings: ['Mod+Comma'],
      ...PANEL_KEYS,
    } as KeyMap,
  },
} as const

export type PresetId = keyof typeof PRESETS
export const PRESET_IDS = Object.keys(PRESETS) as PresetId[]
export const DEFAULT_PRESET: PresetId = 'capcut'
