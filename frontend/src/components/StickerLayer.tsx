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
//     a different design from the artwork the export bakes — on every platform,
//     since the fetched set is a pinned release and not any locally-installed
//     font — so painting it made the sticker visibly change appearance on
//     commit (and differ between a Mac and a Windows viewer). It survives only
//     as a fallback for when the artwork genuinely can't be fetched (the
//     emoji cache was cleared, a brand-kit end-card moved on disk).
//   • Preview.tsx's videoFingerprint must NOT include sticker tracks — no
//     sticker edit needs an ffmpeg round-trip any more.

import { useEffect, useRef } from 'react'
import { useStore } from '../store'
import { isMediaClip, clipEnd, type EDL, type Clip } from '../types'
import {
  isSticker, stickerGeom, boxFromStickerGeom, toLocal, hitsBody,
  getTextBoxes, setOverlayDrag, unsentinel, CORNERS, paintOrder, dropSettled,
  resolveLiveOverride, clampOverlayCentre, pipGeom, sampleKF, mergeLivePipScale,
  type StickerClip, type OverlayBox, type LiveDrag,
} from '../lib/overlay'
import { srcDimsFor, sessionFileUrl } from '../lib/media'
import {
  pipVideo, syncPipVideo, pipDrawGeom, clipToShape, pipIsClientDrawn,
  pausePipVideosExcept, drawPipVideoFrame, pipInnerPlan,
  setLivePipFraming, livePipFraming,
} from '../lib/pipDraw'
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
  // 'pip-frame' is an ALT-drag that pans the picture inside a cropped PIP's
  // shape rather than moving the shape; live.x/y are the normalised framing
  // offsets, not canvas pixels.
  | { id: string; kind: 'sticker' | 'text' | 'pip' | 'pip-frame'; mode: 'move'
      startMx: number; startMy: number
      x0: number; y0: number; xSentinels: number[]; ySentinels: number[]
      live: { x: number; y: number } }
  | { id: string; kind: 'sticker' | 'text' | 'pip'; mode: 'resize'; cx: number; cy: number
      startDist: number; scale0: number; size0: number; live: { scale: number; mul: number } }
  // Word/PowerPoint-style rotate: grab the grip above the box and turn it.
  // `rot0` + `a0` make the gesture RELATIVE, so the box does not snap its top
  // to the pointer on the first pixel of the drag.
  | { id: string; kind: 'sticker' | 'text' | 'pip'; mode: 'rotate'
      cx: number; cy: number; rot0: number; a0: number; live: { rotation: number } }
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
  // Framing mode gates the base-video drag (see v1DragAllowed).
  const framing = useStore((s) => s.framing)
  // Playback state decides whether a PIP's hidden element is PLAYED or SEEKED
  // (see syncPipVideo — seeking it per frame is what made the PIP flicker).
  const isPlaying = useStore((s) => s.isPlaying)
  const playbackRate = useStore((s) => s.playbackRate)
  // The Properties Transform sliders publish their in-flight value here. For a
  // v1 clip Preview.tsx turns it into a CSS transform on the <video>; for a PIP
  // the <video> is the WRONG element (the browser draws the PIP, so the render
  // does not contain it), and this layer is the right one. See livePipTx.
  const liveTransform = useStore((s) => s.liveTransform)

  // Keep the latest reactive values in a ref so the rAF loop + event handlers
  // (registered once) always read fresh state without re-binding.
  const stateRef = useRef({ edl, width, height, selection, sessionId, framing,
                            isPlaying, playbackRate, liveTransform })
  stateRef.current = { edl, width, height, selection, sessionId, framing,
                       isPlaying, playbackRate, liveTransform }
  const dragRef = useRef<Drag | null>(null)
  // Committed-but-unconfirmed rotation from the grip (see liveRot).
  const heldRotRef = useRef<{ id: string; deg: number; at: number } | null>(null)

  // What the last drop committed, held until the EDL comes back carrying it.
  //
  // The override that draws the overlay under the pointer must outlive the
  // gesture, because a commit is not instant: dispatch → ~120 ms refreshSoon()
  // debounce → GET /edl. Clearing on pointer-up meant the layer redrew from the
  // STORED coordinates for that whole window, so the sticker jumped back to
  // where the drag began and then forward again once the refresh landed
  // (measured at 233 ms on the reported recording). Same lifecycle Preview.tsx
  // already uses for a v1 clip's liveTransform, which is cleared on the
  // <video>'s onLoadedData rather than on pointer-up.
  const pendingRef = useRef<
    { id: string; x?: number; y?: number; scale?: number; size?: number } | null>(null)
  const holdTimerRef = useRef<number | null>(null)
  // Same lifecycle for an alt-drag reframe, kept separate because it is checked
  // against `framing` rather than `transform`.
  const framingPendingRef = useRef<{ id: string; x: number; y: number } | null>(null)

  const releaseHold = () => {
    if (holdTimerRef.current !== null) {
      clearTimeout(holdTimerRef.current)
      holdTimerRef.current = null
    }
    pendingRef.current = null
    framingPendingRef.current = null
    setOverlayDrag(null)
    setLivePipFraming(null)
  }

  const holdDrag = (expect: { id: string; x?: number; y?: number; scale?: number; size?: number }) => {
    pendingRef.current = expect
    if (holdTimerRef.current !== null) clearTimeout(holdTimerRef.current)
    // SAFETY NET, and it is load-bearing: if the dispatch fails, or the value
    // is clamped server-side so the EDL never matches what we asked for, the
    // override would otherwise pin the overlay to the dragged spot forever —
    // a permanently wrong position is far worse than the flicker this fixes.
    // Preview.tsx's liveTransform carries the same bounded fallback.
    holdTimerRef.current = window.setTimeout(releaseHold, 2000)
  }

  // Release as soon as the refreshed EDL agrees with what we committed.
  useEffect(() => {
    const fp = framingPendingRef.current
    if (fp) {
      for (const tk of edl.tracks) {
        for (const c of tk.clips as unknown as
             { id: string; framing?: { x?: number; y?: number } | null }[]) {
          if (c.id !== fp.id) continue
          const fx = c.framing?.x ?? 0
          const fy = c.framing?.y ?? 0
          if (Math.abs(fx - fp.x) <= 0.011 && Math.abs(fy - fp.y) <= 0.011) releaseHold()
          return
        }
      }
      releaseHold()   // clip gone
      return
    }
    const p = pendingRef.current
    if (!p) return
    for (const tk of edl.tracks) {
      for (const c of tk.clips as unknown as { id: string }[]) {
        if (c.id !== p.id) continue
        if (dropSettled(c, p)) releaseHold()
        return
      }
    }
    // The clip is gone (deleted mid-flight) — nothing left to wait for.
    releaseHold()
  }, [edl])

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

    // Live gesture, then the committed-but-unconfirmed drop, then stored. The
    // rule and the reason it has to be applied HERE (not only via
    // setOverlayDrag, which is TextLayer's channel) are in resolveLiveOverride.
    const liveOverride = (id: string) =>
      resolveLiveOverride(id, dragRef.current as LiveDrag | null, pendingRef.current)

    /** In-flight Transform-slider value for `id`, or null.
     *
     *  A PIP's picture is drawn HERE, not baked into the render the <video>
     *  shows, so this layer is the only place a PIP's live scale/rotation/
     *  opacity can appear. Preview.tsx deliberately no longer applies them to
     *  the <video> for a non-v1 clip — doing so dimmed and moved the main
     *  picture instead of the PIP.
     *
     *  Scoped by clipId, so it only ever affects the element whose slider is
     *  being dragged. It cannot conflict with a pointer drag on the canvas
     *  (one pointer, one gesture), and where both could name a field the drag
     *  override wins — it is the more direct manipulation.
     */
    const livePipTx = (id: string) => {
      const lt = stateRef.current.liveTransform
      return lt && lt.clipId === id ? lt : null
    }

    const stickerBox = (sk: StickerClip, t: number): OverlayBox => {
      const { edl, width, height } = stateRef.current
      const ov = liveOverride(sk.id)
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
    // PIP (v2+) clips active at t. This layer PAINTS THEIR PIXELS, because the
    // preview render no longer bakes them (the `preview` branch in
    // render/pip.py) — the same ownership split text and stickers already have.
    //
    // It used to draw only chrome plus a mid-drag ghost rectangle, which was the
    // honest thing to draw while the picture was baked: it could not move until
    // the re-render carrying the commit arrived. That is what "the video doesn't
    // follow the blue box… it reacts very late" was — not slowness, but a
    // picture the client had no way to move. Owning the pixels is the only fix;
    // a client cannot erase a baked one, so a live copy over a baked copy shows
    // TWO PIPs for the whole gesture.
    //
    // The cost of that ownership: lib/pipDraw's geometry must match pip.py's,
    // since EXPORT still bakes and has no client. Both sides are pinned to one
    // table of box dimensions (overlay.test.ts + test_pip_overlay.py).
    const activePips = (t: number): Clip[] => {
      const out: Clip[] = []
      for (const tk of stateRef.current.edl.tracks) {
        if (tk.type !== 'video' || tk.id === 'v1') continue
        for (const c of tk.clips) {
          if (isMediaClip(c) && c.start <= t && t < clipEnd(c)) out.push(c)
        }
      }
      return out
    }

    const pipBox = (c: Clip, t: number): OverlayBox => {
      const { edl, width, height, sessionId } = stateRef.current
      // The EDL records no frame size, so the PIP's aspect is probed from the
      // source file and cached (lib/media). While it loads, pipGeom falls back
      // to the canvas aspect so the box stays grabbable instead of vanishing.
      const dims = srcDimsFor(c.src, sessionId)
      const aspect = typeof dims === 'object' ? dims.w / Math.max(1, dims.h) : null
      // A canvas drag wins over a slider where both name a field (see
      // livePipTx); `??` gives exactly that precedence without dropping the
      // drag's x/y when only `scale` is being slid.
      const ov = liveOverride(c.id)
      const lt = livePipTx(c.id)
      const g = pipGeom(
        c as unknown as { start: number; transform?: StickerClip['transform']
                          mask?: { type?: string } | null; fit?: string },
        t, edl.canvas, aspect, width, height, mergeLivePipScale(ov, lt?.scale))
      const lr = liveRot(c.id)
      const rotDeg = lr ?? lt?.rotation ?? null
      return { id: c.id, kind: 'pip', cx: g.cx, cy: g.cy, hw: g.hw, hh: g.hh,
               rot: rotDeg === null ? g.rot : (rotDeg * Math.PI) / 180, x: g.x, y: g.y }
    }

    // Hit order is top-most LAST. Text and stickers sit above the video layers
    // on screen, and a PIP sits above v1 — so a PIP is grabbable in the gap
    // between them, and clicking one no longer falls through to dragging the
    // base video underneath it.
    const allBoxes = (t: number): OverlayBox[] => [
      ...activePips(t).map((c) => pipBox(c, t)),
      ...activeStickers(t).map((sk) => stickerBox(sk, t)),
      ...getTextBoxes(),
    ]

    /** May the base video be dragged right now? Only while FRAMING it.
     *
     *  Repositioning the picture is a framing decision, and framing is an
     *  explicit mode now (store.framing, entered from the Properties panel's
     *  "Adjust framing…" button). Outside it the preview is for watching and
     *  selecting, not for nudging the shot.
     */
    const v1DragAllowed = (t: number): boolean => {
      const c = activeV1Clip(t)
      return !!c && stateRef.current.framing?.clipId === c.id
    }

    // The v1 clip on screen at time t, if any — the direct-drag target when
    // nothing else (sticker/text) is under the cursor.
    const activeV1Clip = (t: number): Clip | undefined => {
      const v1 = stateRef.current.edl.tracks.find((tk) => tk.id === 'v1')
      if (!v1) return undefined
      return v1.clips.find(
        (c): c is Clip => isMediaClip(c) && c.start <= t && t < clipEnd(c),
      )
    }

    // Rotation being dragged right now, so the box turns under the pointer
    // instead of after the commit round-trip — and then HELD at the committed
    // angle until the refreshed EDL carries it, the same lifecycle (and for the
    // same snap-back reason) as a dropped overlay's position.
    const liveRot = (id: string): number | null => {
      const d = dragRef.current
      if (d && d.mode === 'rotate' && d.id === id) return d.live.rotation
      const h = heldRotRef.current
      if (h && h.id === id) {
        const c = stateRef.current.edl.tracks
          .flatMap((tk) => tk.clips)
          .find((k) => (k as { id?: string }).id === id) as
            { transform?: { rotation?: unknown } } | undefined
        const stored = sampleKF(c?.transform?.rotation as never, 0, 0)
        // Settled once the EDL agrees (within the commit's own rounding), or
        // after a bounded wait — a permanently wrong angle is far worse than
        // one flicker, which is why the timeout is not optional.
        if (Math.abs(((stored - h.deg + 540) % 360) - 180) < 1.5
            || performance.now() - h.at > 2000) {
          heldRotRef.current = null
          return null
        }
        return h.deg
      }
      return null
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
      // CLEAR IN DEVICE PIXELS, NOT CSS PIXELS. `cv.width` is
      // `Math.round(width * dpr)`, but `clearRect(0,0,width,height)` under a
      // dpr transform only covers `width * dpr` device px — and at a fractional
      // dpr those differ, so the far right column and bottom row are never
      // fully cleared and keep whatever was last painted there FOREVER.
      //
      // This is the "phantom colour strip" reported four times, and it hid
      // behind three wrong diagnoses (a GPU sampling artifact, the panel
      // splitter's hover tint, the sticker being dragged half off-frame)
      // because it is invisible wherever anyone looked for it: it needs a
      // FRACTIONAL devicePixelRatio, so it never reproduces in headless
      // Chromium at dpr 1, and Windows display scaling at 125% is what makes
      // it routine on a real machine. Measured inside the packaged WebView2
      // window at dpr=1.25: canvas cssW 914 -> backing 1143 while the clear
      // reached only 1142.5, leaving accent ink (#5b8dff selection chrome)
      // in columns 1139-1142 that was byte-identical before a drag, after
      // dragging right, and after dragging back — i.e. never redrawn, just
      // never erased. That is why it read as the sticker "leaving" a colour.
      //
      // Resetting the transform first makes the clear cover the whole backing
      // store no matter how the rounding fell. Do NOT "simplify" this back to
      // clearRect(0, 0, width, height).
      ctx.setTransform(1, 0, 0, 1, 0, 0)
      ctx.clearRect(0, 0, cv.width, cv.height)
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      const t = now()
      const stickers = activeStickers(t)
      const boxes = allBoxes(t)
      // Intercept clicks whenever there's a sticker/text to hit OR a v1 clip
      // to grab-and-drag directly — which in practice is "whenever there's
      // any footage on screen", i.e. almost always. Only a genuine gap (no
      // v1 clip, no overlays) lets clicks fall through with nothing to do.
      cv.style.pointerEvents = (boxes.length || activeV1Clip(t)) ? 'auto' : 'none'

      // PIP pictures, UNDER the stickers/text drawn below and over the base
      // video. The preview render leaves these out on purpose (pip.py's
      // `preview` branch), so this is the preview's PIP renderer — which is
      // what makes a drag/resize/reframe move real frames under the pointer
      // instead of arriving with the next render.
      // Sources drawn this frame; everything else gets paused below so an
      // ended PIP does not keep decoding in the background.
      const livePipSrcs = new Set<string>()
      for (const pc of activePips(t)) {
        // A chromakey'd PIP is still BAKED (pipIsClientDrawn explains why), so
        // painting it here would double-draw it — and the baked copy is one the
        // client cannot erase. It keeps its selection chrome and its drag; only
        // the live picture is unavailable, so it lags as it did before.
        if (!pipIsClientDrawn(pc as unknown as { chromakey?: unknown })) continue
        const box = pipBox(pc, t)
        const url = sessionFileUrl(pc.src, stateRef.current.sessionId ?? '')
        const dims = srcDimsFor(pc.src, stateRef.current.sessionId)
        const v = url ? pipVideo(pc.src, url) : null
        if (v && typeof dims === 'object') {
          // PLAY the element while the timeline plays; only SEEK while paused.
          // Seeking per frame is what made the PIP flicker and vanish during
          // playback — see syncPipVideo.
          syncPipVideo(v, t, pc.start, (pc as unknown as { in?: number }).in ?? 0,
                       stateRef.current.edl.canvas.fps ?? 30,
                       { playing: stateRef.current.isPlaying,
                         rate: stateRef.current.playbackRate })
          livePipSrcs.add(pc.src)
        }
        const pcx = pc as unknown as {
          mask?: { type?: string; invert?: boolean } | null
          framing?: { x?: number; y?: number; zoom?: number; rotation?: number } | null
          transform?: { opacity?: unknown }
        }
        // Live alt-drag framing wins over the stored value, so the picture pans
        // under the pointer rather than after the commit.
        const lf = livePipFraming(pc.id)
        const fr = lf
          ? { ...(pcx.framing ?? {}), x: lf.x, y: lf.y,
              ...(lf.rotation === undefined ? {} : { rotation: lf.rotation }) }
          : pcx.framing
        ctx.save()
        ctx.translate(box.cx, box.cy)
        ctx.rotate(box.rot)
        // Live slider value first, so lowering a PIP's opacity fades THE PIP
        // while the pointer is down. Without it the element sat at its stored
        // opacity for the whole gesture and only the main video appeared to
        // react, which is the bug this pair of changes fixes.
        ctx.globalAlpha = livePipTx(pc.id)?.opacity
          ?? sampleKF(pcx.transform?.opacity as never, t - pc.start, 1)
        clipToShape(ctx, pcx.mask, box.hw, box.hh)
        // NEVER `continue` past a PIP whose frame is unavailable — before this
        // layer owned the pixels the renderer baked it, so skipping is a
        // REGRESSION to invisible, and an element that silently isn't there is
        // the worst outcome in this whole subsystem (the same failure as "my
        // stickers just aren't there", which had no error either).
        //
        // Three ways the frame goes missing, one of them routine:
        //   * readyState dips mid-SEEK, every seek, so a scrub would punch a
        //     hole in the PIP for a frame or two;
        //   * the source is still loading right after a project opens;
        //   * the src resolves outside <session>/uploads/, so the files
        //     endpoint 404s (an absolute path from Claude/MCP — sessionFileUrl
        //     falls back to the basename and there is nothing to find).
        // The first two clear themselves; only the third is permanent, and it
        // still EXPORTS correctly, which is exactly why it must not look empty.
        let drawn = false
        if (v && typeof dims === 'object') {
          const innerRot = fr?.rotation ?? 0
          if (Math.abs(innerRot) > 0.001) {
            // Picture turns INSIDE the shape: mirror pip.py's
            // cover -> rotate -> crop. The source rect is taken at zoom 1 /
            // pan 0 because the plan re-applies both — zoom through the
            // enlarged destination, pan as a post-rotation translate, which is
            // where the renderer applies it (folding pan into the source rect
            // would make it slide diagonally once turned).
            const base = pipDrawGeom(dims.w, dims.h, box.hw * 2, box.hh * 2, null)
            const plan = pipInnerPlan(box.hw * 2, box.hh * 2, {
              zoom: fr?.zoom ?? 1, rotation: innerRot,
              x: fr?.x ?? 0, y: fr?.y ?? 0,
            })
            ctx.save()
            ctx.translate(plan.offX, plan.offY)
            ctx.rotate(plan.rotRad)
            drawn = drawPipVideoFrame(ctx, v, pc.src, {
              sx: base.sx, sy: base.sy, sw: base.sw, sh: base.sh,
              dw: plan.destW, dh: plan.destH,
            })
            ctx.restore()
          } else {
            const g = pipDrawGeom(dims.w, dims.h, box.hw * 2, box.hh * 2, fr)
            // Falls back to the LAST frame that decoded rather than to the
            // placeholder: a decoder dip is routine (every paused seek causes
            // one) and one repeated frame is far less alarming than the picture
            // blanking out.
            drawn = drawPipVideoFrame(ctx, v, pc.src, g)
          }
        }
        if (!drawn) {
          // Deliberately a neutral wash, not a guess at the footage: it shows
          // the element's true position, size and SHAPE (it is drawn inside the
          // same clip path) so placement and dragging still work, without
          // claiming to be a frame. Attempting the draw first means a stale
          // frame always wins over this, which is what ffmpeg's own
          // eof_action=repeat does at the edges of a PIP.
          ctx.fillStyle = 'rgba(120,130,150,0.35)'
          ctx.fillRect(-box.hw, -box.hh, box.hw * 2, box.hh * 2)
        }
        ctx.restore()
      }

      pausePipVideosExcept(livePipSrcs)

      // The sticker being dragged paints LAST — see paintOrder's comment: its z
      // is only raised at commit time, so stored order made an older sticker
      // dive under a newer one for the whole gesture.
      // Keep it hoisted for the held phase too, not just while the pointer is
      // down: its z is raised by the same commit we are waiting on, so dropping
      // back to stored order here would make it dive under a neighbour for the
      // hold window and pop back out — the exact flicker paintOrder exists to
      // prevent, just moved a few frames later.
      for (const sk of paintOrder(stickers, dragRef.current?.id ?? pendingRef.current?.id)) {
        const ov = liveOverride(sk.id)
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
        // No ghost wash for a dragged PIP any more: this layer now paints the
        // PIP's real frames (above), so the picture itself follows the pointer
        // and a placeholder rectangle would just sit on top of it.
        ctx.save()
        ctx.translate(sel.cx, sel.cy)
        ctx.rotate(sel.rot)
        ctx.globalAlpha = 1
        // The grip flips below the box when the box sits too close to the top
        // of the canvas for it to be drawn (and therefore clicked) above.
        const rotAt = dv.rotateHandleLocal(
          sel.hh, sel.cy - sel.hh - dv.ROT_GAP - dv.ROT_R < 2)
        dv.drawSelectionChrome(ctx, sel.hw, sel.hh, {
          dragging: !!dragging,
          resizing: !!dragging && d!.mode === 'resize',
          // Offered on the PIP, which is the element the request is about and
          // the one whose shape rotates WITH it in the bake (pip.py masks
          // before rotating). Stickers and text keep the slider only, so a
          // handle is never shown for something the renderer would not turn.
          showRotate: sel.kind === 'pip',
          rotateAt: rotAt,
          rotating: !!dragging && d!.mode === 'rotate',
          // No ✕ on a PIP: Backspace deletes the clip, and a delete handle on
          // the base-adjacent video layer is far too easy to hit by accident
          // while reframing it.
          showDelete: sel.kind !== 'pip',
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

      // 1b) The rotate grip, above the selected PIP's box. Checked before the
      // corner handles: the grip sits outside the box, so nothing else claims
      // that point, but ordering it first keeps the precedence explicit.
      if (selBox && selBox.kind === 'pip') {
        const { lx, ly } = toLocal(px, py, selBox)
        const at = dv.rotateHandleLocal(
          selBox.hh, selBox.cy - selBox.hh - dv.ROT_GAP - dv.ROT_R < 2)
        if (Math.hypot(lx - at.lx, ly - at.ly) <= dv.ROT_HIT) {
          e.preventDefault()
          try { cv.setPointerCapture(e.pointerId) } catch { /* synthetic pointer */ }
          const rot0 = (selBox.rot * 180) / Math.PI
          dragRef.current = {
            id: selBox.id, kind: 'pip', mode: 'rotate',
            cx: selBox.cx, cy: selBox.cy, rot0,
            // Angle of the pointer at grab time. The gesture is a DELTA from
            // it, so grabbing the grip does not snap the box's top to the
            // pointer on the first pixel.
            a0: Math.atan2(py - selBox.cy, px - selBox.cx),
            live: { rotation: rot0 },
          }
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
          // Start the resize from the overlay's CURRENT scale. Defaulting to 1
          // for a non-sticker made a PIP jump to full default size on the first
          // pixel of the drag, since the commit is scale0 * mul.
          const sk = activeStickers(t).find((s) => s.id === selBox.id)
          let scale0 = 1
          if (sk) {
            scale0 = stickerGeom(sk, t, stateRef.current.edl.canvas.w,
                                 stateRef.current.edl.canvas.h,
                                 stateRef.current.width, stateRef.current.height).scale
          } else if (selBox.kind === 'pip') {
            const pc = activePips(t).find((c) => c.id === selBox.id)
            if (pc) {
              scale0 = pipGeom(
                pc as unknown as { start: number; transform?: StickerClip['transform']
                                   mask?: { type?: string } | null; fit?: string },
                t, stateRef.current.edl.canvas, null,
                stateRef.current.width, stateRef.current.height).scale
            }
          }
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
        // ALT-drag on a cropped PIP pans the picture INSIDE its shape, instead
        // of moving the shape on the canvas — the drag equivalent of the panel's
        // pan X/Y sliders ("there should be an alternate for the x/y coordinate
        // for adjusting the video inside the shapes"). Alt rather than a mode
        // toggle so it needs no round trip and cannot be left switched on; the
        // panel spells it out, since a bare modifier is undiscoverable.
        //
        // Only when the element is actually cropped: with the source's own
        // aspect there is no margin to pan within, so the gesture would silently
        // do nothing.
        const pipClip = pick.kind === 'pip'
          ? activePips(t).find((c) => c.id === pick.id)
          : undefined
        const pipCropped = !!pipClip && (
          (pipClip as unknown as { mask?: { type?: string } | null }).mask?.type === 'circle'
          || (pipClip as unknown as { fit?: string }).fit === 'cover')
        if (pipClip && pipCropped && e.altKey) {
          const fr = (pipClip as unknown as {
            framing?: { x?: number; y?: number; zoom?: number; rotation?: number } | null }).framing ?? {}
          dragRef.current = {
            id: pick.id, kind: 'pip-frame', mode: 'move', startMx: px, startMy: py,
            x0: fr.x ?? 0, y0: fr.y ?? 0, xSentinels: [], ySentinels: [],
            live: { x: fr.x ?? 0, y: fr.y ?? 0 },
          }
          return
        }
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
        // Moving the base video is a FRAMING gesture, so it only happens in
        // framing mode. Outside it, a click on the picture selects the clip and
        // nothing else — dragging used to reposition it silently, which is the
        // reported "I can still move the video even though framing is not
        // selected".
        //
        // A cover-fit clip's direct drag is owned by <CropReposition> instead
        // (Preview.tsx mounts it, covering this canvas, once framing opens) — it
        // needs the raw uncropped source and a zoomed-out viewport to show real
        // footage while panning, which this simple CSS-translate-the-baked-frame
        // approximation can't provide (that approximation is exactly what made a
        // cover-fit pan reveal manufactured black instead of real footage).
        //
        // Those two rules leave this path unreachable in practice today, since
        // entering framing sets `cover`. It is kept rather than deleted because
        // it is the correct behaviour for a `contain` clip being framed, and the
        // guard states the intent instead of relying on that coincidence.
        if (!v1DragAllowed(t)) return
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
          // The rotate grip advertises itself, both so the gesture is
          // discoverable without a tooltip and so it is FINDABLE by feel — the
          // same reason the resize handles carry a cursor.
          if (selBox.kind === 'pip') {
            const at = dv.rotateHandleLocal(
              selBox.hh, selBox.cy - selBox.hh - dv.ROT_GAP - dv.ROT_R < 2)
            if (Math.hypot(lx - at.lx, ly - at.ly) <= dv.ROT_HIT) cursor = 'grab'
          }
          const del = dv.deleteHandleLocal(selBox, stateRef.current.width, stateRef.current.height)
          if (cursor === 'default'
              && Math.hypot(lx - del.lx, ly - del.ly) <= dv.DEL_R + 4) {
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
        // The base video reads as draggable ONLY inside framing mode.
        //
        // It used to advertise `move` whenever any v1 clip was on screen, so the
        // cursor promised a gesture that repositions the picture at a moment the
        // user had not asked to reposition anything: "Even though the framing
        // option is not selected, I can still move to the video, which shouldn't
        // happen... As the cursor is changed when it was hovered on the video."
        // The cursor is half the bug — a pointer that says "grab me" is an
        // invitation, so it has to go quiet at exactly the same times the drag
        // does (see the pointerdown handler).
        if (cursor === 'default' && v1DragAllowed(t)) cursor = 'move'
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
      } else if (d.kind === 'pip-frame') {
        // Pan inside the shape. The offsets are normalised to the crop margin,
        // and the box's own size is the natural travel: dragging across the
        // whole shape sweeps the picture from one edge of its margin to the
        // other. Inverted, because dragging the PICTURE right means moving the
        // crop window LEFT — the same sign rule cropLayout documents for v1.
        const box = allBoxes(now()).find((b) => b.id === d.id)
        const spanX = Math.max(20, (box?.hw ?? 60) * 2)
        const spanY = Math.max(20, (box?.hh ?? 60) * 2)
        d.live.x = Math.max(-1, Math.min(1, d.x0 - (px - d.startMx) / spanX * 2))
        d.live.y = Math.max(-1, Math.min(1, d.y0 - (py - d.startMy) / spanY * 2))
        setLivePipFraming({ id: d.id, x: d.live.x, y: d.live.y })
      } else if (d.mode === 'rotate') {
        // Delta from the grab angle, so the box turns WITH the pointer rather
        // than snapping its top to it. Shift snaps to 15°, the increment every
        // office app uses for this handle.
        const a = Math.atan2(py - d.cy, px - d.cx)
        let deg = d.rot0 + ((a - d.a0) * 180) / Math.PI
        if (e.shiftKey) deg = Math.round(deg / 15) * 15
        // Wrap into -180..180: the schema clamps there, so letting the gesture
        // run past it would silently stick at the end instead of coming round.
        deg = ((deg + 180) % 360 + 360) % 360 - 180
        d.live.rotation = deg
      } else if (d.mode === 'move') {
        // The CENTRE is clamped to the canvas, so at worst half the overlay
        // hangs off an edge and half stays on frame. Keeping the WHOLE overlay
        // on frame was tried and reverted — see clampOverlayCentre.
        //
        // Unclamped, a drag could strand a sticker almost entirely off-canvas,
        // leaving a thin vertical band of its artwork down the frame edge —
        // reported as "when i drag the emoji on the right side of the video,
        // then it leaves a phantom color". Measured with 🤣 at x=1900 on a 1920
        // canvas: the overlay canvas's last 16 columns inked #e6c75e→#d6af4d,
        // 224px tall, and the export carried the same band at columns
        // 1912-1919. It reads as a rendering artifact rather than as the
        // sticker, and it is nearly unrecoverable by hand: the selection box
        // and its ✕ are off-canvas too, so the only grab target left is the
        // sliver itself.
        //
        // Clamped HERE, in the gesture, not in set_clip_transform — see
        // clampOverlayCentre for why the tool must stay unclamped.
        const c = clampOverlayCentre(
          d.x0 + (px - d.startMx) * (edl.canvas.w / width),
          d.y0 + (py - d.startMy) * (edl.canvas.h / height),
          edl.canvas,
        )
        d.live.x = c.x
        d.live.y = c.y
        // Feed the layers the CLAMPED offset, or the drawn overlay would keep
        // travelling with the pointer past the edge and then jump back on
        // release — the drag has to show what will be committed.
        setOverlayDrag({
          id: d.id,
          dx: (d.live.x - d.x0) * (width / edl.canvas.w),
          dy: (d.live.y - d.y0) * (height / edl.canvas.h),
          sizeMul: 1,
        })
      } else {
        const dist = Math.hypot(px - d.cx, py - d.cy)
        const mul = dist / d.startDist
        d.live.mul = Math.max(0.1, Math.min(8, mul))
        d.live.scale = Math.max(0.1, Math.min(8, d.scale0 * mul))
        setOverlayDrag({ id: d.id, dx: 0, dy: 0, sizeMul: d.live.mul })
      }
    }

    const holdRotate = (id: string, deg: number) => {
      heldRotRef.current = { id, deg, at: performance.now() }
    }

    const onUp = (e: PointerEvent) => {
      const d = dragRef.current
      if (!d) return
      try { cv.releasePointerCapture(e.pointerId) } catch { /* noop */ }
      if (d.mode === 'rotate') {
        // Held until the commit lands, exactly like a move: clearing here would
        // drop the box back to its stored angle for the dispatch -> refresh
        // round-trip, which is the snap-back resolveLiveOverride exists to stop.
        const deg = Math.round(d.live.rotation)
        holdRotate(d.id, deg)
        dragRef.current = null
        if (deg !== Math.round(d.rot0)) {
          dispatch('set_clip_transform', { clip_id: d.id, rotation: deg })
        }
        return
      }
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
      // Do NOT clear the drag override here — see `holdDrag` below. Nine lines
      // up, the video branch says exactly this and the overlay branch did the
      // opposite: clearing on pointer-up drops the sticker back to its stored
      // (pre-drag) position until the commit round-trips, so a drop visibly
      // bounced to where the drag STARTED and then jumped forward.
      //
      // Measured off the reported screen recording, frame by frame: dropped at
      // x=515, back to x=359 on the very next frame, held there for 8 frames
      // (233 ms), then forward to x=511. That is the ~120 ms refreshSoon()
      // debounce plus the fetch — not a render cost, so it will not "just get
      // faster".
      if (d.kind === 'pip-frame') {
        const nx = Math.round(d.live.x * 100) / 100
        const ny = Math.round(d.live.y * 100) / 100
        if (nx === Math.round(d.x0 * 100) / 100 && ny === Math.round(d.y0 * 100) / 100) {
          setLivePipFraming(null)
          return
        }
        // Held past pointer-up, like every other overlay commit here: released
        // by the [edl] effect once the refreshed EDL carries the value, so the
        // picture does not snap back to its old framing for the round-trip.
        framingPendingRef.current = { id: d.id, x: nx, y: ny }
        if (holdTimerRef.current !== null) clearTimeout(holdTimerRef.current)
        holdTimerRef.current = window.setTimeout(releaseHold, 2000)
        dispatch('set_pip_framing', { clip_id: d.id, x: nx, y: ny })
        return
      }
      if (d.mode === 'move') {
        // A body pointerdown always starts a 'move' drag, so a plain
        // select-click lands here with unchanged coords — skip the commit
        // entirely (an identical-x/y op would still clear the redo stack).
        const moved = Math.round(d.live.x) !== Math.round(d.x0)
          || Math.round(d.live.y) !== Math.round(d.y0)
        if (!moved) { setOverlayDrag(null); return }
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
        const cx = unsentinel(Math.round(d.live.x), d.xSentinels)
        const cy = unsentinel(Math.round(d.live.y), d.ySentinels)
        holdDrag({ id: d.id, x: cx, y: cy })
        dispatch('set_clip_transform', {
          clip_id: d.id, x: cx, y: cy,
          ...(raise ? { raise_to_front: true } : {}),
        })
      } else if (d.kind === 'text') {
        // Text resizes by its style.size (EDL-canvas px) — the same field the
        // Properties panel drives and the server's resolve_size_override reads.
        if (d.size0 <= 0) { setOverlayDrag(null); return }
        const next = Math.round(Math.min(TEXT_SIZE_MAX, Math.max(TEXT_SIZE_MIN, d.size0 * d.live.mul)))
        if (next === Math.round(d.size0)) { setOverlayDrag(null); return }
        holdDrag({ id: d.id, size: next })
        dispatch('set_property', { clip_id: d.id, path: 'style.size', value: next })
      } else {
        const scale = Math.round(d.live.scale * 100) / 100
        holdDrag({ id: d.id, scale })
        dispatch('set_clip_transform', { clip_id: d.id, scale })
      }
    }

    cv.addEventListener('pointerdown', onDown)
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    return () => {
      cancelAnimationFrame(raf)
      releaseHold()          // also kills the pending safety-net timer
      cv.removeEventListener('pointerdown', onDown)
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
    }
  }, [videoEl, dispatch, setSelection])

  return <canvas ref={canvasRef} style={{ position: 'absolute', inset: 0 }} />
}
