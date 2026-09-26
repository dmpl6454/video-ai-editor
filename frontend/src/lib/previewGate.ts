// Filmstrip thumbnails wait for the preview (QA-059 remainder).
//
// On first open every thumbnail request raced the preview <video> for the
// engine and the browser's connections; measured cold, the player reached
// readyState 2 at 40 s instead of 3 s. The gate opens when a <video> in the
// page has loaded data (or failed — no reason to wait then), or after
// `timeoutMs` so a preview that never arrives cannot hide the filmstrip.

export interface GateDoc {
  addEventListener(type: string, fn: (e: Event) => void, capture: boolean): void
  removeEventListener(type: string, fn: (e: Event) => void, capture: boolean): void
  querySelectorAll?(sel: string): ArrayLike<{ readyState: number }>
}

export interface PreviewGate { isOpen(): boolean; dispose(): void }

export const PREVIEW_GATE_TIMEOUT_MS = 6000

export function createPreviewGate(opts: {
  doc: GateDoc
  onOpen: () => void
  timeoutMs?: number
  setTimer?: (fn: () => void, ms: number) => unknown
  clearTimer?: (h: unknown) => void
}): PreviewGate {
  const setTimer = opts.setTimer ?? ((fn, ms) => setTimeout(fn, ms))
  const clearTimer = opts.clearTimer ?? ((h) => clearTimeout(h as ReturnType<typeof setTimeout>))
  let open = false
  let timer: unknown = null
  const onMedia = (e: Event) => {
    const t = e.target as { tagName?: string } | null
    if (t?.tagName === 'VIDEO') openNow()
  }
  const detach = () => {
    opts.doc.removeEventListener('loadeddata', onMedia, true)
    opts.doc.removeEventListener('error', onMedia, true)
    if (timer !== null) { clearTimer(timer); timer = null }
  }
  function openNow() {
    if (open) return
    open = true
    detach()
    opts.onOpen()
  }
  // A preview that already loaded before the timeline mounted (a remount).
  const vids = opts.doc.querySelectorAll?.('video')
  if (vids && Array.from(vids).some((v) => v.readyState >= 2)) {
    open = true
    return { isOpen: () => true, dispose: () => {} }
  }
  opts.doc.addEventListener('loadeddata', onMedia, true)
  opts.doc.addEventListener('error', onMedia, true)
  timer = setTimer(openNow, opts.timeoutMs ?? PREVIEW_GATE_TIMEOUT_MS)
  return { isOpen: () => open, dispose: detach }
}
