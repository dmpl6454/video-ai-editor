// CapCut-style crop/reposition view. Mounted by Preview.tsx ON TOP of the
// normal composited preview (video + FrameScrubber + StickerLayer + TextLayer)
// whenever the selected clip is the base v1 media clip AND its `fit` is
// "cover" — i.e. exactly the case a simple "outline" overlay can't serve
// (see below for why).
//
// Why not just draw an outline around the true source bounds? Fill Frame
// scales the source up to cover the canvas, and for a landscape source on a
// portrait canvas (or vice versa) that scale-up can be extreme — 5x+ isn't
// unusual. An outline drawn at that true size lands thousands of pixels off
// the visible preview pane: mathematically correct, visually useless (tried
// this first; it silently drew nothing anyone could see). The reference this
// was built from (a CapCut recording) shows the actual trick: the whole
// uncropped source is shown zoomed OUT, fixed in place, and the CANVAS
// WINDOW itself is what you drag around on top of it — an "X/Y" readout
// tracks the window's own offset, not the picture's. The normal <video> only
// ever contains the FINAL, already-cropped bake, so there is no way to reveal
// "more" of it — the raw source file has to be shown instead, which is why
// this needs its own <video> element rather than reusing the composited
// preview's.
import { useCallback, useEffect, useRef, useState } from 'react'
import { useStore } from '../store'
import type { Clip } from '../types'
import { sessionFileUrl, srcDimsFor } from '../lib/media'
import { cropLayout, dragToOffset } from '../lib/cropLayout'

interface Props {
  clip: Clip
  canvasW: number
  canvasH: number
  sid: string
  paneW: number   // available preview pane, screen px (same box Preview.tsx
  paneH: number   // sizes its normal <video> to)
  playhead: number
}

// Upper bound mirrors Properties.tsx's Transform scale slider so a wheel-zoom
// here can never commit a value that panel would clamp right back.
//
// The LOWER bound is 1, not 0.1, and that is a render contract rather than a
// preference: compositor.py's cover-with-pan branch applies
// `extra_zoom = max(1.0, scale)`. Zooming below 1 would shrink the frame to
// smaller than the canvas, leaving `crop` with less input than its output size
// — which renders solid black, with no ffmpeg error. Offering 0.1 here let the
// view show a zoomed-OUT picture the renderer would never produce, so the
// preview disagreed with the export the moment the value committed.
const SCALE_MIN = 1
const SCALE_MAX = 4

// How much of the pane the crop window is allowed to occupy. Below 1 so the
// croppable footage AROUND the window stays on screen — that surrounding
// context is the whole reason this view exists.
const WINDOW_FILL = 1

