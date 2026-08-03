// Interactive OVERLAY layer. Draws selection/drag chrome on a transparent
// canvas stacked over the <video>, and makes every canvas overlay — stickers
// AND text — directly manipulable:
//   • click to select (→ Properties panel)
//   • drag the body to move (commits x/y)
//   • drag a corner handle to resize (sticker → scale, text → style.size)
//   • click the ✕ handle above the top-right corner to delete
// Live feedback is drawn locally during the gesture; the server is hit ONCE on
// pointer-up — same commit-on-release pattern as the other transform controls.
//
// Text was handle-less until round 5 (finding M-01): you could only move or
// resize it from the Properties panel, while a sticker two pixels away had
// full direct manipulation. Text geometry still belongs to <TextLayer> — it is
// the only place that measures and wraps the string — so TextLayer publishes
// each active clip's measured box through lib/overlay's registry and this
// layer hit-tests it with the exact same code paths as a sticker's. The one
// definition of the handles lives in lib/dragVisuals, so the two kinds cannot
// drift apart again.
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
// TEXT needs no equivalent: TextLayer draws it client-side every frame and
// picks up this layer's live drag offset directly.

import { useEffect, useRef } from 'react'
import { useStore } from '../store'
import type { EDL } from '../types'
import {
  isSticker, stickerGeom, boxFromStickerGeom, toLocal, hitsBody,
  getTextBoxes, setOverlayDrag, unsentinel, CORNERS,
  type StickerClip, type OverlayBox,
} from '../lib/overlay'
import * as dv from '../lib/dragVisuals'

interface Props {
  edl: EDL
  videoEl: HTMLVideoElement | null
  width: number
  height: number
}

