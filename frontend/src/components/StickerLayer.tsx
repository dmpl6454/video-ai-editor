// Interactive sticker overlay. Draws selection/drag chrome on a transparent
// canvas stacked over the <video>, and makes stickers directly manipulable:
//   • click a sticker to select it (→ Properties panel)
//   • drag the body to move (commits x/y)
//   • drag a corner handle to resize (commits scale)
//   • click the ✕ handle above the top-right corner to delete
// Live feedback is drawn locally during the gesture; the server is hit ONCE on
// pointer-up via set_clip_transform — same commit-on-release pattern as the
// other transform controls.
//
// PIXEL-OWNERSHIP RULE (issue 12): the server bakes EVERY sticker into the
// preview render, even with preview=True (text_overlay.py build_overlay_chain
// skips only TEXT clips in preview — its docstring says so explicitly). So
// when idle, the <video> underneath already shows the sticker pixels and this
// layer must draw NOTHING on top — the old code drew the emoji glyph (Apple
// Color Emoji over the baked Twemoji: a double-draw) or, for label-less PNG
// stickers, a translucent white circle OVER the perfectly-correct baked
// sticker (the "white circle covers my PNG" bug). We only paint the sticker's
// image/glyph WHILE it is being dragged/resized, as live feedback at the new
// position — the baked copy is stale (pre-drag) for that window, and the
// solid drag box visually supersedes it. Selection chrome always draws.

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
const DEL_R = 9           // radius of the ✕ delete handle, display px
const DEL_GAP = 14        // gap between box corner and the delete handle center

// Cache of sticker images for live drag feedback, keyed by the EDL `src`
// (server-absolute path). Values: HTMLImageElement once decoded, 'loading'
// while in flight, 'error' after a failed load (→ translucent-circle
// fallback during drags only).
const IMG_CACHE = new Map<string, HTMLImageElement | 'loading' | 'error'>()

// Server src path → session file URL. Sticker uploads land under
// <session>/uploads/stickers/<name> (main.py sticker_upload) and
// serve_session_file streams /api/sessions/{sid}/files/uploads/<subpath>
// (the `name` segment may include subdirs; there is also an rglob-by-name
// fallback one level deeper). NOTE: /thumb is deliberately NOT used — it
// re-encodes to JPEG, which drops the alpha channel a PNG sticker needs.
function stickerUrl(src: string, sid: string): string | null {
  const norm = src.replace(/\\/g, '/')
  const i = norm.indexOf('/uploads/')
  const name = i >= 0 ? norm.slice(i + '/uploads/'.length) : norm.split('/').pop()
  if (!name) return null
  const encoded = name.split('/').map(encodeURIComponent).join('/')
  return `/api/sessions/${encodeURIComponent(sid)}/files/uploads/${encoded}`
}

function imageFor(sk: StickerClip, sid: string | null): HTMLImageElement | 'loading' | 'error' {
  const cached = IMG_CACHE.get(sk.src)
  if (cached) return cached
  if (!sid) return 'error'
  const url = stickerUrl(sk.src, sid)
  if (!url) {
    IMG_CACHE.set(sk.src, 'error')
    return 'error'
  }
  IMG_CACHE.set(sk.src, 'loading')
  const img = new Image()
  img.onload = () => IMG_CACHE.set(sk.src, img)
  img.onerror = () => IMG_CACHE.set(sk.src, 'error')
  img.src = url
  return 'loading'
}

type Drag =
  | { id: string; mode: 'move'; startMx: number; startMy: number; x0: number; y0: number; live: { x: number; y: number } }
  | { id: string; mode: 'resize'; cx: number; cy: number; startDist: number; scale0: number; live: { scale: number } }

