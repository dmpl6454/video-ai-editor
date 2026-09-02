// Safe-zone overlay for the preview, plus the top-bar control that drives it.
//
// Two kinds of rectangle get drawn over the picture, both in the 0..1 canvas
// space of lib/safeZones.ts:
//   - the chosen platform's chrome (status bar, action rail, caption/nav band)
//     from SAFE_ZONES, hatched so the picture stays readable underneath;
//   - ad-hoc guide rects other panels publish through lib/guideRects.ts (the
//     AI panel's bbox fields for object_erase / motion_track — a typed 0..1
//     box is unusable blind).
//
// The layer is pointer-events:none end to end: StickerLayer beneath it owns
// every click and drag on the picture (stickers, text AND the direct video
// drag), and a guide that swallowed a click would break all three. It sits
// above TextLayer in the DOM so the hatch reads over captions — placing those
// is the whole point — and below CropReposition, which replaces the picture
// entirely while framing. It subscribes only to its two tiny stores, never to
// `playhead`: re-rendering a div stack every frame is exactly the churn the
// canvas layers exist to avoid.
import { memo } from 'react'
import { useStore } from '../store'
import type { Canvas } from '../types'
import { useSafeZones } from '../lib/safeZonesStore'
import { useGuideRects } from '../lib/guideRects'
import {
  SAFE_ZONES, SAFE_ZONE_MODES, NOT_916_HINT,
  isSafeZoneMode, isVertical916, zonesFor, toPx, type SafeZoneMode,
} from '../lib/safeZones'
import './safeZones.css'

interface Props {
  canvas: Canvas
  /** The preview stage's CSS size (Preview.tsx's `boxSize`). */
  width: number
  height: number
}

// Below this width:height a zone is a column (the action rail) and its label
// is written vertically so it fits inside the ~55px-wide strip instead of
// spilling across the picture.
const TALL_ZONE_RATIO = 1.5

export const SafeZones = memo(function SafeZones({ canvas, width, height }: Props) {
  const mode = useSafeZones((s) => s.mode)
  const guides = useGuideRects((s) => s.rects)
  const { zones } = zonesFor(mode, canvas)
  const guideIds = Object.keys(guides)
  if (zones.length === 0 && guideIds.length === 0) return null
  const platform = mode === 'off' || zones.length === 0 ? null : SAFE_ZONES[mode]

  return (
    <div className="safe-zones" aria-hidden="true">
      {platform && zones.map((z) => {
        const box = toPx(z.rect, width, height)
        const tall = box.height > box.width * TALL_ZONE_RATIO
        return (
          <div
            key={z.id}
            className={`safe-zone${tall ? ' is-tall' : ''}`}
            style={{ left: box.left, top: box.top, width: box.width, height: box.height }}
          >
            <span className="safe-zone-label">{platform.label} · {z.label}</span>
          </div>
        )
      })}
      {guideIds.map((id) => {
        const box = toPx(guides[id].rect, width, height)
        return (
          <div
            key={id}
            className="guide-rect"
            style={{ left: box.left, top: box.top, width: box.width, height: box.height }}
          >
            <span className="guide-rect-label">{guides[id].label}</span>
          </div>
        )
      })}
      {platform && (
        <span className="safe-zones-platform">{platform.label} · approx. guides</span>
      )}
    </div>
  )
})

const BASE_TITLE = 'Overlay where TikTok / Reels / Shorts draw their own UI over a 9:16 video, '
  + 'so captions and lower-thirds land clear of it (approximate guides)'

function optionLabel(mode: SafeZoneMode, nonVertical: boolean): string {
  if (mode === 'off') return 'Safe zones: off'
  return `Safe zones: ${SAFE_ZONES[mode].label}${nonVertical ? ' (9:16 only)' : ''}`
}

/**
 * The top-bar control. A NATIVE <select>: `.topbar` is `overflow: hidden`, so
 * a custom popup would be clipped (CaptionsButton has to portal its menu to
 * escape the same rule), and a native one needs no portal. Fully restyled in
 * safeZones.css — styles.css has no `select` rule, and a UA-default control
 * next to the aspect buttons would look like nothing else in the toolbar.
 *
 * Keyboard note: the keymap engine deliberately lets Space through to
 * play/pause even on a focused select (keymap/engine.ts), matching every
 * other toolbar control; Enter and the arrow keys still operate it natively.
 */
export function SafeZoneToggle() {
  const mode = useSafeZones((s) => s.mode)
  const setMode = useSafeZones((s) => s.setMode)
  // Primitive selectors, not `s.edl?.canvas`: the canvas object is replaced on
  // every EDL refresh, which would re-render this control after every edit.
  const canvasW = useStore((s) => s.edl?.canvas.w ?? 0)
  const canvasH = useStore((s) => s.edl?.canvas.h ?? 0)
  const hasCanvas = canvasW > 0 && canvasH > 0
  const nonVertical = hasCanvas && !isVertical916({ w: canvasW, h: canvasH })
  const title = nonVertical ? `${NOT_916_HINT} (approximate guides)` : BASE_TITLE

  return (
    <select
      className={`safe-zone-select${mode !== 'off' ? ' is-on' : ''}`}
      aria-label="Safe zones overlay"
      title={title}
      value={mode}
      onChange={(e) => { if (isSafeZoneMode(e.target.value)) setMode(e.target.value) }}
    >
      {SAFE_ZONE_MODES.map((m) => (
        <option key={m} value={m}>{optionLabel(m, nonVertical)}</option>
      ))}
    </select>
  )
}