// Text style.size bounds for a corner-drag resize. The floor keeps a
// mis-drag from producing an unreadable 1px caption that then has no handle
// big enough to grab back.
const TEXT_SIZE_MIN = 8
const TEXT_SIZE_MAX = 600

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
  | { id: string; kind: 'sticker' | 'text'; mode: 'move'; startMx: number; startMy: number
      x0: number; y0: number; xSentinels: number[]; ySentinels: number[]
      live: { x: number; y: number } }
  | { id: string; kind: 'sticker' | 'text'; mode: 'resize'; cx: number; cy: number
      startDist: number; scale0: number; size0: number; live: { scale: number; mul: number } }

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

    const stickerBox = (sk: StickerClip, t: number): OverlayBox => {
      const { edl, width, height } = stateRef.current
      const d = dragRef.current
      const ov =
        d && d.id === sk.id
          ? d.mode === 'move'
            ? { x: d.live.x, y: d.live.y }
            : { scale: d.live.scale }
          : undefined
      return boxFromStickerGeom(sk.id, stickerGeom(sk, t, edl.canvas.w, edl.canvas.h, width, height, ov))
    }

    // A text box, with the live drag offset folded in so the chrome tracks the
    // pointer exactly like a sticker's does. TextLayer applies the identical
    // offset to the glyphs it paints (it reads the same override), so box and
    // text move together.
    const textBoxLive = (b: OverlayBox): OverlayBox => {
      const d = dragRef.current
      if (!d || d.id !== b.id) return b
      if (d.mode === 'move') {
        const { edl, width, height } = stateRef.current
        return { ...b,
                 cx: b.cx + (d.live.x - d.x0) * (width / edl.canvas.w),
                 cy: b.cy + (d.live.y - d.y0) * (height / edl.canvas.h) }
      }
      return { ...b, hw: b.hw * d.live.mul, hh: b.hh * d.live.mul }
    }

    // Every selectable overlay at time t, in hit order (top-most LAST).
    // Text sits above stickers on screen (Preview.tsx stacks TextLayer over
    // this one), so it is hit first.
    const allBoxes = (t: number): OverlayBox[] => [
      ...activeStickers(t).map((sk) => stickerBox(sk, t)),
      ...getTextBoxes().map(textBoxLive),
    ]

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
      const boxes = allBoxes(t)
      // Only intercept clicks when there is something to hit at the playhead.
      cv.style.pointerEvents = boxes.length ? 'auto' : 'none'

      for (const sk of stickers) {
        if (dragRef.current?.id !== sk.id) continue
        const g = stickerGeom(sk, t, stateRef.current.edl.canvas.w, stateRef.current.edl.canvas.h,
                              width, height,
                              dragRef.current.mode === 'move'
                                ? { x: dragRef.current.live.x, y: dragRef.current.live.y }
                                : { scale: dragRef.current.live.scale })
        // Paint the sticker's own pixels ONLY mid-gesture (see the
        // pixel-ownership rule in the module comment): the baked video shows
        // the pre-drag position, and this is the live-position feedback.
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
            // Fit inside the g.size box preserving the PNG's aspect — same
            // contain-fit the server bake uses (target_long on the longer edge).
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

      const sel = boxes.find((b) => b.id === selection)
      if (sel) {
        const d = dragRef.current
        const dragging = d?.id === sel.id
        ctx.save()
        ctx.translate(sel.cx, sel.cy)
        ctx.rotate(sel.rot)
        ctx.globalAlpha = 1
        dv.drawSelectionChrome(ctx, sel.hw, sel.hh, {
          dragging: !!dragging,
          resizing: !!dragging && d!.mode === 'resize',
          showDelete: true,
        })
        ctx.restore()
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
      const boxes = allBoxes(t)
      const sel = stateRef.current.selection
      const selBox = boxes.find((b) => b.id === sel)

      // 1) The ✕ delete handle of the currently-selected overlay (checked
      // before resize — it sits just outside the top-right corner handle).
      // ripple_delete on a Sticker/TextClip is safe: dispatch.py only ripples
      // other overlays when the deleted clip is a v1 media Clip.
      if (selBox) {
        const { lx, ly } = toLocal(px, py, selBox)
        if (Math.hypot(lx - (selBox.hw + dv.DEL_GAP), ly - (-selBox.hh - dv.DEL_GAP)) <= dv.DEL_R + 4) {
          e.preventDefault()
          setSelection(null)
          dispatch('ripple_delete', { clip_id: selBox.id })
          return
        }
      }

      // 2) Resize: a corner handle of the currently-selected overlay.
      if (selBox) {
        const { lx, ly } = toLocal(px, py, selBox)
        const onHandle = CORNERS.some(
          ([sx, sy]) => Math.hypot(lx - sx * selBox.hw, ly - sy * selBox.hh) <= dv.HANDLE_HIT,
        )
        if (onHandle) {
          e.preventDefault()
          try { cv.setPointerCapture(e.pointerId) } catch { /* synthetic/edge pointer */ }
          const sk = activeStickers(t).find((s) => s.id === selBox.id)
          const scale0 = sk
            ? stickerGeom(sk, t, stateRef.current.edl.canvas.w, stateRef.current.edl.canvas.h,
                          stateRef.current.width, stateRef.current.height).scale
            : 1
          dragRef.current = {
            id: selBox.id, kind: selBox.kind, mode: 'resize', cx: selBox.cx, cy: selBox.cy,
            startDist: Math.max(4, Math.hypot(px - selBox.cx, py - selBox.cy)),
            scale0, size0: selBox.sizeCanvasPx ?? 0, live: { scale: scale0, mul: 1 },
          }
          setOverlayDrag({ id: selBox.id, dx: 0, dy: 0, sizeMul: 1 })
          return
        }
      }

      // 3) Body hit → select + start move, CYCLING through overlaps.
      //
      // This used to return the first (top-most) hit unconditionally, so an
      // overlay underneath another was unreachable: clicking selected the top one
      // forever, and Backspace then had nothing of the buried one to delete. The
      // backend now cascades identical insert positions, but a deliberate stack —
      // or any project created before that fix — still needs a way in.
      //
      // Repeat-clicking the same spot walks DOWN the stack and wraps, which is
      // the standard behaviour for overlapping canvas objects.
      const under: OverlayBox[] = []
      for (let i = boxes.length - 1; i >= 0; i--) {
        if (hitsBody(px, py, boxes[i])) under.push(boxes[i])
      }
      if (under.length) {
        const at = under.findIndex((b) => b.id === sel)
        // Currently-selected one is under the cursor → take the next one down
        // (wrapping). Otherwise take the top-most.
        const pick = at === -1 ? under[0] : under[(at + 1) % under.length]
        e.preventDefault()
        if (pick.id !== sel) setSelection(pick.id)
        try { cv.setPointerCapture(e.pointerId) } catch { /* synthetic/edge pointer */ }
        dragRef.current = {
          id: pick.id, kind: pick.kind, mode: 'move', startMx: px, startMy: py,
          x0: pick.x, y0: pick.y,
          xSentinels: pick.xSentinels ?? [], ySentinels: pick.ySentinels ?? [],
          live: { x: pick.x, y: pick.y },
        }
        setOverlayDrag({ id: pick.id, dx: 0, dy: 0, sizeMul: 1 })
        return
      }

      // 4) Empty space → deselect.
      if (sel) setSelection(null)
    }

    const onMove = (e: PointerEvent) => {
      const d = dragRef.current
      const { px, py } = posOf(e)
      if (!d) {
        // Hover cursor feedback: resize cursor over the selected overlay's
        // corner handle, move cursor over any overlay body, default otherwise.
        const t = now()
        const sel = stateRef.current.selection
        const boxes = allBoxes(t)
        let cursor = 'default'
        const selBox = boxes.find((b) => b.id === sel)
        if (selBox) {
          const { lx, ly } = toLocal(px, py, selBox)
          if (Math.hypot(lx - (selBox.hw + dv.DEL_GAP), ly - (-selBox.hh - dv.DEL_GAP)) <= dv.DEL_R + 4) {
            cursor = 'pointer'  // the ✕ delete handle
          }
          if (cursor === 'default') {
            for (const [sx, sy] of CORNERS) {
              if (Math.hypot(lx - sx * selBox.hw, ly - sy * selBox.hh) <= dv.HANDLE_HIT) {
                cursor = dv.cursorForCorner(sx, sy)
                break
              }
            }
          }
        }
        if (cursor === 'default' && boxes.some((b) => hitsBody(px, py, b))) cursor = 'move'
        cv.style.cursor = cursor
        return
      }
      const { edl, width, height } = stateRef.current
      if (d.mode === 'move') {
        d.live.x = d.x0 + (px - d.startMx) * (edl.canvas.w / width)
        d.live.y = d.y0 + (py - d.startMy) * (edl.canvas.h / height)
        setOverlayDrag({ id: d.id, dx: px - d.startMx, dy: py - d.startMy, sizeMul: 1 })
      } else {
        const dist = Math.hypot(px - d.cx, py - d.cy)
        const mul = dist / d.startDist
        d.live.mul = Math.max(0.1, Math.min(8, mul))
        d.live.scale = Math.max(0.1, Math.min(8, d.scale0 * mul))
        setOverlayDrag({ id: d.id, dx: 0, dy: 0, sizeMul: d.live.mul })
      }
    }

    const onUp = (e: PointerEvent) => {
      const d = dragRef.current
      if (!d) return
      try { cv.releasePointerCapture(e.pointerId) } catch { /* noop */ }
      dragRef.current = null
      setOverlayDrag(null)
      if (d.mode === 'move') {
        // A body pointerdown always starts a 'move' drag, so a plain
        // select-click lands here with unchanged coords — skip the commit
        // entirely (an identical-x/y op would still clear the redo stack).
        const moved = Math.round(d.live.x) !== Math.round(d.x0)
          || Math.round(d.live.y) !== Math.round(d.y0)
        if (!moved) return
        // Deliberately position-only: a drag does NOT restack. Auto-raising
        // the dragged overlay would silently override an explicit "Send to
        // back", and it can't share this commit — two dispatches means Undo
        // reverts the raise while leaving the overlay moved. Stacking is
        // controlled explicitly by Properties' Bring-to-front / Send-to-back.
        dispatch('set_clip_transform', {
          clip_id: d.id,
          x: unsentinel(Math.round(d.live.x), d.xSentinels),
          y: unsentinel(Math.round(d.live.y), d.ySentinels),
        })
      } else if (d.kind === 'text') {
        // Text resizes by its style.size (EDL-canvas px) — the same field the
        // Properties panel drives and the server's resolve_size_override reads.
        if (d.size0 <= 0) return
        const next = Math.round(Math.min(TEXT_SIZE_MAX, Math.max(TEXT_SIZE_MIN, d.size0 * d.live.mul)))
        if (next !== Math.round(d.size0)) {
          dispatch('set_property', { clip_id: d.id, path: 'style.size', value: next })
        }
      } else {
        dispatch('set_clip_transform', { clip_id: d.id, scale: Math.round(d.live.scale * 100) / 100 })
      }
    }

    cv.addEventListener('pointerdown', onDown)
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    return () => {
      cancelAnimationFrame(raf)
      setOverlayDrag(null)
      cv.removeEventListener('pointerdown', onDown)
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
    }
  }, [videoEl, dispatch, setSelection])

  return <canvas ref={canvasRef} style={{ position: 'absolute', inset: 0 }} />
}
