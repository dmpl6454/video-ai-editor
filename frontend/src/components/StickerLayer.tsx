// Interactive sticker overlay. Draws every active sticker on a transparent
// canvas stacked over the <video>, and makes them directly manipulable:
//   • click a sticker to select it (→ Properties panel)
//   • drag the body to move (commits x/y)
//   • drag a corner handle to resize (commits scale)
// Live feedback is drawn locally during the gesture; the server is hit ONCE on
// pointer-up via set_clip_transform — same commit-on-release pattern as the
// other transform controls.

import { useEffect, useRef } from 'react'
import { useStore } from '../store'
import type { EDL } from '../types'
import { isSticker, stickerGeom, type StickerClip, type StickerGeom } from '../lib/overlay'
import * as dv from '../lib/dragVisuals'

interface Props {
  edl: EDL
  videoEl: HTMLVideoElement | null
  width: number
  height: number
}

const HANDLE = 7          // half-size of a corner handle, display px
const HANDLE_HIT = 13     // click tolerance around a handle

type Drag =
  | { id: string; mode: 'move'; startMx: number; startMy: number; x0: number; y0: number; live: { x: number; y: number } }
  | { id: string; mode: 'resize'; cx: number; cy: number; startDist: number; scale0: number; live: { scale: number } }

export function StickerLayer({ edl, videoEl, width, height }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const selection = useStore((s) => s.selection)
  const setSelection = useStore((s) => s.setSelection)
  const dispatch = useStore((s) => s.dispatch)

  // Keep the latest reactive values in a ref so the rAF loop + event handlers
  // (registered once) always read fresh state without re-binding.
  const stateRef = useRef({ edl, width, height, selection })
  stateRef.current = { edl, width, height, selection }
  const dragRef = useRef<Drag | null>(null)

  useEffect(() => {
    const cv = canvasRef.current
    if (!cv) return
    const ctx = cv.getContext('2d')!

    const now = () => (videoEl ? videoEl.currentTime : useStore.getState().playhead)

    // All stickers active at time t, top-most (last drawn) last.
    const activeStickers = (t: number): StickerClip[] => {
      const out: StickerClip[] = []
      for (const tk of stateRef.current.edl.tracks) {
        if (tk.type !== 'sticker') continue
        for (const c of tk.clips) if (isSticker(c) && c.start <= t && t <= c.end) out.push(c)
      }
      return out
    }

    const geomFor = (sk: StickerClip, t: number): StickerGeom => {
      const { edl, width, height } = stateRef.current
      const d = dragRef.current
      const ov =
        d && d.id === sk.id
          ? d.mode === 'move'
            ? { x: d.live.x, y: d.live.y }
            : { scale: d.live.scale }
          : undefined
      return stickerGeom(sk, t, edl.canvas.w, edl.canvas.h, width, height, ov)
    }

    // Mouse point → the sticker's local (unrotated, centered) frame.
    const toLocal = (px: number, py: number, g: StickerGeom) => {
      const dx = px - g.cx, dy = py - g.cy
      const cos = Math.cos(-g.rot), sin = Math.sin(-g.rot)
      return { lx: dx * cos - dy * sin, ly: dx * sin + dy * cos }
    }

    let raf = 0
    const draw = () => {
      const dpr = window.devicePixelRatio || 1
      const { width, height, selection } = stateRef.current
      if (cv.width !== Math.round(width * dpr) || cv.height !== Math.round(height * dpr)) {
        cv.width = Math.max(1, Math.round(width * dpr))
        cv.height = Math.max(1, Math.round(height * dpr))
        cv.style.width = `${width}px`
        cv.style.height = `${height}px`
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      ctx.clearRect(0, 0, width, height)
      const t = now()
      const stickers = activeStickers(t)
      // Only intercept clicks when stickers are present at the playhead.
      cv.style.pointerEvents = stickers.length ? 'auto' : 'none'

      for (const sk of stickers) {
        const g = geomFor(sk, t)
        ctx.save()
        ctx.translate(g.cx, g.cy)
        ctx.rotate(g.rot)
        ctx.globalAlpha = g.opa
        if (sk.label) {
          ctx.font = `${g.size}px "Apple Color Emoji", "Segoe UI Emoji", "Noto Color Emoji", sans-serif`
          ctx.textBaseline = 'middle'
          ctx.textAlign = 'center'
          ctx.fillText(sk.label, 0, 0)
        } else {
          ctx.fillStyle = 'rgba(255,255,255,0.6)'
          ctx.beginPath()
          ctx.arc(0, 0, g.size / 2, 0, Math.PI * 2)
          ctx.fill()
        }
        ctx.restore()

        if (sk.id === selection) {
          const h = g.size / 2
          const isDragging = dragRef.current?.id === sk.id
          ctx.save()
          ctx.translate(g.cx, g.cy)
          ctx.rotate(g.rot)
          ctx.globalAlpha = 1
          ctx.strokeStyle = dv.ACCENT
          if (isDragging) {
            // Solid, thicker box + soft shadow while actively dragging/resizing
            // — visually distinct from the resting dashed selection box.
            ctx.lineWidth = dv.DRAG_BORDER_W
            ctx.setLineDash([])
            ctx.shadowColor = 'rgba(0,0,0,0.5)'
            ctx.shadowBlur = 8
          } else {
            ctx.lineWidth = 1.5
            ctx.setLineDash([4, 3])
          }
          ctx.strokeRect(-h, -h, g.size, g.size)
          ctx.setLineDash([])
          ctx.shadowBlur = 0
          ctx.fillStyle = dv.ACCENT
          for (const [sx, sy] of [[-1, -1], [1, -1], [1, 1], [-1, 1]] as const) {
            // Highlight the corner being resized (brighter/larger).
            const active = isDragging && dragRef.current?.mode === 'resize'
            const pad = active ? HANDLE + 1 : HANDLE
            ctx.fillRect(sx * h - pad, sy * h - pad, pad * 2, pad * 2)
          }
          ctx.restore()
        }
      }
      raf = requestAnimationFrame(draw)
    }
    draw()

    const posOf = (e: PointerEvent) => {
      const r = cv.getBoundingClientRect()
      return { px: e.clientX - r.left, py: e.clientY - r.top }
    }

    const onDown = (e: PointerEvent) => {
      const { px, py } = posOf(e)
      const t = now()
      const stickers = activeStickers(t)
      const sel = stateRef.current.selection

      // 1) Resize: a corner handle of the currently-selected sticker.
      const selSk = stickers.find((s) => s.id === sel)
      if (selSk) {
        const g = geomFor(selSk, t)
        const { lx, ly } = toLocal(px, py, g)
        const h = g.size / 2
        const onHandle = [[-h, -h], [h, -h], [h, h], [-h, h]].some(
          ([hx, hy]) => Math.hypot(lx - hx, ly - hy) <= HANDLE_HIT,
        )
        if (onHandle) {
          e.preventDefault()
          try { cv.setPointerCapture(e.pointerId) } catch { /* synthetic/edge pointer */ }
          dragRef.current = {
            id: selSk.id, mode: 'resize', cx: g.cx, cy: g.cy,
            startDist: Math.max(4, Math.hypot(px - g.cx, py - g.cy)),
            scale0: g.scale, live: { scale: g.scale },
          }
          return
        }
      }

      // 2) Body hit (top-most first) → select + start move.
      for (let i = stickers.length - 1; i >= 0; i--) {
        const sk = stickers[i]
        const g = geomFor(sk, t)
        const { lx, ly } = toLocal(px, py, g)
        const h = g.size / 2
        if (Math.abs(lx) <= h && Math.abs(ly) <= h) {
          e.preventDefault()
          if (sk.id !== sel) setSelection(sk.id)
          try { cv.setPointerCapture(e.pointerId) } catch { /* synthetic/edge pointer */ }
          dragRef.current = {
            id: sk.id, mode: 'move', startMx: px, startMy: py,
            x0: g.x, y0: g.y, live: { x: g.x, y: g.y },
          }
          return
        }
      }

      // 3) Empty space → deselect.
      if (sel) setSelection(null)
    }

    const onMove = (e: PointerEvent) => {
      const d = dragRef.current
      if (!d) {
        // Hover cursor feedback: resize cursor over a selected sticker's corner
        // handle, move cursor over any sticker body, default otherwise.
        const { px, py } = posOf(e)
        const t = now()
        const sel = stateRef.current.selection
        const stickers = activeStickers(t)
        let cursor = 'default'
        const selSk = stickers.find((s) => s.id === sel)
        if (selSk) {
          const g = geomFor(selSk, t)
          const { lx, ly } = toLocal(px, py, g)
          const h = g.size / 2
          for (const [sx, sy] of [[-1, -1], [1, -1], [1, 1], [-1, 1]] as const) {
            if (Math.hypot(lx - sx * h, ly - sy * h) <= HANDLE_HIT) {
              cursor = dv.cursorForCorner(sx, sy)
              break
            }
          }
        }
        if (cursor === 'default') {
          const overBody = stickers.some((sk) => {
            const g = geomFor(sk, t)
            const { lx, ly } = toLocal(px, py, g)
            return Math.abs(lx) <= g.size / 2 && Math.abs(ly) <= g.size / 2
          })
          if (overBody) cursor = 'move'
        }
        cv.style.cursor = cursor
        return
      }
      const { px, py } = posOf(e)
      const { edl, width, height } = stateRef.current
      if (d.mode === 'move') {
        d.live.x = d.x0 + (px - d.startMx) * (edl.canvas.w / width)
        d.live.y = d.y0 + (py - d.startMy) * (edl.canvas.h / height)
      } else {
        const dist = Math.hypot(px - d.cx, py - d.cy)
        d.live.scale = Math.max(0.1, Math.min(8, d.scale0 * (dist / d.startDist)))
      }
    }

    const onUp = (e: PointerEvent) => {
      const d = dragRef.current
      if (!d) return
      try { cv.releasePointerCapture(e.pointerId) } catch { /* noop */ }
      dragRef.current = null
      if (d.mode === 'move') {
        dispatch('set_clip_transform', { clip_id: d.id, x: Math.round(d.live.x), y: Math.round(d.live.y) })
      } else {
        dispatch('set_clip_transform', { clip_id: d.id, scale: Math.round(d.live.scale * 100) / 100 })
      }
    }

    cv.addEventListener('pointerdown', onDown)
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    return () => {
      cancelAnimationFrame(raf)
      cv.removeEventListener('pointerdown', onDown)
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
    }
  }, [videoEl, dispatch, setSelection])

  return <canvas ref={canvasRef} style={{ position: 'absolute', inset: 0 }} />
}
