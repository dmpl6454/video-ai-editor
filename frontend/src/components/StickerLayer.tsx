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
// PIXEL-OWNERSHIP RULE: in PREVIEW, this layer owns a sticker's pixels
// outright — text_overlay.py's build_overlay_chain(preview=True) skips
// stickers as well as text, so the <video> underneath carries none. Export
// still bakes both (there is no TextLayer/StickerLayer there).
//
// It used to be the other way round: the server baked stickers even in
// preview, so this layer drew the sticker's own image ONLY mid-gesture, as
// live feedback. That produced a ghost — the baked copy stayed frozen at the
// pre-drag position while this one tracked the pointer, so a drag showed TWO
// stickers, and on release the live copy vanished while the stale one lingered
// at the old spot for the entire commit→re-render gap ("it disappears, then
// leaves a trail at the last position"). A client cannot erase a baked pixel,
// so smooth direct manipulation requires owning them — the conclusion text
// reached first.
//
// Consequences to preserve:
//   • Draw the ARTWORK PNG (imageFor), never the OS emoji glyph. The glyph is
//     a different design from the Twemoji the export bakes, so painting it
//     made the sticker visibly change appearance on commit. It survives only
//     as a fallback for when the artwork genuinely can't be fetched (the
//     emoji cache was cleared, a brand-kit end-card moved on disk).
//   • Preview.tsx's videoFingerprint must NOT include sticker tracks — no
//     sticker edit needs an ffmpeg round-trip any more.

