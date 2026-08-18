import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'
import { useStore, errorMessage } from '../store'
import { isMediaClip, type Clip } from '../types'
import { TextLayer } from './TextLayer'
import { StickerLayer } from './StickerLayer'
import { CropReposition } from './CropReposition'
import { FrameScrubber, type FrameScrubberHandle } from './FrameScrubber'
import { ErrorBoundary } from './ErrorBoundary'
import { chordLabel } from '../keymap/engine'
import { pipIsClientDrawn, sourcePreviewVideo, releaseSourcePreviewVideos,
         syncPipVideo } from '../lib/pipDraw'
import { liveCssTransform, liveCssFilter, colorGradeOf, sampleKF,
         liveVideoCssApplies } from '../lib/overlay'
import { planSourceDraw, sourcePreviewApplies } from '../lib/sourcePreview'
import { srcDimsFor, sessionFileUrl } from '../lib/media'

/**
 * Preview pane.
 *
 * Realtime strategy:
 *   - The <video> only re-renders on the server when the *video/audio* tracks
 *     change (cuts, trims, adds, music). Text/captions changes draw client-side
 *     in <TextLayer> so they appear instantly with no ffmpeg roundtrip.
 *   - Re-render requests are debounced (300ms quiescence) and cancelled if
 *     superseded, so rapid edits collapse to one server call.
 */
