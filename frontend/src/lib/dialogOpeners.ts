// Imperative openers for the shell's dialogs (components/dialogs): each
// dialog registers itself on mount. A separate module so the dialog files
// export only components (react-refresh) and callers need no React UI.
type Opener = (() => void) | null
const reg: Record<string, Opener> = {}
export function registerOpener(name: string, fn: Opener): void { reg[name] = fn }
export function openProjectSettings(): void { reg.projectSettings?.() }
export function openShare(): void { reg.share?.() }
export function openShortcutsDialog(): void { reg.shortcuts?.() }
