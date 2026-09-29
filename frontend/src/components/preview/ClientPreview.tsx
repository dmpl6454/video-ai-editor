// The preview pane in CLIENT mode (wave D, INSTANT_PREVIEW_SPEC §3, §4.1,
// §4.3, §7, §9.2): the instant engine's WebGL2 canvas (and its hidden laneA
// <video>) in the canvas box, under the same StickerLayer / TextLayer /
// SafeZones / CropReposition the server preview uses — the layers draw at
// the engine's PRESENTED-frame clock instead of a <video>'s currentTime.
//
// * Paused: the store playhead is the truth; every move is an engine seek
//   (frame-exact, (k + 0.5)/R).
// * Playing: the engine is the truth; an rAF loop writes its presented time
//   into the store playhead (no TRUST_TOL: the clock is the frame on
//   screen). A playhead move the loop did not write (a click on the ruler)
//   is a seek.
// * Transport is the store's setPlaying, which starts the engine inside the
//   key/click handler (the user's gesture).
// * Bakes: the server render still runs — niced, 1.5 s after the last edit,
//   or 250 ms when the program has BAKED ranges — and its bake spans are
//   spliced in by the controller.
// * One corner spinner, per fidelity state: last good frame held (pending),
//   playback waiting for frames (buffering), or a BAKED range still showing
//   the RAW client frames (baking).

import { useEffect, useMemo, useRef, useState } from 'react'
import { errorMessage, previewController, useStore } from '../../store'
import { isMediaClip, type Clip } from '../../types'
import { TextLayer } from '../TextLayer'
import { StickerLayer } from '../StickerLayer'
import { CropReposition } from '../CropReposition'
import { SafeZones } from '../SafeZones'
import { CommandKey } from '../CommandKey'
import { Icon } from '../Icon'
import { committedGradeOf, committedPoseAt, liveCssFilter, liveCssTransform, liveVideoCssApplies } from '../../lib/overlay'
import { videoFingerprintOf } from '../../lib/previewFingerprint'
import type { WaitKind } from '../../lib/preview/previewController'
import { MODE_APPROX } from '../../lib/preview/timeline/support'
import { approxSummary } from '../../lib/preview/fidelityLabels'
import { useLiveApprox } from '../../lib/preview/liveApprox'
import './clientPreview.css'
import { playheadSeek } from './playheadSeek'

/** Background render cadence (§4.1 step 8). */
export const BAKE_RENDER_DELAY_MS = 250
export const IDLE_RENDER_DELAY_MS = 1500

const WAIT_LABEL: Record<Exclude<WaitKind, null>, string> = {
  pending: 'Loading this frame',
  buffering: 'Buffering',
  baking: 'Rendering effects for this part',
}