export function Preview() {
  const sid = useStore((s) => s.sessionId)
  const edl = useStore((s) => s.edl)
  const previewHash = useStore((s) => s.previewHash)
  const renderPreview = useStore((s) => s.renderPreview)
  const playhead = useStore((s) => s.playhead)
  const isPlaying = useStore((s) => s.isPlaying)
  const setPlayhead = useStore((s) => s.setPlayhead)
  const setPlaying = useStore((s) => s.setPlaying)
  const liveTransform = useStore((s) => s.liveTransform)
  const setLiveTransform = useStore((s) => s.setLiveTransform)
  const framing = useStore((s) => s.framing)
  const liveFilter = useStore((s) => s.liveFilter)
  const setLiveFilter = useStore((s) => s.setLiveFilter)
  const selection = useStore((s) => s.selection)

  const ref = useRef<HTMLVideoElement>(null)
  const [rendering, setRendering] = useState(false)

  // What the render CURRENTLY ON SCREEN has baked into it, latched when a live
  // transform gesture begins and held until the override clears.
  //
  // The live CSS transform composes with the picture underneath, so it has to be
  // expressed relative to this (see lib/overlay.liveCssTransform, which explains
  // the reported "rotate again from the last angle and the preview is wrong").
  //
  // It MUST be latched rather than read from the live EDL each render: the commit
  // lands ~120ms before the render carrying it does, so re-reading would make
  // `baked` equal the NEW value while the video still shows the OLD frame — the
  // delta would collapse to zero and the preview would snap back to the pre-drag
  // look for the whole round-trip. It clears itself the moment the override goes
  // away (onLoadedData, or the 8s abandonment net), so the next gesture
  // re-latches against whatever is on screen by then — no separate teardown.
  //
  // Latched during RENDER, not in an effect: an effect runs after paint, so the
  // first frame of a gesture would compose against a stale/absent baked value and
  // show a one-frame jump — the very artifact this is fixing. The write is a
  // compare-then-assign, so a StrictMode double-render is idempotent.
  //
  // KEYED ON previewHash AS WELL AS THE CLIP, which is what makes the delta
  // correct by construction rather than by timing. `previewHash` identifies the
  // render the <video> is showing, so when a new one lands the latch is refreshed
  // from the EDL and the delta is recomputed against the picture actually on
  // screen. Without that the latch could outlive the frame it described, and the
  // override would keep subtracting a rotation the render no longer has —
  // reported as "when i re rotated the video to 0 degrees, the video got cropped",
  // where the panel reads 0 and the picture is visibly rotated. That is precisely
  // rotate(0-68) applied to a render already at 0.
  //
  // Clearing the override was previously the ONLY protection, and it hangs off
  // the <video>'s onLoadedData (plus an 8s abandonment net) — i.e. on an event
  // firing, which is not something a correctness argument should rest on. The
  // renderer itself was verified innocent first: over HTTP on a clean session,
  // rotation 0 -> 68 -> 0 restores the frame exactly (white 100.0% -> 60.9% ->
  // 100.0%, identical corners), so nothing is baked permanently and no cache is
  // stale. The only way the picture can disagree with the EDL is this override.
  //
  // …AND THE HASH THAT MATTERS IS THE ONE ON SCREEN, NOT THE ONE REQUESTED.
  // `previewHash` is set when the render RESPONSE arrives (store.ts), but the
  // <video> goes on displaying the PREVIOUS frame until it has loaded the new
  // src. Latching in that window paired the NEW transform with the OLD picture,
  // so the delta came out too small and the preview under-rotated — the second
  // half of "when I again rotate the screen from the last placed angle, then it
  // doesn't work according to the angle rotation". Measured: rotate to -16,
  // release, then drag to -30 before the reload lands, and the element gets
  // rotate(-14deg) over a frame still baked at 0 — the picture reads -14 while
  // the panel says -30. Settled, the same sequence is correct, which is why it
  // presents as intermittent and why "it works fine when I leave the toggle".
  //
  // So the snapshot is taken when the hash CHANGES (the response for it has
  // just arrived, so the EDL still describes it) and only PROMOTED to
  // "this is what you are looking at" when the <video> reports it loaded.
  const pendingTxRef = useRef<{ hash: string | null; clipId: string | null
                                scale: number; rotation: number; opacity: number } | null>(null)
  const screenTxRef = useRef<{ hash: string | null; clipId: string | null
                               scale: number; rotation: number; opacity: number } | null>(null)

  const sampleTxOf = (clipId: string | null) => {
    let sc = 1, rot = 0, opa = 1
    if (!clipId) return { scale: sc, rotation: rot, opacity: opa }
    for (const tk of edl?.tracks ?? []) {
      const found = tk.clips.find((k) => (k as { id?: string }).id === clipId)
      if (!found) continue
      const c = found as unknown as {
        start?: number
        transform?: { scale?: never; rotation?: never; opacity?: never }
      }
      const localT = playhead - (c.start ?? 0)
      sc = sampleKF(c.transform?.scale, localT, 1)
      rot = sampleKF(c.transform?.rotation, localT, 0)
      opa = sampleKF(c.transform?.opacity, localT, 1)
      break
    }
    return { scale: sc, rotation: rot, opacity: opa }
  }

  // Snapshot for the SELECTED clip: a transform drag can only start on one, so
  // that is the only clip whose baked pose is ever needed, and snapshotting the
  // whole timeline every render would be waste.
  if (pendingTxRef.current?.hash !== previewHash
      || pendingTxRef.current?.clipId !== selection) {
    pendingTxRef.current = { hash: previewHash, clipId: selection,
                             ...sampleTxOf(selection) }
  }

  const bakedTxRef = useRef<{ clipId: string; hash: string | null
                              scale: number; rotation: number; opacity: number } | null>(null)
  if (!liveTransform) {
    bakedTxRef.current = null
  } else if (bakedTxRef.current?.clipId !== liveTransform.clipId
             || bakedTxRef.current?.hash !== previewHash) {
    // First frame of a gesture on this clip: prefer the pose recorded for the
    // render actually ON SCREEN. Fall back to sampling the live EDL when there
    // is no such record yet (first render of a session, or a clip that was not
    // the selection when the frame landed) — that is the old behaviour, which
    // is correct whenever the EDL and the picture agree.
    const onScreen = screenTxRef.current?.clipId === liveTransform.clipId
      ? screenTxRef.current : null
    const tx = onScreen ?? sampleTxOf(liveTransform.clipId)
    bakedTxRef.current = { clipId: liveTransform.clipId, hash: previewHash,
                           scale: tx.scale, rotation: tx.rotation, opacity: tx.opacity }
  }
  // WHICH LANE is being dragged decides whether this element may be touched at
  // all. The `<video>` shows the composited render, and in preview that is v1's
  // picture plus whatever is still baked — a PIP is NOT in it (the browser draws
  // it, see pipDraw/StickerLayer). So a CSS transform or opacity here is only a
  // live stand-in for a **v1** clip. Applying it for a PIP dims/moves the main
  // video instead of the element the slider names: reported as "when I lower the
  // opacity for pip, the main video's opacity got also lower". Scale and
  // rotation had the identical defect on the same sliders — the same wrong
  // element, just less obvious than a fade.
  //
  // The live preview for a PIP belongs where its pixels do: StickerLayer's paint
  // loop (`livePipTx`). Both halves are needed — gating here alone would leave
  // the slider with no live feedback at all, which reads as the control being
  // dead until the commit lands.
  const liveTxTrackId = liveTransform && edl
    ? (edl.tracks.find((tk) => tk.clips.some(
        (k) => (k as { id?: string }).id === liveTransform.clipId))?.id ?? null)
    : null
  const liveCss = liveTransform && liveVideoCssApplies(liveTxTrackId)
    ? liveCssTransform(liveTransform, bakedTxRef.current ?? { scale: 1, rotation: 0, opacity: 1 })
    : null


  // The colour grade the visible render already has, latched the same way and for
  // the same reason — CSS brightness/contrast/saturate all multiply what is
  // underneath, so absolute values compounded on every drag after the first.
  // Keyed on previewHash too, for the same reason as the transform latch above.
  const bakedFxRef = useRef<{ clipId: string; hash: string | null
                              brightness: number; contrast: number; saturation: number } | null>(null)
  if (!liveFilter) {
    bakedFxRef.current = null
  } else if (bakedFxRef.current?.clipId !== liveFilter.clipId
             || bakedFxRef.current?.hash !== previewHash) {
    let g = { brightness: 0, contrast: 1, saturation: 1 }
    for (const tk of edl?.tracks ?? []) {
      const found = tk.clips.find((k) => (k as { id?: string }).id === liveFilter.clipId)
      if (found) { g = colorGradeOf(found); break }
    }
    bakedFxRef.current = { clipId: liveFilter.clipId, hash: previewHash, ...g }
  }
  const liveFx = liveFilter
    ? liveCssFilter(liveFilter, bakedFxRef.current ?? { brightness: 0, contrast: 1, saturation: 1 })
    : null
  const [error, setError] = useState<string | null>(null)
  const [boxSize, setBoxSize] = useState({ w: 0, h: 0 })
  const wrapRef = useRef<HTMLDivElement>(null)

  // --- source-based live transform preview -------------------------------
  //
  // See lib/sourcePreview.ts for the whole argument. Short version: the bake
  // rotates IN PLACE and cuts the corners, so once a clip carries a non-zero
  // rotation the frame on screen no longer contains the pixels needed to show
  // any OTHER angle — least of all 0. Drawing from the raw source at the target
  // angle is the only truthful preview, so it takes over exactly there.
  const srcPreview = useMemo(() => {
    if (!liveTransform || !edl || !sid || boxSize.w <= 0) return null
    const track = edl.tracks.find(
      (t) => t.clips.some((k) => (k as { id?: string }).id === liveTransform.clipId))
    const found = track?.clips.find((k) => (k as { id?: string }).id === liveTransform.clipId)
    if (!track || !found || !isMediaClip(found)) return null
    const c = found as unknown as {
      id: string; src: string; start?: number; in?: number; fit?: string
      transform?: Record<string, unknown>
    }
    const tx = c.transform ?? {}
    const kf = (v: unknown) => !!v && typeof v === 'object' && Array.isArray(
      (v as { keyframes?: unknown }).keyframes)
    const dims = srcDimsFor(c.src, sid)
    const baked = bakedTxRef.current?.rotation ?? 0
    if (!sourcePreviewApplies({
      trackId: track.id, fit: c.fit, bakedRotation: baked,
      keyframed: kf(tx.rotation) || kf(tx.scale) || kf(tx.x) || kf(tx.y),
      hasDims: typeof dims === 'object',
    })) return null
    const localT = playhead - (c.start ?? 0)
    // The gesture publishes only the field being dragged; every other field
    // keeps its stored value, so the preview shows the whole transform rather
    // than resetting the ones the user is not touching.
    const plan = planSourceDraw(
      dims as { w: number; h: number }, edl.canvas, boxSize,
      {
        scale: liveTransform.scale ?? sampleKF(tx.scale as never, localT, 1),
        rotation: liveTransform.rotation ?? sampleKF(tx.rotation as never, localT, 0),
        x: sampleKF(tx.x as never, localT, 0),
        y: sampleKF(tx.y as never, localT, 0),
      })
    if (!plan) return null
    const url = sessionFileUrl(c.src, sid)
    if (!url) return null
    // Opacity travels with the plan. This canvas REPLACES the <video> while it
    // is up, so anything it fails to reproduce simply vanishes from the preview
    // — and it was not applying opacity at all, so a dimmed clip jumped to full
    // brightness the moment a rotation drag took over, and the opacity slider
    // did nothing at all while a rotated clip was selected. The live value wins
    // over the stored one so the slider stays live even here.
    const opacity = liveTransform.opacity ?? sampleKF(tx.opacity as never, localT, 1)
    return { plan, url, src: c.src, start: c.start ?? 0, in: c.in ?? 0, opacity }
  }, [liveTransform, edl, sid, boxSize, playhead])

  const srcCanvasRef = useRef<HTMLCanvasElement>(null)
  // Has the source canvas actually PAINTED a frame yet? The <video> stays
  // visible until it has. Hiding the video the instant the view is *armed* means
  // a canvas that cannot draw shows solid black over a perfectly good picture —
  // which is precisely what the shared-element bug produced, and the reason it
  // presented as "the video gets blacked out" rather than as a stale frame.
  // Belt and braces with the pool split above: any future reason the decoder is
  // not ready (a slow load, a seek settling) now degrades to the composited
  // preview instead of to black.
  const srcDrawnRef = useRef(false)
  const [srcDrawn, setSrcDrawn] = useState(false)
  useEffect(() => {
    if (!srcPreview) {
      srcDrawnRef.current = false
      setSrcDrawn(false)
      // THE GESTURE IS OVER — hand the decoder back (WebKit's concurrent
      // media-element budget is far tighter than Blink's).
      //
      // Deliberately HERE and not in the effect's cleanup: `srcPreview` is a
      // fresh object on every slider input, so the effect tears down and re-runs
      // ~30x/second during a drag. Releasing there would destroy and reload the
      // element on every frame, readyState would never reach HAVE_CURRENT_DATA,
      // and the canvas would paint nothing — reintroducing the exact blackout
      // this element split was added to fix, by way of "cleaning up".
      releaseSourcePreviewVideos()
      return
    }
    const cv = srcCanvasRef.current
    const ctx = cv?.getContext('2d')
    if (!cv || !ctx) return
    const fps = edl?.canvas.fps ?? 30
    let raf = 0
    const draw = () => {
      const dpr = window.devicePixelRatio || 1
      const wantW = Math.max(1, Math.round(boxSize.w * dpr))
      const wantH = Math.max(1, Math.round(boxSize.h * dpr))
      if (cv.width !== wantW || cv.height !== wantH) {
        cv.width = wantW; cv.height = wantH
        cv.style.width = `${boxSize.w}px`; cv.style.height = `${boxSize.h}px`
      }
      // Its OWN element, never the PIP pool's — see sourcePreviewVideo.
      const v = sourcePreviewVideo(srcPreview.src, srcPreview.url)
      syncPipVideo(v, playhead, srcPreview.start, srcPreview.in, fps)
      if (v.readyState < 2) {
        // NOTHING TO DRAW. Do not paint black over the <video> underneath.
        // Before the first successful frame, clear to TRANSPARENT so the
        // composited preview shows through; after it, leave the last painted
        // frame alone — one stale frame beats a hole, the same rule the PIP's
        // LAST_FRAME fallback follows.
        if (!srcDrawnRef.current) {
          ctx.setTransform(1, 0, 0, 1, 0, 0)
          ctx.clearRect(0, 0, cv.width, cv.height)
          ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
        }
        raf = requestAnimationFrame(draw)
        return
      }
      // Clear in DEVICE pixels — same rule as StickerLayer's draw loop: at a
      // fractional dpr the backing store is wider than `box * dpr`, and a
      // CSS-space clear leaves the far column holding the previous frame.
      ctx.setTransform(1, 0, 0, 1, 0, 0)
      ctx.clearRect(0, 0, cv.width, cv.height)
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      // The canvas letterbox is black in the bake, so it is black here too.
      ctx.fillStyle = '#000'
      ctx.fillRect(0, 0, boxSize.w, boxSize.h)
      {
        const p = srcPreview.plan
        ctx.save()
        ctx.beginPath()
        ctx.rect(0, 0, boxSize.w, boxSize.h)
        ctx.clip()
        // rotate -> zoom -> pan, the renderer's order (planSourceDraw explains
        // why it is not interchangeable).
        ctx.translate(boxSize.w / 2 + p.panX, boxSize.h / 2 + p.panY)
        ctx.scale(p.zoom, p.zoom)
        ctx.rotate(p.rotRad)
        // Over the black fill above, globalAlpha IS the fade-toward-black the
        // renderer now performs (compositor's colorchannelmixer on gbrp) — the
        // two have to agree or this preview lies about brightness.
        ctx.globalAlpha = Math.max(0, Math.min(1, srcPreview.opacity))
        try {
          ctx.drawImage(v, -p.drawW / 2, -p.drawH / 2, p.drawW, p.drawH)
          // Only NOW is it safe to hide the <video> — see srcDrawnRef.
          if (!srcDrawnRef.current) { srcDrawnRef.current = true; setSrcDrawn(true) }
        } catch { /* decoder had nothing this tick */ }
        ctx.globalAlpha = 1
        ctx.restore()
      }
      raf = requestAnimationFrame(draw)
    }
    draw()
    return () => cancelAnimationFrame(raf)
  }, [srcPreview, boxSize, playhead, edl])
  // WebCodecs scrubber: shown during seek, hidden during playback so frames
  // come from <video> at full smoothness.
  const scrubberRef = useRef<FrameScrubberHandle>(null)
  const [scrubbing, setScrubbing] = useState(false)
  // Mirror of `scrubbing` for the sync effect, which must know "is a scrub
  // already in progress?" WITHOUT taking `scrubbing` as a dependency — doing
  // that would re-run the effect on its own setScrubbing and re-issue the
  // same seek in a loop.
  const scrubbingRef = useRef(false)
  const setScrubbingBoth = (v: boolean) => { scrubbingRef.current = v; setScrubbing(v) }
  const scrubTimer = useRef<number | null>(null)
  // Cancels a pending "wait for the video's native seek to settle before
  // revealing it" listener from a superseded scrub (see the sync effect).
  const scrubSeekedCleanup = useRef<(() => void) | null>(null)
  // A <video> seek that has been DEFERRED so it can run hidden behind the
  // opaque scrubber canvas instead of on screen (see the sync effect).
  const pendingSeekRef = useRef<number | null>(null)
  // Authoritative playback time (seconds). The rAF clock owns this while
  // playing; kept in a ref so the loop never reads a stale `playhead` closure.
  const clockRef = useRef(0)
  // Previous isPlaying value, so the sync effect below can tell a genuine
  // playing→paused transition apart from an ordinary playhead move.
  const prevIsPlayingRef = useRef(isPlaying)

  // A fingerprint that changes only for video-relevant edits. Text edits do
  // NOT change this, so the server preview is reused while client overlays
  // update in real time.
  //
  // Serializes the WHOLE clip object on video/audio-family tracks rather than
  // hand-picking fields (id/src/in/out/start): the backend Clip schema also
  // carries speed, effects (color grade, chromakey, mask…), transform
  // (x/y/scale/rotation/opacity, incl. keyframes) and audio (gain/fade/mute),
  // which types.ts's frontend Clip interface doesn't declare — Properties.tsx
  // reaches them via `as unknown as {...}` casts. A hand-picked field list
  // silently goes stale every time a new video-affecting property is added
  // (that's exactly how speed/color/transform/audio edits used to commit to
  // the EDL but never trigger a preview re-render). Hashing the full clip
  // mirrors how the backend itself decides "did anything render-relevant
  // change" — edl.hash() in schema.py hashes the entire EDL, not a field
  // subset — so this fingerprint can't drift out of sync with the schema again.
  const videoFingerprint = useMemo(() => {
    if (!edl) return ''
    // Sticker tracks are EXCLUDED, exactly like text: StickerLayer now draws
    // every sticker client-side each frame and build_overlay_chain(preview)
    // no longer bakes them (see StickerLayer's pixel-ownership rule). Leaving
    // them in would fire a full ffmpeg re-render for an edit whose result is
    // already on screen — and it was that re-render round-trip which produced
    // the "sticker disappears, then leaves a copy at the old position" gap.
    // NOTE: this is only safe while the preview genuinely skips stickers. If
    // baking ever comes back, sticker tracks must come back here too, or
    // sticker edits stop producing any visual result at all.
    const vidTracks = edl.tracks.filter(t =>
      t.type === 'video' || t.type === 'audio' || t.type === 'music' || t.type === 'vo')
    return JSON.stringify({
      canvas: edl.canvas,
      // Track-LEVEL props matter too: transitions and mute live on the track,
      // not a clip — omitting them left the preview stale after adding a
      // transition (surfaced the day transitions got a UI). `z` is the
      // compositing order (PIP/sticker stacking) — also render-relevant.
      tracks: vidTracks.map(t => ({
        id: t.id,
        z: t.z,
        muted: t.muted,
        transitions: (t as unknown as { transitions?: unknown }).transitions,
        // A PIP lane's clips are reduced to what still affects the RENDER: its
        // audio (pip.py keeps mixing that) and the timing that positions it.
        // Placement, size, shape and framing are the client's now — pip.py's
        // `preview` branch skips baking the picture and StickerLayer paints it
        // — so including them fired a full ffmpeg re-render for a change that
        // was already on screen. That round-trip IS the reported lag: "the
        // video doesn't follow the blue box… it reacts very late".
        //
        // Exactly the same reduction, and the same caveat, as the sticker
        // tracks above: only safe while the preview genuinely skips the PIP
        // picture. If baking ever returns, restore the whole clip here or PIP
        // edits stop producing any visual result.
        clips: t.type === 'video' && t.id !== 'v1'
          ? t.clips.map((c) => {
            const k = c as unknown as {
              id: string; start: number; in?: number; out?: number
              speed?: unknown; audio?: unknown; src?: string; chromakey?: unknown
            }
            // A chromakey'd PIP is the one kind still baked in preview (see
            // pipIsClientDrawn), so it must keep its FULL clip here — reducing
            // it would mean moving or resizing it changed nothing on screen at
            // all, since no client draw is coming to show it.
            if (!pipIsClientDrawn(k)) return c
            return { id: k.id, src: k.src, start: k.start, in: k.in, out: k.out,
                     speed: k.speed, audio: k.audio }
          })
          : t.clips,
      })),
    })
  }, [edl])

  // Debounced + abortable preview render
  const debounceRef = useRef<number | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  useEffect(() => {
    if (!sid || !edl?.duration) return
    if (debounceRef.current) window.clearTimeout(debounceRef.current)
    debounceRef.current = window.setTimeout(() => {
      // Cancel any in-flight request
      abortRef.current?.abort()
      const ac = new AbortController()
      abortRef.current = ac
      setRendering(true)
      setError(null)
      renderPreview()
        .catch((e) => {
          if (ac.signal.aborted) return
          // `String(e)` pasted the whole error envelope — status line, request
          // id and a 400-char raw ffmpeg stderr dump — into the preview pane as
          // a wall of red text (see the tester screenshots). errorMessage()
          // unwraps the same body down to the sentence written for the user.
          setError(errorMessage(e))
          // A failed render means the <video> src never changes, so
          // onLoadedData (which clears liveTransform/liveFilter) never fires
          // either — fail fast instead of leaving the CSS preview stuck
          // for the full safety-net timeout.
          setLiveTransform(null)
          setLiveFilter(null)
        })
        .finally(() => {
          if (ac.signal.aborted) return
          setRendering(false)
        })
    }, 250)
    return () => {
      if (debounceRef.current) window.clearTimeout(debounceRef.current)
    }
  }, [sid, videoFingerprint, edl?.duration, renderPreview, setLiveTransform, setLiveFilter])

  // Safety net for the live-transform/live-filter CSS previews (see the
  // <video> element's onLoadedData below): if the expected re-render never
  // lands — the render fails, gets aborted, or the fingerprint didn't
  // actually change — nothing would otherwise clear them, leaving the CSS
  // override applied forever (a stuck, wrong-looking preview is worse than a
  // brief revert). 250ms debounce + typical render + load latency comfortably
  // fits in 8s; anything still set after that is treated as abandoned.
  useEffect(() => {
    if (!liveTransform && !liveFilter) return
    const t = window.setTimeout(() => { setLiveTransform(null); setLiveFilter(null) }, 8000)
    return () => window.clearTimeout(t)
  }, [liveTransform, liveFilter, setLiveTransform, setLiveFilter])

  // Track preview box size for the text overlay layer
  useEffect(() => {
    if (!wrapRef.current) return
    const el = wrapRef.current
    const update = () => {
      const v = ref.current
      if (!v || !edl) return
      // Letterbox to fit canvas aspect within wrapper
      const canvasAspect = edl.canvas.w / edl.canvas.h
      const boxW = el.clientWidth
      const boxH = el.clientHeight
      const wrapAspect = boxW / boxH
      let w: number, h: number
      if (wrapAspect > canvasAspect) {
        h = boxH
        w = Math.round(h * canvasAspect)
      } else {
        w = boxW
        h = Math.round(w / canvasAspect)
      }
      setBoxSize({ w, h })
    }
    const ro = new ResizeObserver(update)
    ro.observe(el)
    update()
    return () => ro.disconnect()
  }, [edl])

  const playbackRate = useStore((s) => s.playbackRate)

  // Drive video play/pause + seek + JKL shuttle from store
  useEffect(() => {
    if (!ref.current) return
    // HTMLVideoElement supports playbackRate but only positive values; for
    // reverse we'd need WebCodecs. For now, treat negative as paused-with-seek.
    if (playbackRate > 0) {
      ref.current.playbackRate = Math.min(4, playbackRate)
    } else {
      ref.current.playbackRate = 1
    }
    if (isPlaying && playbackRate > 0) ref.current.play().catch(() => {})
    else ref.current.pause()
  }, [isPlaying, playbackRate])

  // One frame of the project's timebase, in seconds. Several thresholds below
  // are "did the displayed frame change?" questions, and a fixed 0.05s answered
  // them wrongly for a 30fps project (a frame is 0.033s).
  const frameDur = 1 / Math.max(1, edl?.canvas?.fps ?? 30)

  useEffect(() => {
    const v = ref.current
    if (!v) return

    // Was this run triggered by a genuine playing→paused transition (hitting
    // Space/the transport button), rather than an ordinary playhead move?
    const justPaused = prevIsPlayingRef.current && !isPlaying
    prevIsPlayingRef.current = isPlaying

    // Is the frame-exact scrubber available to cover the <video>'s own seek?
    const scrubberReady = !isPlaying && !!scrubberRef.current?.isReady()

    // Playback is starting (or already running) with a paused-scrub seek still
    // deferred — land it NOW, before anything below reads currentTime and
    // before audio/video resume from a stale position. Assigning currentTime
    // updates the getter synchronously (the seek itself completes later), so
    // the gap check further down sees the new value and won't seek twice.
    if (isPlaying && pendingSeekRef.current !== null) {
      const target = pendingSeekRef.current
      pendingSeekRef.current = null
      if (v.readyState >= 2 && Math.abs(v.currentTime - target) > 0.02) {
        try { v.currentTime = target } catch { /* non-fatal */ }
      }
    }

    if (justPaused && v.readyState >= 2 && !v.seeking &&
        Math.abs(v.currentTime - playhead) < 0.5) {
      // The video just stopped wherever the browser's own decode pipeline
      // naturally landed. The rAF clock (below) only samples currentTime
      // once per animation frame, so by the time `v.pause()` (the play/pause
      // effect above) actually runs, the video can have advanced a little
      // past the last-sampled `playhead` — forcing `v.currentTime = playhead`
      // in the general branch below would then visibly REWIND the video by
      // that gap on every single pause (reported as "the video stutters and
      // rewinds for a moment when I hit pause or move the playhead"). The
      // video's own stopped position is the ground truth here, not the
      // store's last sample, so pull the playhead FROM the video instead of
      // forcing the video back to a stale one — nothing visibly moves.
      // Bounded to a small gap so an unrelated large jump (e.g. the
      // end-of-timeline clamp, which already set playhead deliberately)
      // still falls through to the general branch below unchanged.
      clockRef.current = v.currentTime
      if (Math.abs(v.currentTime - playhead) > 0.001) {
        try { setPlayhead(v.currentTime) } catch { /* non-fatal */ }
      }
    } else {
      // Sync the <video> to an EXTERNAL playhead move (scrub while paused, or a
      // deliberate jump during playback). While playing, the rAF clock already
      // mirrors the video, so only a large gap warrants a seek — small free-run
      // drift must not trigger a per-frame seek storm.
      const gap = Math.abs(v.currentTime - playhead)
      // Don't re-seek an element that is still servicing the previous seek or
      // hasn't got data yet. While playing, this effect re-runs on every rAF
      // playhead tick, so a stalled/ended/reloading <video> used to get a fresh
      // `currentTime =` write ~60x a second, which keeps it stalled (each write
      // restarts the seek) and shows as the video "lagging" behind a timer that
      // races ahead. HAVE_CURRENT_DATA(2) is the point a seek is honoured
      // rather than queued-and-discarded. Scoped to the <video> seek only — the
      // WebCodecs scrubber below has its own readiness check and must still run.
      const canSeekVideo = !v.seeking && v.readyState >= 2
      if (scrubberReady) {
        // DEFER the <video> seek instead of running it here.
        //
        // `currentTime = t` makes the browser jump to the nearest preceding
        // keyframe and decode forward to the target — and Chrome PAINTS those
        // intermediate frames. On a 2s-GOP preview that is a visible
        // mini-playback burst on every scrub: the exact "stutter when I move
        // the playhead". The scrubber canvas exists to hide precisely that,
        // but it can't when the seek is started synchronously here and the
        // canvas only becomes opaque a frame later (plus its 60ms fade).
        //
        // So: while the frame-exact scrubber is available, nothing touches the
        // <video> during the scrub. The canvas (primed with the frame already
        // on screen, then repainted with the exact target frame) is what the
        // user sees, and the <video>'s messy seek is run by the quiet-timer
        // below — behind an already-opaque canvas — and revealed only once
        // 'seeked' confirms it settled. A continuous drag now also costs ONE
        // <video> seek at the end instead of one per pointer move.
        // Threshold is a QUARTER FRAME, not 0.05s. The canvas is temporary —
        // it fades out and hands the picture back to the <video> — so any move
        // big enough to change the displayed frame must leave a seek behind
        // for the <video> to land on, or the hand-back visibly undoes the
        // move. At 0.05s a single-frame step (0.033s at 30fps) fell under the
        // bar: the canvas showed the stepped frame, no seek was queued, and
        // 250ms later the canvas hid and the picture snapped back to where it
        // started. Measured on a frame-numbered fixture: canvas frame 165,
        // video still on 163, `currentTime` never moved for the whole gesture.
        // Sub-quarter-frame moves still skip, which is what keeps the <video>'s
        // own settle echo from queueing a pointless seek.
        if (gap > frameDur * 0.25) {
          pendingSeekRef.current = playhead
          clockRef.current = playhead   // keep the clock in step with the jump
        }
      } else if (canSeekVideo && gap > (isPlaying ? 0.35 : 0.05)) {
        // A failed/odd <video> can throw on a seek — never let that break the UI.
        // Note: this is the GENERAL sync path (external scrubs, jumps, and the
        // Space-key replay-from-end command — which has no <video> ref of its
        // own and relies entirely on this effect plus the rAF clock's TRUST_TOL
        // check). The transport BUTTON's onClick additionally does a synchronous
        // currentTime/clockRef rewind as defense-in-depth for its own path; this
        // effect's async seek is what the keyboard path depends on exclusively.
        // Reached whenever the scrubber is unavailable (WebCodecs/mp4box
        // failed, still loading) — then a raw <video> seek is still far better
        // than not scrubbing at all.
        try { v.currentTime = playhead } catch { /* non-fatal */ }
        clockRef.current = playhead   // keep the clock in step with the jump
      }
    }

    // While paused, also drive the WebCodecs scrubber so frame-step keys land
    // on the exact frame even when the underlying <video> snapped to a keyframe.
    //
    // Two things must NOT take the canvas, because for them it can only add a
    // visible transition to a picture that is already correct:
    //
    //   * the pause itself. `justPaused` above deliberately pulls the playhead
    //     FROM the video, so at that moment the <video> is displaying exactly
    //     the right frame — but `playhead` in this closure is still the
    //     pre-pause sample, and seeking the scrubber to it painted the frame
    //     BEFORE the one on screen. Measured: the video stopped on frame 55,
    //     the canvas faded up on 54, and 230ms later faded back to 55. That
    //     one-frame flinch is "the video stutters a little after I pause".
    //   * a playhead move too small to change the displayed frame — the
    //     <video>'s own settle echo, which lands a fraction of a frame from
    //     the target. (onTimeUpdate already drops most of these; this is the
    //     backstop for the ones that arrive by another route.)
    //
    // A real scrub always passes: even a single-frame step leaves the <video>
    // a full frame away, and every later move of a drag already holds the
    // canvas.
    const covered = scrubbingRef.current || pendingSeekRef.current !== null
      || Math.abs(v.currentTime - playhead) > frameDur * 0.25
    if (scrubberReady && !justPaused && covered) {
      // Prime the canvas with the frame that is ALREADY on screen before it is
      // revealed, so the reveal itself is invisible (identical pixels) — the
      // canvas then swaps once to the exact target frame when the WebCodecs
      // walk lands. Only when starting a scrub: mid-scrub the canvas already
      // holds a decoded exact frame, and the <video> underneath is stale, so
      // re-priming would visibly step backwards.
      if (!scrubbingRef.current) scrubberRef.current!.prime(v)
      setScrubbingBoth(true)
      scrubberRef.current!.seek(playhead).catch(() => {})
      if (scrubTimer.current) window.clearTimeout(scrubTimer.current)
      scrubSeekedCleanup.current?.()
      scrubSeekedCleanup.current = null
      // Once the scrub goes quiet: run the deferred <video> seek behind the
      // opaque canvas, and only drop the canvas after 'seeked' confirms the
      // element settled. Revealing it any earlier puts the keyframe-then-
      // decode-forward artifact back on screen — the thing this whole path
      // exists to hide.
      scrubTimer.current = window.setTimeout(() => {
        const vid = ref.current
        if (!vid) { setScrubbingBoth(false); return }
        const target = pendingSeekRef.current
        pendingSeekRef.current = null
        const needsSeek = target !== null && vid.readyState >= 2
          && Math.abs(vid.currentTime - target) > 0.02
        if (!needsSeek && !vid.seeking) { setScrubbingBoth(false); return }
        let done = false
        // Guard timer: a <video> that never fires 'seeked' (decode stall,
        // src swapped out from under us) must not strand the canvas opaque
        // forever — a frozen preview is worse than a brief artifact.
        const guard = window.setTimeout(() => finish(), 2000)
        function finish() {
          if (done) return
          done = true
          vid!.removeEventListener('seeked', finish)
          window.clearTimeout(guard)
          scrubSeekedCleanup.current = null
          setScrubbingBoth(false)
        }
        vid.addEventListener('seeked', finish)
        // Supersede (a newer scrub started): detach without hiding, so the
        // new scrub owns the canvas.
        scrubSeekedCleanup.current = () => {
          done = true
          vid.removeEventListener('seeked', finish)
          window.clearTimeout(guard)
        }
        if (needsSeek) {
          try { vid.currentTime = target! } catch { finish() }
        }
      }, 250)
    } else if (isPlaying) {
      scrubSeekedCleanup.current?.()
      scrubSeekedCleanup.current = null
      if (scrubTimer.current) { window.clearTimeout(scrubTimer.current); scrubTimer.current = null }
      setScrubbingBoth(false)
    }
  }, [playhead, isPlaying, setPlayhead, frameDur])

  // When playback STARTS with the playhead freshly at 0 (a replay-from-end via
  // the Space key, which rewinds through the store's replayFromStart), reset
  // the <video> to match. The rAF clock's TRUST_TOL proximity check is what
  // actually prevents a stale currentTime from re-triggering the end-clamp on
  // this path (same as it does for the button, above) — this synchronous
  // reset is defense in depth for the keyboard path specifically, giving it
  // some parity with the button's own rewind even though the keymap layer has
  // no <video> ref of its own to act on directly. It costs nothing: a genuine
  // no-op whenever currentTime is already near 0.
  useEffect(() => {
    if (!isPlaying) return
    const v = ref.current
    if (v && playhead === 0 && v.currentTime > 0.05) {
      try { v.currentTime = 0 } catch { /* non-fatal */ }
      clockRef.current = 0
    }
    // Intentionally runs only on the isPlaying rising edge; depending on
    // playhead here would re-fire every frame during playback.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isPlaying])

  // Playback clock — decoupled from frame rendering.
  //
  // The clock used to be driven by `requestVideoFrameCallback`, which only
  // fires when the <video> actually DECODES a frame. If the preview file can't
  // decode (an odd/torn mp4), rVFC never fires and the playhead freezes even
  // though playback is "running" — the timeline and time readout just stop.
  //
  // Now a rAF wall clock owns the playhead while playing. When the <video> is
  // genuinely advancing we follow its `currentTime` (exact A/V sync); when it
  // stalls or can't render we free-run on wall time so the playhead, time
  // readout and red timeline line keep moving. Render failures are non-fatal.
  useEffect(() => {
    if (!isPlaying) return
    const duration = edl?.duration ?? 0
    let raf = 0
    let last = performance.now()
    clockRef.current = useStore.getState().playhead

    // The media clock (<video>.currentTime) is trusted for advancing the
    // playhead and for the end-of-timeline clamp ONLY on frames where it is
    // close to the wall clock's OWN currently-running value (TRUST_TOL below).
    // This is a per-frame, self-re-arming proximity check — no latch, no
    // one-way state — so it naturally covers two hazards with one rule:
    //   (a) a mid-playback src reload resets currentTime to ~0 while the wall
    //       clock is genuinely mid-timeline (e.g. 5.0s) — far apart, so the
    //       stale-LOW value is never trusted; the wall clock keeps free-
    //       running from where it legitimately was (this is what the old
    //       `resyncing` flag was trying to do, but its entry condition only
    //       fired on a BACKWARD jump — a value that's stale but not
    //       "backward" relative to the last sample slipped through).
    //   (b) a replay-from-end whose currentTime=0 seek hasn't landed yet, so
    //       currentTime briefly sits near the OLD `duration` while the wall
    //       clock has already been reset to 0 for the new play session — far
    //       apart, so the stale-HIGH value is never trusted either, and the
    //       end-clamp (which only ever fires from a wall-clock `t` that was
    //       never snapped to an untrusted value) cannot fire off it.
    // Once the real currentTime lands close to the wall clock's current
    // value (in either hazard, once the seek/reload settles), trust resumes
    // immediately — no waiting for a permanent flag, no re-arm bookkeeping.
    // TRUST_TOL is the same 0.35s tolerance the playhead-sync effect already
    // uses while playing (line ~168) — a fresh seek can legitimately land a
    // few frames later, this is not a tight equality check.
    const TRUST_TOL = 0.35

    const loop = (now: number) => {
      const dt = (now - last) / 1000
      last = now
      const rate = useStore.getState().playbackRate
      const vid = ref.current
      const trustworthy = !!vid && !vid.paused && !vid.ended &&
        Math.abs(vid.currentTime - clockRef.current) < TRUST_TOL
      // Follow the media clock only on trustworthy frames; otherwise the wall
      // clock free-runs so a stalled/failed renderer, a mid-reload video, or
      // a not-yet-landed seek can't freeze or yank the playhead. Because
      // clockRef is NEVER set from an untrusted currentTime, `t >= duration`
      // below can only ever be true from genuine wall-clock (or genuinely
      // trusted media-clock) progress — the end-clamp needs no separate gate.
      if (trustworthy) {
        clockRef.current = vid!.currentTime
      } else {
        clockRef.current += dt * Math.max(-4, Math.min(4, rate || 1))
      }

      let t = clockRef.current
      // Clamp to [0, duration] and stop at the ends. Advancing the playhead is
      // never gated on a frame render succeeding.
      if (duration && t >= duration) {
        try { setPlayhead(duration) } catch { /* non-fatal */ }
        setPlaying(false)
        return
      }
      if (t <= 0 && rate < 0) {
        try { setPlayhead(0) } catch { /* non-fatal */ }
        setPlaying(false)
        return
      }
      try { setPlayhead(Math.max(0, t)) } catch { /* non-fatal */ }
      raf = requestAnimationFrame(loop)
    }
    raf = requestAnimationFrame(loop)
    return () => cancelAnimationFrame(raf)
  }, [isPlaying, edl?.duration, setPlayhead, setPlaying])

  // Frame-step is owned by the keymap (keymap/commands.ts → frameBack /
  // frameForward), which moves the store playhead; the <video> follows via the
  // playhead-sync effect above. Keeping it in one place avoids double-stepping.

  if (!sid) return <div className="preview-empty">Loading…</div>
  if (!edl?.duration) {
    return (
      <div className="preview-empty">
        <div style={{ fontSize: 24, marginBottom: 6 }}>🎞️</div>
        <div>Drop a video in the Media panel to start.</div>
        <div style={{ marginTop: 6 }}><span className="kbd">Space</span> play · <span className="kbd">{chordLabel('Mod+KeyB')}</span> split · <span className="kbd">⌫</span> delete</div>
      </div>
    )
  }

  const url = previewHash ? api.previewURL(sid, previewHash) : api.previewURL(sid)

  // The base v1 clip, if IT is the current selection — drives <CropReposition>
  // below. Scoped to v1 for the same reason StickerLayer's direct-drag is:
  // a v2/PIP clip's on-screen box depends on its own transform, which would
  // need real hit-testing to place this correctly.
  const v1Track = edl.tracks.find((t) => t.id === 'v1')
  const selectedV1Clip = v1Track?.clips.find(
    (c) => c.id === selection && isMediaClip(c),
  ) as Clip | undefined
  const selectedV1Fit = selectedV1Clip
    ? (selectedV1Clip as unknown as { fit?: string }).fit
    : undefined
  // Only while paused — matches the WebCodecs scrubber's own
  // `scrubbing && !isPlaying` gate, and avoids syncing raw-source playback
  // with the timeline's rAF clock (a real complication for no real benefit:
  // repositioning is a paused-editing gesture in every reference this was
  // built from).
  // A KEYFRAMED transform disqualifies the clip. The crop view reads only
  // scalar x/y/scale, so an animated pan drew as if it were centred — the user
  // would be framing against a picture the renderer never produces — and one
  // drag or wheel-tick dispatches set_clip_transform, replacing the whole
  // keyframe list with a constant and destroying the animation with no warning.
  // Same rule StickerLayer already applies to text: one drag cannot express a
  // curve, so the control is withheld rather than made lossy.
  const selectedV1Tx = selectedV1Clip
    ? (selectedV1Clip as unknown as {
      transform?: { x?: unknown; y?: unknown; scale?: unknown }
    }).transform
    : undefined
  const v1TransformKeyframed = !!selectedV1Tx && (['x', 'y', 'scale'] as const).some(
    (k) => selectedV1Tx[k] !== undefined && typeof selectedV1Tx[k] !== 'number',
  )
  // The framing view opens because the user ASKED for it, not because the clip
  // happens to have fit:'cover'. The old gate made the "Fill frame" checkbox do
  // two unrelated jobs at once — set a render property and open an editing mode
  // — so there was no way to finish framing without also changing the render.
  //
  // `fit === 'cover'` is still required: the view IS a crop window, and under
  // `contain` there is nothing to crop (entering the mode sets cover, so this
  // only rejects a clip whose fit was changed out from under it). The keyframe
  // and playing guards are unchanged.
  const showReposition = !!selectedV1Clip && selectedV1Fit === 'cover'
    && framing?.clipId === selectedV1Clip.id
    && !isPlaying && !v1TransformKeyframed

  return (
    <div ref={wrapRef} style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
      {/* `overflow: hidden` because this box IS the canvas: nothing may paint
          outside the frame that will be exported. The live preview applies
          transforms with CSS, and a CSS `rotate()` does not shrink its element
          — the corners swing OUT of the box. With overflow visible they simply
          kept painting into the surrounding pane, so at 60° the picture drew
          711px wide against a 349px frame (2.04x) and then snapped to the
          cropped version the moment the value committed and ffmpeg's render
          landed: "while doing it it is fine, but once I leave the bar the
          results are different". Clipping here makes the live preview show the
          same framing the bake will produce. `.preview-pane`'s own overflow is
          no substitute — it is the whole pane, several times wider than the
          canvas box. */}
      <div style={{ position: 'relative', width: boxSize.w, height: boxSize.h, background: '#000', overflow: 'hidden' }}>
        <video
          ref={ref}
          src={url}
          controls={false}
          preload="auto"
          style={{
            width: '100%', height: '100%', objectFit: 'fill',
            // NOTE: there was briefly a `clipPath: 'inset(2px)'` here, to hide a
            // "phantom colour" strip down the right edge on the theory that the
            // GPU was sampling past the video texture while upscaling the
            // 960px-wide preview into a ~1088px element. It was the wrong
            // diagnosis and has been removed: the strip is the STICKER, dragged
            // so far past the edge that only a thin band of its artwork stays on
            // frame (see the clamp in StickerLayer's move branch). Trimming the
            // video hid nothing and cost 2px of picture. Evidence that ruled the
            // compositor out, kept so nobody re-derives it: the source, the
            // ingest-normalised source, the rendered preview and a full
            // 1920x1080 export all measured a uniform #2d2f35 right edge, and it
            // never reproduced in Chromium at devicePixelRatio 1.0/1.25/1.5/2.0.
            // Live transform preview: while a transform slider is being dragged,
            // apply it as a pure CSS transform (GPU-composited, 0ms) instead of
            // waiting on a server re-render. Commits to the real render on release.
            // dx/dy (StickerLayer's direct on-canvas video drag) are canvas-pixel
            // deltas — converted to CSS px via the same canvas→box ratio the
            // overlay layers use. translate() comes first so it moves the frame
            // in its own untransformed pixel space, not post-scale/rotate space.
            // The values are RELATIVE to what this render already has baked in —
            // an absolute `rotate(${live.rotation}deg)` here showed baked+live,
            // so the second rotation from a non-zero angle was visibly wrong.
            // While the source-based preview is up it fully replaces this
            // element's picture, so the CSS transform must NOT also be applied
            // — the two would compose and the angle would double.
            // `srcPreview && srcDrawn`, not `srcPreview` alone: an armed-but-
            // blank canvas must not take the picture away. Until it paints, the
            // composited preview stays visible with its CSS stand-in — slightly
            // wrong in angle, which is the whole reason the source view exists,
            // but a picture rather than a black rectangle.
            transform: srcPreview && srcDrawn
              ? undefined
              : liveCss
                ? `translate(${liveCss.dx * (boxSize.w / edl.canvas.w)}px, `
                  + `${liveCss.dy * (boxSize.h / edl.canvas.h)}px) `
                  + `scale(${liveCss.scaleMul}) rotate(${liveCss.rotateDeg}deg)`
                : undefined,
            // Hidden (not unmounted) under the source canvas: unmounting would
            // drop the loaded render and make the release flash black while it
            // re-loads, and it is still the element the playback clock and the
            // scrubber talk to.
            opacity: srcPreview && srcDrawn ? 0 : (liveCss?.opacityMul ?? 1),
            transition: liveTransform ? 'none' : 'transform 60ms linear',
            // Live color preview: same idea for the Color panel's brightness/
            // contrast/saturation drags. liveFilter carries the backend's eq
            // params (render/effects.py _color): eq brightness is ADDITIVE in
            // −0.5..0.5 → CSS brightness(1+v); eq contrast and saturation are
            // multiplicative around 1 → CSS contrast(v)/saturate(v). Clamped
            // ≥0 to stay CSS-valid. Applies to the whole preview video — a
            // per-clip approximation, same caveat class as liveTransform.
            filter: liveFx
              ? `brightness(${liveFx.brightnessMul}) contrast(${liveFx.contrastMul}) saturate(${liveFx.saturateMul})`
              : undefined,
          }}
          onTimeUpdate={(e) => {
            // While playing, the rAF clock loop above is the sole owner of
            // `playhead` (including deciding when to trust vs. ignore the
            // video's own currentTime across a reload). This native event
            // fires independently of that loop, so writing straight through
            // to setPlayhead here would race it — e.g. reasserting the
            // pre-resync currentTime==0 the clock loop just decided to
            // distrust. Only let it drive the playhead when paused (scrubbing
            // via native seek, not our rAF loop).
            if (isPlaying) return
            // Even while paused, distrust a currentTime that's far from the
            // store playhead: a preview re-render swaps <video src>, and the
            // media-load algorithm resets currentTime to 0 and fires
            // timeupdate — writing that 0 through permanently yanked the
            // paused playhead back to the start on EVERY edit (the same
            // stale-reload value the playing-path TRUST_TOL check rejects).
            // A genuine native seek settles within the tolerance (the sync
            // effect above just set currentTime = playhead), so real scrubs
            // still pass. Fresh load (playhead≈0) needs no special case:
            // |0 − 0| < 0.35 passes and writes 0, which equals the playhead.
            // `seeking` guards the window where the restore-on-reload seek
            // (onLoadedMetadata below) is still in flight.
            const v = e.target as HTMLVideoElement
            const ph = useStore.getState().playhead
            // readyState < 2: the media-load algorithm fires a bogus
            // timeupdate (currentTime=0, readyState=0, seeking=false) BEFORE
            // loadedmetadata. For ph in (0.05, 0.35] that 0 passes the
            // proximity gate below and stomps the playhead to 0 before the
            // onLoadedMetadata restore even runs. A genuine settled scrub
            // reports readyState >= 2 (HAVE_CURRENT_DATA; observed 4).
            if (v.readyState < 2) return
            const delta = Math.abs(v.currentTime - ph)
            if (v.seeking || delta > 0.35) return
            // Ignore the ECHO of a seek we asked for. A settled seek reports a
            // currentTime a fraction of a frame from the target, which carries
            // no information the playhead doesn't already have — but writing it
            // through still counts as a playhead change, which re-runs the sync
            // effect and stands up a whole new scrub: canvas primed, faded in,
            // a frame decoded and faded out again, ~250ms after the user
            // stopped. That second, uninvited reveal is visible. A move that
            // genuinely lands somewhere else (the fallback path, where the
            // <video> can only reach a keyframe) is off by far more than a
            // frame and still writes through.
            if (delta < frameDur) return
            setPlayhead(v.currentTime)
          }}
          onPlay={() => setPlaying(true)}
          onPause={() => setPlaying(false)}
          onLoadedMetadata={(e) => {
            // Restore the playhead position after a src swap (preview
            // re-render). The media-load algorithm resets currentTime to 0;
            // nothing else re-seeks a PAUSED video (the playhead-sync effect
            // keys on playhead/isPlaying, which don't change across a
            // reload), so without this the paused preview showed frame 0
            // after every edit. loadedmetadata is the earliest moment
            // (readyState >= 1) a seek is honored rather than discarded.
            // Reads the LATEST store playhead so a drag that happened
            // mid-reload wins. While playing this is redundant-but-
            // consistent: the sync effect + rAF TRUST_TOL already converge
            // the reloaded video to the same target.
            const v = e.target as HTMLVideoElement
            const ph = useStore.getState().playhead
            // Clamp to the NEW render's duration: a speed-up can shrink the
            // video below the stored playhead. The browser clamps the seek
            // silently, but the store playhead then sits stranded past the
            // end ("5.00/3.00s" transport, red line off the content) — so
            // when ph exceeds the new duration, pull the store playhead back
            // too, re-cohering transport/red-line/video. duration can be NaN
            // here in theory (guard), though loadedmetadata implies it's set.
            const vd = v.duration
            const maxT = Number.isFinite(vd) && vd > 0 ? vd : ph
            const target = Math.min(ph, maxT)
            if (ph > maxT + 0.001) {
              try { setPlayhead(target) } catch { /* non-fatal */ }
              clockRef.current = target
            }
            if (target > 0.05) {
              try { v.currentTime = target } catch { /* non-fatal */ }
            }
          }}
          onLoadedData={(e) => {
            // The committed transform (Properties.tsx's onChange) is only
            // visible once THIS reload finishes — clearing liveTransform any
            // earlier drops the CSS preview back to the untransformed old
            // frame for the gap between commit and re-render (the "reverts
            // the moment you let go" bug). Clearing it here means the CSS
            // transform stays applied right up until the new, correctly
            // transformed frame is actually on screen. liveFilter follows the
            // identical lifecycle.
            // THIS is the moment the new render becomes what the user is
            // looking at, so promote the pose snapshotted for it. Until now the
            // element was still showing the previous frame, and a gesture
            // starting in that window must compose against THAT one.
            screenTxRef.current = pendingTxRef.current
            if (liveTransform) setLiveTransform(null)
            if (liveFilter) setLiveFilter(null)
            // Re-arm playback after a mid-playback src swap. The media-load
            // algorithm sets paused=true WITHOUT firing a `pause` event, and
            // the play/pause effect keys on [isPlaying, playbackRate] — which
            // don't change across a reload — so nothing ever called play()
            // again: the audio went silent while the rAF wall clock kept
            // counting, and the sync effect scrub-seeked the paused element
            // frame by frame. That is the reported "music stops, timer speeds
            // up, video lags out of sync".
            const v = e.target as HTMLVideoElement
            if (useStore.getState().isPlaying && v.paused && playbackRate > 0) {
              v.play().catch(() => { /* autoplay/decode hiccup — non-fatal */ })
            }
          }}
        />
        {/* WebCodecs frame-accurate scrubber. Sits between <video> and text
            overlays; only opaque while seeking (caller decides). Wrapped in
            an ErrorBoundary so a mp4box / VideoDecoder hiccup on an unusual
            preview can never blank the entire editor — we silently fall back
            to <video>.currentTime, which still scrubs (just less precisely). */}
        {boxSize.w > 0 && (
          <ErrorBoundary resetKey={url} fallback={() => null}>
            <FrameScrubber
              ref={scrubberRef}
              src={url}
              width={boxSize.w}
              height={boxSize.h}
              visible={scrubbing && !isPlaying}
            />
          </ErrorBoundary>
        )}
        {/* Interactive stickers (draw + select + drag + resize). Sits under the
            text layer so text stays on top, but captures clicks because the
            text layer is pointer-events:none. */}
        {/* Source-based live transform preview. Above the <video> (which is
            hidden while this is up) and below the overlay layers, so stickers
            and text keep drawing over the picture during the gesture. */}
        {srcPreview && (
          <canvas
            ref={srcCanvasRef}
            style={{ position: 'absolute', left: 0, top: 0,
                     width: boxSize.w, height: boxSize.h, pointerEvents: 'none' }} />
        )}
        {edl && boxSize.w > 0 && (
          <StickerLayer edl={edl} videoEl={ref.current} width={boxSize.w} height={boxSize.h} />
        )}
        {/* Realtime text overlay — no server roundtrip per edit */}
        {edl && boxSize.w > 0 && (
          <TextLayer edl={edl} videoEl={ref.current} width={boxSize.w} height={boxSize.h} />
        )}
        {/* CapCut-style crop/reposition view. Deliberately LAST (topmost) —
            it fully replaces the view above (raw uncropped source, zoomed
            out) while active, so sticker/text interaction is unavailable
            until framing is finished — press Apply in the Framing section (or
            select another clip). See the component's own header for why a
            simple outline overlay doesn't work here. */}
        {showReposition && selectedV1Clip && sid && boxSize.w > 0 && (
          <CropReposition
            clip={selectedV1Clip}
            canvasW={edl.canvas.w}
            canvasH={edl.canvas.h}
            sid={sid}
            paneW={boxSize.w}
            paneH={boxSize.h}
            playhead={playhead}
          />
        )}
        {rendering && (
          <div style={{ position: 'absolute', top: 8, right: 8, color: 'var(--text-dim)', fontSize: 11, background: 'rgba(0,0,0,0.5)', padding: '2px 6px', borderRadius: 4 }}>
            ⚙ Rendering…
          </div>
        )}
        {error && (
          <div style={{ position: 'absolute', top: 8, left: 8, color: 'var(--accent)', fontSize: 11, background: 'rgba(0,0,0,0.6)', padding: '4px 8px', borderRadius: 4, maxWidth: '80%' }}>
            {error}
          </div>
        )}
      </div>
      {/* The floating transport that used to sit here has MOVED into the
          timeline toolbar (Timeline.tsx), along with Undo/Redo. It was
          absolutely positioned at bottom-centre OVER the picture — precisely
          where captions and lower-thirds are placed — so the controls covered
          the region being edited. */}
    </div>
  )
}
