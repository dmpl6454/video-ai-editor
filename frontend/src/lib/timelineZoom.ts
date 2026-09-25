// Timeline zoom: limits, fit, the log slider and cursor-anchored zoom
// (QA-054 / QA-055).
//
// Zoom to fit divided `window.innerWidth − 240` by the duration, which ignores
// the media and Properties panels and the 80 px lane-label column: a 40 s
// project "fit" at 30 px/s put its end at 1280 px in a 928 px view. The zoom
// floor of 10 px/s left a 12-minute timeline 7200 px wide at best. And every
// zoom change let the playhead-follow effect re-centre on the playhead, so
// ⌘-wheel over minute 5 jumped the view back to 0:04.

/** px per second. 0.5 px/s shows an hour in 1800 px; 1200 px/s is 40 px a
 *  frame at 30 fps. */
export const ZOOM_MIN = 0.5
export const ZOOM_MAX = 1200
export const ZOOM_DEFAULT = 80

export function clampZoom(z: number): number {
  if (!Number.isFinite(z)) return ZOOM_DEFAULT
  return Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, z))
}

/** The zoom at which `duration` seconds fill `laneW` px — the timeline's
 *  visible width right of the lane-label column — with a small right margin so
 *  the last clip's edge stays grabbable. */
export function fitZoom(duration: number, laneW: number, marginPx = 24): number {
  if (!(duration > 0)) return ZOOM_DEFAULT
  return clampZoom(Math.max(40, laneW - marginPx) / duration)
}

/** scrollLeft that keeps time `t` under the same viewport x after zooming to
 *  `zoom` — the anchor rule for ⌘-wheel (pointer) and buttons (playhead). */
export function anchoredScroll(t: number, viewX: number, zoom: number, labelWidth: number): number {
  return Math.max(0, labelWidth + t * zoom - viewX)
}

// The slider is logarithmic: a linear 0.5..1200 range would spend 99% of its
// travel above 12 px/s.
const LOG_MIN = Math.log(ZOOM_MIN)
const LOG_SPAN = Math.log(ZOOM_MAX) - LOG_MIN
export const SLIDER_STEPS = 1000

export function zoomToSlider(z: number): number {
  return Math.round(((Math.log(clampZoom(z)) - LOG_MIN) / LOG_SPAN) * SLIDER_STEPS)
}

export function sliderToZoom(v: number): number {
  return clampZoom(Math.exp(LOG_MIN + (Math.max(0, Math.min(SLIDER_STEPS, v)) / SLIDER_STEPS) * LOG_SPAN))
}

/** How much of the timeline one screen shows, for the zoom control's label
 *  ("12 s", "3 min") — a number an editor reads, not a px/s float. */
export function visibleSpanLabel(zoom: number, laneW: number): string {
  const s = Math.max(0, laneW) / clampZoom(zoom)
  if (s < 10) return `${s.toFixed(1)} s`
  if (s < 120) return `${Math.round(s)} s`
  if (s < 7200) return `${Math.round(s / 60)} min`
  return `${(s / 3600).toFixed(1)} h`
}

// The mounted timeline registers its viewport so the keyboard command (Zoom to
// fit, ⌘\) fits the width it really has rather than guessing from the window.
export interface TimelineView {
  laneWidth(): number          // visible px right of the label column
  fitTo(zoom: number): void    // apply a fit zoom and scroll to the start
}
let view: TimelineView | null = null

export function registerTimelineView(v: TimelineView | null): void {
  view = v
}

export function timelineView(): TimelineView | null {
  return view
}