export function CropReposition({ clip, canvasW, canvasH, sid, paneW, paneH, playhead }: Props) {
  const dispatch = useStore((s) => s.dispatch)
  const videoRef = useRef<HTMLVideoElement>(null)
  const [dims, setDims] = useState<{ w: number; h: number } | null>(null)
  const [dimsFailed, setDimsFailed] = useState(false)

  // Poll the shared src-dims cache (populated by a throwaway hidden <video>)
  // until it resolves.
  useEffect(() => {
    let cancelled = false
    const check = () => {
      const d = srcDimsFor(clip.src, sid)
      if (cancelled) return
      // 'error' used to be dropped on the floor — it neither set dims nor
      // rescheduled, so `dims` stayed null forever and the guard below painted
      // an opaque black div over the WORKING composited preview, with no
      // message and no way out but deselecting the clip. It fires whenever the
      // browser cannot decode the raw source, which is also a macOS/Windows
      // split: a codec WKWebView decodes may fail in WebView2. Fall back to the
      // normal preview instead of blanking the pane.
      if (d === 'error') { setDimsFailed(true); return }
      if (d === 'loading') { requestAnimationFrame(check); return }
      setDims(d)
    }
    setDims(null)
    setDimsFailed(false)
    check()
    return () => { cancelled = true }
  }, [clip.src, sid])

  const tx = (clip as unknown as
    { transform?: { x?: unknown; y?: unknown; scale?: unknown } }).transform
  const committedX = typeof tx?.x === 'number' ? tx.x : 0
  const committedY = typeof tx?.y === 'number' ? tx.y : 0
  const committedScale = typeof tx?.scale === 'number' && tx.scale > 0 ? tx.scale : 1

  // Live drag/zoom state — plain refs/state, not the global liveTransform
  // channel: this view positions the crop window directly (exact, not an
  // approximation to correct for later), so there's no separate
  // "commit vs. live preview" gap to bridge the way the CSS-translate
  // approximation elsewhere needs.
  const dragRef = useRef<{ startPx: number; startPy: number; x0: number; y0: number } | null>(null)
  const [live, setLive] = useState<{ x: number; y: number } | null>(null)
  const [liveScale, setLiveScale] = useState<number | null>(null)
  const xOff = live?.x ?? committedX
  const yOff = live?.y ?? committedY
  const scale = liveScale ?? committedScale

  // Debounced commit for wheel-zoom (many small events per gesture; commit
  // once the wheel goes quiet, same "commit on release" posture the drag
  // uses, just time-gated instead of pointer-gated since wheel has no
  // discrete up/down to hook).
  const zoomCommitTimer = useRef<number | null>(null)

  // Keep the frozen raw-source frame in sync with the timeline position.
  //
  // `seekToPlayhead` is also wired to the <video>'s onLoadedMetadata, because
  // on FIRST MOUNT this effect always ran against readyState 0 (the element is
  // created in the same commit) and returned without seeking. The view only
  // opens while paused, so none of the deps changed afterwards and the raw
  // source sat at source time 0 forever — a different moment, often a different
  // shot, than the composited preview the user was just looking at. They would
  // then choose a crop against footage that is not at that timeline position.
  const seekToPlayhead = useCallback(() => {
    const v = videoRef.current
    if (!v || v.readyState < 1) return
    const srcTime = Math.max(0, (playhead - clip.start) + clip.in)
    if (Math.abs(v.currentTime - srcTime) > 0.03) {
      try { v.currentTime = srcTime } catch { /* non-fatal */ }
    }
  }, [playhead, clip.start, clip.in])

  useEffect(() => { seekToPlayhead() }, [seekToPlayhead])

  useEffect(() => () => {
    if (zoomCommitTimer.current) window.clearTimeout(zoomCommitTimer.current)
  }, [])

  // Could not read the source's real size: render NOTHING so the composited
  // preview underneath stays visible and usable. Repositioning is unavailable,
  // but a working preview beats an opaque black rectangle over it.
  if (dimsFailed) return null
  if (!dims || paneW <= 0 || paneH <= 0) {
    // Dims still probing — show nothing rather than a wrongly-scaled flash.
    return <div style={{ position: 'absolute', inset: 0, background: '#000' }} />
  }

  // All of the geometry — and every sign convention it has to share with
  // render/compositor.py — lives in cropLayout(), where it is unit-tested.
  // The window is the OUTPUT FRAME: fixed size, fixed position. The picture is
  // what `scale` grows and what x/y moves, following the pointer.
  const { winScale, win, pic, clamped, limit } = cropLayout({
    canvasW, canvasH, srcW: dims.w, srcH: dims.h,
    paneW, paneH, scale, x: xOff, y: yOff, windowFill: WINDOW_FILL,
  })
  const canvasScreenW = win.w
  const canvasScreenH = win.h
  const frameScreenW = pic.w
  const frameScreenH = pic.h
  const rectLeft = win.left
  const rectTop = win.top
  const frameLeft = pic.left
  const frameTop = pic.top

  const url = sessionFileUrl(clip.src, sid)
  const dragging = !!(dragRef.current || live)

  const onPointerDown = (e: React.PointerEvent) => {
    (e.target as Element).setPointerCapture?.(e.pointerId)
    dragRef.current = { startPx: e.clientX, startPy: e.clientY, x0: xOff, y0: yOff }
    setLive({ x: xOff, y: yOff })
  }
  const onPointerMove = (e: React.PointerEvent) => {
    const d = dragRef.current
    if (!d) return
    // Screen px → canvas px through the SAME winScale used to lay everything
    // out, so a 1:1 screen-pixel drag feels direct regardless of zoom level.
    const next = dragToOffset(
      { x: d.x0, y: d.y0 },
      { dx: e.clientX - d.startPx, dy: e.clientY - d.startPy },
      winScale,
    )
    setLive(next)
  }
  const onPointerUp = () => {
    const d = dragRef.current
    dragRef.current = null
    if (!d || !live) { setLive(null); return }
    // Commit the CLAMPED offset, not the raw pointer total. Past the pan margin
    // ffmpeg pins its crop to the edge, so an unclamped value stored a number
    // the bake silently discards — and the next mount would re-read it and draw
    // a position the export does not have.
    const moved = Math.round(clamped.x) !== Math.round(committedX)
      || Math.round(clamped.y) !== Math.round(committedY)
    if (moved) {
      dispatch('set_clip_transform', {
        clip_id: clip.id, x: Math.round(clamped.x), y: Math.round(clamped.y),
      })
    }
    setLive(null)
  }

  const onWheel = (e: React.WheelEvent) => {
    e.preventDefault()
    const base = liveScale ?? committedScale
    // A wheel "click" is ~100 (deltaMode 0, pixel mode) on most mice/trackpads;
    // scale multiplicatively so the zoom feels proportional at any level
    // rather than a fixed additive step that's huge at 0.2x and tiny at 4x.
    const factor = Math.exp(-e.deltaY * 0.001)
    const next = Math.min(SCALE_MAX, Math.max(SCALE_MIN, base * factor))
    setLiveScale(next)
    if (zoomCommitTimer.current) window.clearTimeout(zoomCommitTimer.current)
    zoomCommitTimer.current = window.setTimeout(() => {
      if (Math.abs(next - committedScale) > 0.001) {
        dispatch('set_clip_transform', { clip_id: clip.id, scale: Math.round(next * 100) / 100 })
      }
      setLiveScale(null)
    }, 250)
  }

  return (
    <div
      style={{ position: 'absolute', inset: 0, background: '#000', overflow: 'hidden', cursor: 'move' }}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
      onWheel={onWheel}
    >
      {url && (
        <video
          ref={videoRef}
          src={url}
          className="crop-source"
          onLoadedMetadata={seekToPlayhead}
          onLoadedData={seekToPlayhead}
          muted
          playsInline
          preload="auto"
          style={{
            position: 'absolute',
            left: frameLeft, top: frameTop, width: frameScreenW, height: frameScreenH,
            pointerEvents: 'none',
          }}
        />
      )}
      {/* Canvas-bounds window: the box-shadow trick dims everything OUTSIDE
          this element without needing a separate overlay/mask element — a
          shadow with a huge spread and no blur fills the rest of the
          viewport, punched through only where this rect itself sits. */}
      <div
        style={{
          position: 'absolute',
          left: rectLeft, top: rectTop, width: canvasScreenW, height: canvasScreenH,
          border: '2px solid #ffd400',
          boxShadow: '0 0 0 9999px rgba(0,0,0,0.65)',
          pointerEvents: 'none',
        }}
      >
        {/* Centre crosshair — a precision-alignment guide, same idea as the
            reference's centre guide lines. */}
        <div style={{ position: 'absolute', left: '50%', top: 0, bottom: 0, width: 1, background: 'rgba(255,212,0,0.6)' }} />
        <div style={{ position: 'absolute', top: '50%', left: 0, right: 0, height: 1, background: 'rgba(255,212,0,0.6)' }} />
      </div>
      {/* Live X/Y (while dragging) or zoom (while wheel-zooming) readout,
          matching the reference's on-screen badge. */}
      {(dragging || liveScale !== null) && (
        <div style={{
          position: 'absolute', left: rectLeft, top: rectTop + canvasScreenH + 6,
          background: 'rgba(0,0,0,0.7)', color: '#ffd400', fontSize: 12,
          padding: '2px 6px', borderRadius: 4, pointerEvents: 'none',
        }}>
          {/* The CLAMPED values, so the badge never reports a pan the render
              will discard. When an axis has no margin at all (a source whose
              aspect matches the canvas has none until you zoom in), say so
              rather than printing a number that does nothing. */}
          {dragging
            ? (limit.x < 1 && limit.y < 1
              ? 'Zoom in to reposition'
              : `X${Math.round(clamped.x)} Y${Math.round(clamped.y)}`)
            : `${scale.toFixed(2)}×`}
        </div>
      )}
    </div>
  )
}
