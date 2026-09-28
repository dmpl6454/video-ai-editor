// One polite live region for short spoken confirmations that have no visible
// text of their own (a keyboard selection: "Selected intro.mp4"). Created on
// first use; the text is replaced, then cleared, so the same words twice in
// a row are read twice.
let region: HTMLElement | null = null
let clearTimer: ReturnType<typeof setTimeout> | null = null

export function announce(text: string): void {
  if (typeof document === 'undefined') return
  if (!region || !region.isConnected) {
    region = document.createElement('div')
    region.setAttribute('role', 'status')
    region.setAttribute('aria-live', 'polite')
    region.setAttribute('aria-atomic', 'true')
    region.dataset.announcer = ''
    // Fixed at the viewport's corner: an absolute box with no top/left sat
    // at its static position below the shell and made the page 1 px taller.
    region.style.cssText = 'position:fixed;top:0;left:0;width:1px;height:1px;overflow:hidden;clip-path:inset(50%);white-space:nowrap;'
    document.body.appendChild(region)
  }
  region.textContent = text
  if (clearTimer) clearTimeout(clearTimer)
  clearTimer = setTimeout(() => { if (region) region.textContent = '' }, 4000)
}
