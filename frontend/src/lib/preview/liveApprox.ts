// Reasons the LIVE overlay draw (StickerLayer: PiPs and their blend modes)
// is an approximation of the export on the frame on screen (review RE): a
// blend mode this browser draws differently (lib/canvasBlend LIVE_BLEND_FLAGS
// — `blend:<mode>`). ClientPreview's "≈" chip joins them to the engine's
// own APPROX reasons. A tiny external store: StickerLayer publishes only
// when the set changes.
import { useSyncExternalStore } from 'react'

let current: readonly string[] = []
const listeners = new Set<() => void>()

export function publishLiveApprox(reasons: readonly string[]): void {
  const next = [...new Set(reasons)].sort()
  if (next.length === current.length && next.every((r, i) => r === current[i])) return
  current = next
  for (const l of [...listeners]) l()
}

export function liveApproxReasons(): readonly string[] {
  return current
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn)
  return () => { listeners.delete(fn) }
}

export function useLiveApprox(): readonly string[] {
  return useSyncExternalStore(subscribe, liveApproxReasons, liveApproxReasons)
}
