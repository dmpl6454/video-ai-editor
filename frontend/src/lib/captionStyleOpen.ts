// Opens the Caption style panel from anywhere (the CC ▾ menu, a caption cue's
// inspector) — the panel registers itself on mount. Its own module so the
// component file only exports components (react-refresh).

let openFn: (() => void) | null = null

export function registerCaptionStyleOpener(fn: (() => void) | null): void { openFn = fn }

/** Open the Caption style panel (QA-075). */
export function openCaptionStyle(): void { openFn?.() }
