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
    region.style.cssText = 'position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%);white-space:nowrap;'
    document.body.appendChild(region)
  }
  region.textContent = text
  if (clearTimer) clearTimeout(clearTimer)
  clearTimer = setTimeout(() => { if (region) region.textContent = '' }, 4000)
}
