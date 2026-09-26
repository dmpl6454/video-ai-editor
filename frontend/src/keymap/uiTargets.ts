// Commands that do exactly what a visible control does (⌘E = the Export
// button, ⌥⌘K = "Customize keyboard shortcuts", ⌥T = "Add text") press that
// control rather than re-implementing it (LEFT_RAIL_SPEC §4.1: "same path as
// the button"). One click path means the command can never drift from the
// button: the Export trigger's own disabled rule (nothing to export, already
// exporting) and its focus return, the text insert's playhead and default
// span, the dialog registration of ShortcutsSettings — none of it is copied.
// The selectors are the ones the spec keeps stable across the rail phases
// (§3, §8.1): `.topbar-pinned button.primary` (a11y tests), the button named
// "Customize keyboard shortcuts" (top bar, then the rail foot in R3), and the
// first button of `div[data-text-presets]` (TextTool, then the Text panel in
// R2). `el.click()` works on a control inside a hidden panel too.

export const UI_TARGETS = {
  exportDialog: '.topbar-pinned button.primary[aria-haspopup="dialog"]',
  shortcutsDialog: 'button[aria-label="Customize keyboard shortcuts"]',
  addText: '[data-text-presets] > button',
} as const

export type UiTarget = keyof typeof UI_TARGETS

export type PressResult =
  | { kind: 'pressed' }
  /** The control exists but is disabled; `reason` is its tooltip. */
  | { kind: 'disabled'; reason: string }
  | { kind: 'missing' }

/** Click the control behind `target` unless it is disabled. */
export function pressControl(target: UiTarget, doc: Pick<Document, 'querySelector'> = document): PressResult {
  const el = doc.querySelector<HTMLButtonElement>(UI_TARGETS[target])
  if (!el) return { kind: 'missing' }
  if (el.disabled) return { kind: 'disabled', reason: el.title || el.getAttribute('aria-label') || '' }
  el.click()
  return { kind: 'pressed' }
}
