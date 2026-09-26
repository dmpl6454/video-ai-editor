// What a wheel / trackpad gesture over the timeline does (QA-117).
//
// The toolbar used to promise "scroll to pan" while a plain wheel scrolled the
// tracks vertically. The rule is now stated in Help (lib/helpShortcuts) and
// decided here, in one place:
//   ⌘/Ctrl + wheel, or a trackpad pinch (which browsers send as Ctrl+wheel):
//                         zoom, anchored on the pointer, proportional to the delta
//   Shift + wheel:        pan the timeline sideways
//   sideways swipe:       pan (the browser's own horizontal scroll)
//   plain wheel:          scroll the tracks up/down — or, when every track
//                         already fits, pan (there is nothing to scroll)

export interface WheelLike {
  deltaX: number; deltaY: number; deltaMode?: number
  shiftKey: boolean; ctrlKey: boolean; metaKey: boolean
}

export type WheelAction =
  | { kind: 'zoom'; factor: number }
  | { kind: 'pan'; dx: number }
  | { kind: 'native' }

const LINE_PX = 16          // deltaMode 1 (Firefox mouse wheels) is in lines
const NOTCH_PX = 100        // one mouse-wheel notch in Chromium/WebKit
const NOTCH_ZOOM = 1.15     // …zooms by this much, as before
const MAX_STEP = 1.5        // one event never zooms more than this

function px(e: WheelLike, d: number): number {
  return e.deltaMode === 1 ? d * LINE_PX : e.deltaMode === 2 ? d * 800 : d
}

export function wheelAction(e: WheelLike, canScrollV: boolean): WheelAction {
  const dx = px(e, e.deltaX)
  const dy = px(e, e.deltaY)
  if (e.ctrlKey || e.metaKey) {
    // Proportional: a mouse notch is 1.15×, a pinch's many small deltas are
    // smooth instead of 1.15× per event (which made pinch jump wildly).
    const f = Math.pow(NOTCH_ZOOM, -dy / NOTCH_PX)
    return { kind: 'zoom', factor: Math.min(MAX_STEP, Math.max(1 / MAX_STEP, f)) }
  }
  // WebKit/Chromium on macOS already turn Shift+wheel into deltaX.
  if (e.shiftKey) return { kind: 'pan', dx: dy || dx }
  if (Math.abs(dx) > Math.abs(dy)) return { kind: 'native' }        // a sideways swipe
  if (canScrollV) return { kind: 'native' }                           // tracks scroll
  return dy ? { kind: 'pan', dx: dy } : { kind: 'native' }
}