export function ClientPreview() {
  const sid = useStore((s) => s.sessionId)
  const edl = useStore((s) => s.edl)
  const playhead = useStore((s) => s.playhead)
  const isPlaying = useStore((s) => s.isPlaying)
  const setPlayhead = useStore((s) => s.setPlayhead)
  const renderPreview = useStore((s) => s.renderPreview)
  const view = useStore((s) => s.clientView)
  const selection = useStore((s) => s.selection)
  const framing = useStore((s) => s.framing)
  const liveTransform = useStore((s) => s.liveTransform)
  const liveFilter = useStore((s) => s.liveFilter)
  const liveApprox = useLiveApprox()
  const setLiveTransform = useStore((s) => s.setLiveTransform)
  const setLiveFilter = useStore((s) => s.setLiveFilter)

  const wrapRef = useRef<HTMLDivElement>(null)
  const hostRef = useRef<HTMLDivElement>(null)
  const [boxSize, setBoxSize] = useState({ w: 0, h: 0 })
  const [error, setError] = useState<string | null>(null)
  const hasPicture = !!sid && !!edl?.duration

  // The canvas box: the EDL canvas aspect, letterboxed in the pane.
  useEffect(() => {
    const el = wrapRef.current
    if (!el || !edl) return
    const update = () => {
      const aspect = edl.canvas.w / edl.canvas.h
      const bw = el.clientWidth
      const bh = el.clientHeight
      let w: number, h: number
      if (bw / Math.max(1, bh) > aspect) { h = bh; w = Math.round(h * aspect) } else { w = bw; h = Math.round(w / aspect) }
      setBoxSize((b) => (b.w === w && b.h === h ? b : { w, h }))
    }
    const ro = new ResizeObserver(update)
    ro.observe(el)
    update()
    return () => ro.disconnect()
  }, [edl, hasPicture])

  // Mount the engine for as long as this pane shows a picture.
  useEffect(() => {
    const host = hostRef.current
    const ctl = previewController()
    if (!host || !ctl || !hasPicture) return
    ctl.attach(host)
    return () => ctl.detach()
  }, [sid, hasPicture])

  // The overlays' clock: the frame on screen (spec §3.5, R6).
  const clock = useMemo(() => ({ now: () => previewController()?.now() ?? useStore.getState().playhead }), [])

  // Playing: the engine's presented time drives the store playhead.
  const lastWrittenRef = useRef<number | null>(null)
  const justPausedRef = useRef(false)
  useEffect(() => {
    if (!isPlaying) return
    let raf = 0
    const loop = () => {
      const ctl = previewController()
      if (ctl?.engine?.playing) {
        const t = ctl.now()
        lastWrittenRef.current = t
        setPlayhead(t)
      }
      raf = requestAnimationFrame(loop)
    }
    raf = requestAnimationFrame(loop)
    return () => {
      cancelAnimationFrame(raf)
      justPausedRef.current = true
    }
  }, [isPlaying, setPlayhead])

  // Paused (or a jump while playing): the playhead is a seek.
  useEffect(() => {
    const ctl = previewController()
    if (!ctl) return
    if (!isPlaying && justPausedRef.current) {
      // The engine stopped ON the frame it presented; take the playhead from
      // it instead of seeking back to the last sampled value (a visible
      // one-frame rewind on every pause).
      justPausedRef.current = false
      const t = ctl.now()
      lastWrittenRef.current = t
      if (Math.abs(t - playhead) > 1e-9) setPlayhead(t)
      return
    }
    const step = playheadSeek(isPlaying, playhead, lastWrittenRef.current)
    lastWrittenRef.current = step.lastWritten
    if (step.seek) ctl.seekTime(playhead)
  }, [playhead, isPlaying, setPlayhead])

  // The background render (bakes, and the loudness gain it measures). Text
  // and sticker edits never need one. Urgent (BAKED ranges, or a loudness
  // gain not measured yet — K2, 0.8.0 QA) after BAKE_RENDER_DELAY_MS, asked
  // THEN so the loudness answer of the new hash is in; else at the idle
  // cadence.
  const fingerprint = useMemo(() => videoFingerprintOf(edl), [edl])
  useEffect(() => {
    if (!sid || !edl?.duration) return
    const fire = () => {
      setError(null)
      renderPreview({ priority: 'low' }).catch((e) => {
        // Only worth a word where the picture depends on it (BAKED ranges).
        if (previewController()?.needsBake()) setError(errorMessage(e))
        else console.warn('[preview] background render failed:', errorMessage(e))
      })
    }
    let t = window.setTimeout(() => {
      if (previewController()?.renderUrgent()) fire()
      else t = window.setTimeout(fire, IDLE_RENDER_DELAY_MS - BAKE_RENDER_DELAY_MS)
    }, BAKE_RENDER_DELAY_MS)
    return () => window.clearTimeout(t)
  }, [sid, fingerprint, edl?.duration, renderPreview])

  // Live transform / colour drags (Phase 1): the same CSS stand-in the
  // server preview uses, over the engine's picture of the COMMITTED clip —
  // which is what the engine shows the moment a commit lands, so the
  // stand-in clears as soon as the EDL changes.
  const liveTxTrackId = liveTransform && edl
    ? (edl.tracks.find((tk) => tk.clips.some((k) => (k as { id?: string }).id === liveTransform.clipId))?.id ?? null)
    : null
  const liveTxClipId = liveTransform?.clipId ?? null
  const committedTx = useMemo(() => committedPoseAt(edl, liveTxClipId, playhead), [edl, liveTxClipId, playhead])
  const liveCss = liveTransform && liveVideoCssApplies(liveTxTrackId) ? liveCssTransform(liveTransform, committedTx) : null
  const committedFx = useMemo(() => committedGradeOf(edl, liveFilter?.clipId), [liveFilter, edl])
  const liveFx = liveFilter ? liveCssFilter(liveFilter, committedFx) : null
  const edlSeenRef = useRef(edl)
  useEffect(() => {
    if (edlSeenRef.current === edl) return
    edlSeenRef.current = edl
    // the commit is on screen by the next frame
    const raf = requestAnimationFrame(() => {
      const s = useStore.getState()
      if (s.liveTransform) setLiveTransform(null)
      if (s.liveFilter) setLiveFilter(null)
    })
    return () => cancelAnimationFrame(raf)
  }, [edl, setLiveTransform, setLiveFilter])
  useEffect(() => {
    if (!liveTransform && !liveFilter) return
    const t = window.setTimeout(() => { setLiveTransform(null); setLiveFilter(null) }, 8000)
    return () => window.clearTimeout(t)
  }, [liveTransform, liveFilter, setLiveTransform, setLiveFilter])

  if (!sid) return <div className="preview-empty">Loading…</div>
  if (!edl?.duration) {
    return (
      <div className="preview-empty">
        <div style={{ marginBottom: 6, color: 'var(--text-dim)' }}><Icon name="film" size={28} /></div>
        <div>Drop a video in the Media panel to start.</div>
        <div style={{ marginTop: 6 }}><CommandKey id="playPause" /> play · <CommandKey id="split" /> split · <CommandKey id="rippleDelete" /> delete</div>
      </div>
    )
  }

  const v1Track = edl.tracks.find((t) => t.id === 'v1')
  const selectedV1Clip = v1Track?.clips.find((c) => c.id === selection && isMediaClip(c)) as Clip | undefined
  const selectedV1Tx = selectedV1Clip
    ? (selectedV1Clip as unknown as { transform?: { x?: unknown; y?: unknown; scale?: unknown }; fit?: string })
    : undefined
  const keyframed = !!selectedV1Tx?.transform && (['x', 'y', 'scale'] as const).some(
    (k) => selectedV1Tx.transform![k] !== undefined && typeof selectedV1Tx.transform![k] !== 'number')
  const showReposition = !!selectedV1Clip && selectedV1Tx?.fit === 'cover'
    && framing?.clipId === selectedV1Clip.id && !isPlaying && !keyframed
  const wait = view?.wait ?? null
  // review RE: an APPROX frame says so — the engine's reasons at the
  // presented frame, plus the live overlay draw's (a blend this browser
  // approximates); the chip is static (nothing moves under reduced motion)
  const approx = [...(view && view.live && view.modeAtPlayhead === MODE_APPROX ? view.reasons : []), ...liveApprox]

  return (
    <div ref={wrapRef} className="client-preview">
      <div className="client-preview-box" data-preview-engine="client"
           style={{ width: boxSize.w, height: boxSize.h }}>
        <div ref={hostRef} className="client-preview-engine"
             style={{
               transform: liveCss
                 ? `translate(${liveCss.dx * (boxSize.w / edl.canvas.w)}px, ${liveCss.dy * (boxSize.h / edl.canvas.h)}px) `
                   + `scale(${liveCss.scaleMul}) rotate(${liveCss.rotateDeg}deg)`
                 : undefined,
               opacity: liveCss?.opacityMul ?? 1,
               filter: liveFx
                 ? `brightness(${liveFx.brightnessMul}) contrast(${liveFx.contrastMul}) saturate(${liveFx.saturateMul})`
                 : undefined,
             }} />
        {boxSize.w > 0 && (
          <StickerLayer edl={edl} videoEl={null} clock={clock} width={boxSize.w} height={boxSize.h} />
        )}
        {boxSize.w > 0 && (
          <TextLayer edl={edl} videoEl={null} clock={clock} width={boxSize.w} height={boxSize.h} />
        )}
        {boxSize.w > 0 && <SafeZones canvas={edl.canvas} width={boxSize.w} height={boxSize.h} />}
        {showReposition && selectedV1Clip && boxSize.w > 0 && (
          <CropReposition clip={selectedV1Clip} canvasW={edl.canvas.w} canvasH={edl.canvas.h} sid={sid}
                          paneW={boxSize.w} paneH={boxSize.h} playhead={playhead} />
        )}
        {wait && (
          <div className="client-preview-spinner" data-wait={wait} role="status" aria-label={WAIT_LABEL[wait]}
               title={WAIT_LABEL[wait]}>
            <span className="client-preview-ring" aria-hidden="true" />
          </div>
        )}
        {approx.length > 0 && !wait && (
          <div className="client-preview-approx" role="note" data-fidelity="approx"
               aria-label={approxSummary(approx)} title={approxSummary(approx)}>
            <span aria-hidden="true">≈</span>
          </div>
        )}
        {error && <div className="client-preview-error" role="alert">{error}</div>}
      </div>
    </div>
  )
}