export function StickerLayer({ edl, videoEl, width, height }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const selection = useStore((s) => s.selection)
  const setSelection = useStore((s) => s.setSelection)
  const dispatch = useStore((s) => s.dispatch)
  const sessionId = useStore((s) => s.sessionId)

  // Keep the latest reactive values in a ref so the rAF loop + event handlers
  // (registered once) always read fresh state without re-binding.
  const stateRef = useRef({ edl, width, height, selection, sessionId })
  stateRef.current = { edl, width, height, selection, sessionId }
  const dragRef = useRef<Drag | null>(null)

  useEffect(() => {
    const cv = canvasRef.current
    if (!cv) return
    const ctx = cv.getContext('2d')!

    const now = () => (videoEl ? videoEl.currentTime : useStore.getState().playhead)

    // All stickers active at time t, top-most (last drawn / hit first) last.
    // Sorted (track_z, clip_z, start) — identical to the server's compositing
    // order (text_overlay.py: collect_stickers sorts (clip_z, start), then
    // build_overlay_chain stable-sorts items by (track_z, clip_z)) — so the
    // click-through / hit-test order always matches what's visually on top.
    // Raw clip-array order used to be a THIRD, unsynchronized ordering.
    const activeStickers = (t: number): StickerClip[] => {
      const out: { sk: StickerClip; tz: number; cz: number }[] = []
      for (const tk of stateRef.current.edl.tracks) {
        if (tk.type !== 'sticker') continue
        const tz = (tk as unknown as { z?: number }).z ?? 0
        for (const c of tk.clips) {
          if (isSticker(c) && c.start <= t && t <= c.end) {
            out.push({ sk: c, tz, cz: (c as unknown as { z?: number }).z ?? 0 })
          }
        }
      }
      out.sort((a, b) => a.tz - b.tz || a.cz - b.cz || a.sk.start - b.sk.start)
      return out.map((o) => o.sk)
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
        const isDragging = dragRef.current?.id === sk.id

        // Paint the sticker's own pixels ONLY mid-gesture (see the
        // pixel-ownership rule in the module comment): the baked video shows
        // the pre-drag position, and this is the live-position feedback.
        // When idle, draw nothing — the server bake is the truth for both
        // emoji AND uploaded-PNG stickers.
        if (isDragging) {
          ctx.save()
          ctx.translate(g.cx, g.cy)
          ctx.rotate(g.rot)
          ctx.globalAlpha = g.opa
          if (sk.label) {
            // Emoji sticker: the glyph is a faithful-enough live proxy for
            // the baked Twemoji artwork.
            ctx.font = `${g.size}px "Apple Color Emoji", "Segoe UI Emoji", "Noto Color Emoji", sans-serif`
            ctx.textBaseline = 'middle'
            ctx.textAlign = 'center'
            ctx.fillText(sk.label, 0, 0)
          } else {
            const im = imageFor(sk, stateRef.current.sessionId)
            if (im instanceof HTMLImageElement && im.naturalWidth > 0) {
              // Fit inside the g.size box preserving the PNG's aspect —
              // same contain-fit the server bake uses (target_long on the
              // longer edge).
              const ar = im.naturalWidth / im.naturalHeight
              const dw = ar >= 1 ? g.size : g.size * ar
              const dh = ar >= 1 ? g.size / ar : g.size
              ctx.drawImage(im, -dw / 2, -dh / 2, dw, dh)
            } else if (im === 'error') {
              // Image unreachable (e.g. src outside the session's uploads/):
              // legacy translucent-circle placeholder, but only mid-drag.
              ctx.fillStyle = 'rgba(255,255,255,0.6)'
              ctx.beginPath()
              ctx.arc(0, 0, g.size / 2, 0, Math.PI * 2)
              ctx.fill()
            }
            // 'loading': draw nothing extra — the drag box below is enough
            // feedback, and the image resolves within a frame or two.
          }
          ctx.restore()
        }

        if (sk.id === selection) {
          const h = g.size / 2
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
          // ✕ delete handle: small filled circle floating above the top-right
          // corner (offset so it never overlaps the resize handle there).
          // Hidden mid-gesture — a drag that ends over it must not read as a
          // delete click, and it reduces chrome noise while moving.
          if (!isDragging) {
            const dx = h + DEL_GAP, dy = -h - DEL_GAP
            ctx.beginPath()
            ctx.arc(dx, dy, DEL_R, 0, Math.PI * 2)
            ctx.fillStyle = 'rgba(20,20,24,0.9)'
            ctx.fill()
            ctx.lineWidth = 1.5
            ctx.strokeStyle = dv.ACCENT
            ctx.stroke()
            // the ✕ glyph, drawn as two strokes (crisper than fillText)
            const r = DEL_R * 0.42
            ctx.beginPath()
            ctx.moveTo(dx - r, dy - r); ctx.lineTo(dx + r, dy + r)
            ctx.moveTo(dx + r, dy - r); ctx.lineTo(dx - r, dy + r)
            ctx.lineWidth = 1.8
            ctx.strokeStyle = '#fff'
            ctx.stroke()
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

      // 1) The ✕ delete handle of the currently-selected sticker (checked
      // before resize — it sits just outside the top-right corner handle).
      // ripple_delete on a Sticker is safe: dispatch.py only ripples other
      // overlays when the deleted clip is a v1 media Clip.
      const selSk = stickers.find((s) => s.id === sel)
      if (selSk) {
        const g = geomFor(selSk, t)
        const { lx, ly } = toLocal(px, py, g)
        const h = g.size / 2
        if (Math.hypot(lx - (h + DEL_GAP), ly - (-h - DEL_GAP)) <= DEL_R + 4) {
          e.preventDefault()
          setSelection(null)
          dispatch('ripple_delete', { clip_id: selSk.id })
          return
        }
      }

      // 2) Resize: a corner handle of the currently-selected sticker.
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

      // 3) Body hit (top-most first) → select + start move.
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

      // 4) Empty space → deselect.
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
          if (Math.hypot(lx - (h + DEL_GAP), ly - (-h - DEL_GAP)) <= DEL_R + 4) {
            cursor = 'pointer'  // the ✕ delete handle
          }
          if (cursor === 'default') {
            for (const [sx, sy] of [[-1, -1], [1, -1], [1, 1], [-1, 1]] as const) {
              if (Math.hypot(lx - sx * h, ly - sy * h) <= HANDLE_HIT) {
                cursor = dv.cursorForCorner(sx, sy)
                break
              }
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