import { useEffect, useRef } from 'react'
import { useStore } from '../store'
import { isMediaClip, clipEnd, type EDL, type Clip } from '../types'
import {
  isSticker, stickerGeom, boxFromStickerGeom, toLocal, hitsBody,
  getTextBoxes, setOverlayDrag, unsentinel, CORNERS, paintOrder,
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

// Cache of sticker artwork, keyed by the EDL `src` (server-absolute path) so
// two clips sharing one image decode once. Values: HTMLImageElement once
// decoded, 'loading' while in flight, 'error' after a failed load.
const IMG_CACHE = new Map<string, HTMLImageElement | 'loading' | 'error'>()

function imageFor(sk: StickerClip, sid: string | null): HTMLImageElement | 'loading' | 'error' {
  const cached = IMG_CACHE.get(sk.src)
  if (cached) return cached
  if (!sid) return 'error'
  // Fetch by CLIP ID, not by path. `/files/uploads/<name>` only reaches files
  // under this session's uploads/ — correct, that containment check is its
  // security model — but plenty of legitimate sticker srcs live elsewhere:
  // emoji added before add_sticker copied art into the session, a brand kit's
  // end-card/watermark, any absolute path. Those resolved to a 404 and, now
  // that this layer owns sticker pixels in preview, would have drawn an empty
  // box while exporting perfectly. /sticker/{clip_id} resolves the path
  // through the session's own EDL instead.
  const url = `/api/sessions/${encodeURIComponent(sid)}/sticker/${encodeURIComponent(sk.id)}`
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
  // Direct on-canvas drag of the base video clip itself (Canva-style "just
  // grab the picture and move it", replacing having to type x/y numbers in
  // Properties). Scoped to v1 specifically — a v2/PIP clip's on-screen box
  // depends on ITS OWN transform (position/scale), which would need real
  // geometry + hit-testing to disambiguate overlapping video layers, the same
  // way stickerGeom() does for stickers. v1 always fills the whole canvas
  // with nothing else competing for the same screen space, so any click that
  // isn't on a sticker/text unambiguously means v1.
  | { id: string; kind: 'video'; mode: 'move'; startMx: number; startMy: number
      x0: number; y0: number; live: { x: number; y: number } }

export function StickerLayer({ edl, videoEl, width, height }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const selection = useStore((s) => s.selection)
  const setSelection = useStore((s) => s.setSelection)
  const dispatch = useStore((s) => s.dispatch)
  const sessionId = useStore((s) => s.sessionId)
  const setLiveTransform = useStore((s) => s.setLiveTransform)

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

    // NOTE: a text box arrives from TextLayer ALREADY LIVE — it reads the same
    // getOverlayDrag() override and applies the move offset and the resize
    // multiplier before publishing, because it has to draw the glyphs there.
    // This layer used to re-apply both on top, so the chrome moved at DOUBLE
    // the pointer distance and detached upward from the text it was supposed
    // to be around ("when I drag the text, the text box gets outside of the
    // text"); a resize inflated it by mul² the same way. The commit reads
    // d.live.* rather than the box, which is why the text still landed in the
    // right place and only the box lied.
    //
    // TextLayer is the correct owner: it is the only place that measures and
    // wraps a string, and under a live resize it RE-WRAPS at the new size —
    // line breaks can change, so the true box is not a uniform scale of the
    // old one and could not be reconstructed here anyway. The cost is that the
    // chrome can trail the glyphs by at most one frame (two rAF loops), which
    // is invisible; being 2× wrong was not. A sticker's box is still built
    // locally (stickerBox) because THIS layer owns sticker geometry.
    // Every selectable overlay at time t, in hit order (top-most LAST).
    // Text sits above stickers on screen (Preview.tsx stacks TextLayer over
    // this one), so it is hit first.
    const allBoxes = (t: number): OverlayBox[] => [
      ...activeStickers(t).map((sk) => stickerBox(sk, t)),
      ...getTextBoxes(),
    ]

    // The v1 clip on screen at time t, if any — the direct-drag target when
    // nothing else (sticker/text) is under the cursor.
    const activeV1Clip = (t: number): Clip | undefined => {
      const v1 = stateRef.current.edl.tracks.find((tk) => tk.id === 'v1')
      if (!v1) return undefined
      return v1.clips.find(
        (c): c is Clip => isMediaClip(c) && c.start <= t && t < clipEnd(c),
      )
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
      const boxes = allBoxes(t)
      // Intercept clicks whenever there's a sticker/text to hit OR a v1 clip
      // to grab-and-drag directly — which in practice is "whenever there's
      // any footage on screen", i.e. almost always. Only a genuine gap (no
      // v1 clip, no overlays) lets clicks fall through with nothing to do.
      cv.style.pointerEvents = (boxes.length || activeV1Clip(t)) ? 'auto' : 'none'

      // The sticker being dragged paints LAST — see paintOrder's comment: its z
      // is only raised at commit time, so stored order made an older sticker
      // dive under a newer one for the whole gesture.
      for (const sk of paintOrder(stickers, dragRef.current?.id)) {
        const d = dragRef.current
        const ov = d?.id === sk.id
          ? (d.mode === 'move' ? { x: d.live.x, y: d.live.y } : { scale: d.live.scale })
          : undefined
        const g = stickerGeom(sk, t, stateRef.current.edl.canvas.w, stateRef.current.edl.canvas.h,
                              width, height, ov)
        ctx.save()
        ctx.translate(g.cx, g.cy)
        ctx.rotate(g.rot)
        ctx.globalAlpha = g.opa
        const im = imageFor(sk, stateRef.current.sessionId)
        if (im instanceof HTMLImageElement && im.naturalWidth > 0) {
          // Fit inside the g.size box preserving the PNG's aspect — same
          // contain-fit the server bake uses (target_long on the longer edge).
          const ar = im.naturalWidth / im.naturalHeight
          const dw = ar >= 1 ? g.size : g.size * ar
          const dh = ar >= 1 ? g.size / ar : g.size
          ctx.drawImage(im, -dw / 2, -dh / 2, dw, dh)
        } else if (im === 'error' && sk.label) {
          // The artwork file is genuinely gone (emoji cache cleared, image
          // moved on disk) — /sticker/{clip_id} 404s. The OS glyph is a
          // near-enough stand-in; it is NOT what the export bakes, so this is
          // a fallback, never the normal path.
          ctx.font = `${g.size}px "Apple Color Emoji", "Segoe UI Emoji", "Noto Color Emoji", sans-serif`
          ctx.textBaseline = 'middle'
          ctx.textAlign = 'center'
          ctx.fillText(sk.label, 0, 0)
        } else if (im === 'error') {
          // Label-less PNG sticker we can't fetch: outline the box so the
          // clip is still selectable/draggable rather than invisible.
          ctx.strokeStyle = 'rgba(255,255,255,0.45)'
          ctx.setLineDash([5, 4])
          ctx.strokeRect(-g.size / 2, -g.size / 2, g.size, g.size)
          ctx.setLineDash([])
        }
        // 'loading': draw nothing — resolves within a frame or two.
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
          deleteAt: dv.deleteHandleLocal(sel, width, height),
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
        const del = dv.deleteHandleLocal(selBox, stateRef.current.width, stateRef.current.height)
        if (Math.hypot(lx - del.lx, ly - del.ly) <= dv.DEL_R + 4) {
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

      // 4) Nothing overlay-shaped under the cursor → grab the base video
      // clip itself, Canva-style direct manipulation instead of requiring
      // the Properties panel's x/y number fields. Falls through to plain
      // deselect if there's no v1 footage at this instant (a gap).
      const vclip = activeV1Clip(t)
      if (vclip) {
        const fit = (vclip as unknown as { fit?: string }).fit
        e.preventDefault()
        if (vclip.id !== sel) setSelection(vclip.id)
        // A cover-fit clip's direct drag is owned by <CropReposition> instead
        // (Preview.tsx mounts it, covering this canvas, once the clip is
        // selected) — it needs the raw uncropped source and a zoomed-out
        // viewport to show real footage while panning, which this simple
        // CSS-translate-the-baked-frame approximation can't provide (that
        // approximation is exactly what made a cover-fit pan reveal
        // manufactured black instead of real footage). Only start a drag
        // here for `contain`, where the approximation has no such gap.
        if (fit === 'cover') return
        const tx = (vclip as unknown as { transform?: { x?: unknown; y?: unknown } }).transform
        const x0 = typeof tx?.x === 'number' ? tx.x : 0
        const y0 = typeof tx?.y === 'number' ? tx.y : 0
        try { cv.setPointerCapture(e.pointerId) } catch { /* synthetic/edge pointer */ }
        dragRef.current = {
          id: vclip.id, kind: 'video', mode: 'move', startMx: px, startMy: py,
          x0, y0, live: { x: x0, y: y0 },
        }
        setLiveTransform({ clipId: vclip.id, dx: 0, dy: 0 })
        return
      }

      // 5) Truly empty space → deselect.
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
          const del = dv.deleteHandleLocal(selBox, stateRef.current.width, stateRef.current.height)
          if (Math.hypot(lx - del.lx, ly - del.ly) <= dv.DEL_R + 4) {
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
        // No overlay under the cursor, but there's real footage to grab —
        // same hint a sticker/text body gets, so the video reads as
        // draggable too instead of looking static.
        if (cursor === 'default' && activeV1Clip(t)) cursor = 'move'
        cv.style.cursor = cursor
        return
      }
      const { edl, width, height } = stateRef.current
      if (d.kind === 'video') {
        d.live.x = d.x0 + (px - d.startMx) * (edl.canvas.w / width)
        d.live.y = d.y0 + (py - d.startMy) * (edl.canvas.h / height)
        // Preview.tsx applies this as a CSS translate on the <video> — see
        // the liveTransform.dx/dy comment in store.ts.
        setLiveTransform({ clipId: d.id, dx: d.live.x - d.x0, dy: d.live.y - d.y0 })
      } else if (d.mode === 'move') {
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
      if (d.kind === 'video') {
        // Deliberately do NOT clear liveTransform here — Preview.tsx's
        // onLoadedData clears it once the re-render carrying this commit
        // actually lands, so the CSS-translated preview stays put instead of
        // snapping back to the pre-drag frame for the commit/re-render gap
        // (the same lifecycle scale/rotation dragging already follows).
        const moved = Math.round(d.live.x) !== Math.round(d.x0)
          || Math.round(d.live.y) !== Math.round(d.y0)
        if (!moved) { setLiveTransform(null); return }
        dispatch('set_clip_transform', {
          clip_id: d.id, x: Math.round(d.live.x), y: Math.round(d.live.y),
        })
        return
      }
      setOverlayDrag(null)
      if (d.mode === 'move') {
        // A body pointerdown always starts a 'move' drag, so a plain
        // select-click lands here with unchanged coords — skip the commit
        // entirely (an identical-x/y op would still clear the redo stack).
        const moved = Math.round(d.live.x) !== Math.round(d.x0)
          || Math.round(d.live.y) !== Math.round(d.y0)
        if (!moved) return
        // Raise the dragged sticker above anything that currently COVERS it,
        // in the same commit (set_clip_transform's raise_to_front).
        //
        // Stacking is (track_z, clip_z, start), so with every sticker at the
        // default z=0 the newest-added always won and dragging an older one
        // on top of it changed nothing — you could see the sticker you were
        // holding disappear underneath. Conditional rather than unconditional:
        // only when something actually overlaps it on screen AND composites
        // above it, so a deliberate "Send to back" survives an unrelated nudge
        // and a drag in open space commits no pointless z change. Text is
        // excluded — TextClip has no per-clip z (it layers by track).
        let raise = false
        if (d.kind === 'sticker') {
          const t2 = now()
          const order = activeStickers(t2)      // ascending composite order
          const meIdx = order.findIndex((s) => s.id === d.id)
          if (meIdx >= 0) {
            // The DROPPED box, not the stored one: the commit hasn't landed
            // yet, so the EDL still holds the pre-drag x/y and testing that
            // would answer "did it overlap where it USED to be?".
            const mine = stickerBox(order[meIdx], t2)
            const { edl, width: w, height: h } = stateRef.current
            const cx = d.live.x * (w / edl.canvas.w)
            const cy = d.live.y * (h / edl.canvas.h)
            raise = order.slice(meIdx + 1).some((other) => {
              const ob = stickerBox(other, t2)
              return Math.abs(ob.cx - cx) < ob.hw + mine.hw
                  && Math.abs(ob.cy - cy) < ob.hh + mine.hh
            })
          }
        }
        dispatch('set_clip_transform', {
          clip_id: d.id,
          x: unsentinel(Math.round(d.live.x), d.xSentinels),
          y: unsentinel(Math.round(d.live.y), d.ySentinels),
          ...(raise ? { raise_to_front: true } : {}),
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
