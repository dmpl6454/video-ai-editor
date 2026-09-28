// The viewer's "Reduce motion" setting, LIVE (review RE: the Animation
// section's looping preset previews read it once, so switching the OS
// setting on while the Inspector was open left 11 previews running).
// `useSyncExternalStore` over the media query's `change` event: a switch
// re-renders every subscriber, and a Tile's effect then cancels its loop.
import { useSyncExternalStore } from 'react'

const QUERY = '(prefers-reduced-motion: reduce)'

function mql(): MediaQueryList | null {
  try {
    return typeof window !== 'undefined' && typeof window.matchMedia === 'function' ? window.matchMedia(QUERY) : null
  } catch {
    return null
  }
}

export function prefersReducedMotion(): boolean {
  return mql()?.matches ?? false
}

function subscribe(onChange: () => void): () => void {
  const m = mql()
  if (!m) return () => {}
  // Safari < 14 has only addListener
  if (typeof m.addEventListener === 'function') {
    m.addEventListener('change', onChange)
    return () => m.removeEventListener('change', onChange)
  }
  m.addListener(onChange)
  return () => m.removeListener(onChange)
}

export function useReducedMotion(): boolean {
  return useSyncExternalStore(subscribe, prefersReducedMotion, () => false)
}
