// Opens Settings from anywhere — the top bar's gear, the `openSettings`
// keymap command (⌘, / Ctrl+,), the brain popover's "Open Settings". The
// dialog registers itself on mount. Its own module so the component file only
// exports components (react-refresh) and the keymap need not import React UI.

let openFn: (() => void) | null = null

export function registerSettingsOpener(fn: (() => void) | null): void { openFn = fn }

/** Open Settings (QA-063-SETTINGS). A no-op until the dialog has mounted. */
export function openSettings(): void { openFn?.() }
