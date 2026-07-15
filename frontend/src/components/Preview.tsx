import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'
import { useStore } from '../store'
import { TextLayer } from './TextLayer'
import { StickerLayer } from './StickerLayer'
import { FrameScrubber, type FrameScrubberHandle } from './FrameScrubber'
import { ErrorBoundary } from './ErrorBoundary'

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

  const ref = useRef<HTMLVideoElement>(null)
  const [rendering, setRendering] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [boxSize, setBoxSize] = useState({ w: 0, h: 0 })
  const wrapRef = useRef<HTMLDivElement>(null)
  // WebCodecs scrubber: shown during seek, hidden during playback so frames
  // come from <video> at full smoothness.
  const scrubberRef = useRef<FrameScrubberHandle>(null)
  const [scrubbing, setScrubbing] = useState(false)
  const scrubTimer = useRef<number | null>(null)
  // Authoritative playback time (seconds). The rAF clock owns this while
  // playing; kept in a ref so the loop never reads a stale `playhead` closure.
  const clockRef = useRef(0)

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
    const vidTracks = edl.tracks.filter(t => t.type === 'video' || t.type === 'audio' || t.type === 'music' || t.type === 'vo')
    return JSON.stringify({
      canvas: edl.canvas,
      tracks: vidTracks.map(t => ({ id: t.id, clips: t.clips })),
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
          setError(String(e))
          // A failed render means the <video> src never changes, so
          // onLoadedData (which clears liveTransform) never fires either —
          // fail fast instead of leaving the CSS transform preview stuck
          // for the full safety-net timeout.
          setLiveTransform(null)
        })
        .finally(() => {
          if (ac.signal.aborted) return
          setRendering(false)
        })
    }, 250)
    return () => {
      if (debounceRef.current) window.clearTimeout(debounceRef.current)
    }
  }, [sid, videoFingerprint, edl?.duration, renderPreview, setLiveTransform])

  // Safety net for the live-transform CSS preview (see the <video> element's
  // onLoadedData below): if the expected re-render never lands — the render
  // fails, gets aborted, or the fingerprint didn't actually change — nothing
  // would otherwise clear liveTransform, leaving the CSS override applied
  // forever (a stuck, wrong-looking preview is worse than a brief revert).
  // 250ms debounce + typical render + load latency comfortably fits in 8s;
  // any liveTransform still set after that is treated as abandoned.
  useEffect(() => {
    if (!liveTransform) return
    const t = window.setTimeout(() => setLiveTransform(null), 8000)
    return () => window.clearTimeout(t)
  }, [liveTransform, setLiveTransform])

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

  useEffect(() => {
    const v = ref.current
    if (!v) return
    // Sync the <video> to an EXTERNAL playhead move (scrub while paused, or a
    // deliberate jump during playback). While playing, the rAF clock already
    // mirrors the video, so only a large gap warrants a seek — small free-run
    // drift must not trigger a per-frame seek storm.
    const gap = Math.abs(v.currentTime - playhead)
    if (gap > (isPlaying ? 0.35 : 0.05)) {
      // A failed/odd <video> can throw on a seek — never let that break the UI.
      try { v.currentTime = playhead } catch { /* non-fatal */ }
      clockRef.current = playhead   // keep the clock in step with the jump
    }
    // While paused, also drive the WebCodecs scrubber so frame-step keys land
    // on the exact frame even when the underlying <video> snapped to a keyframe.
    if (!isPlaying && scrubberRef.current?.isReady()) {
      setScrubbing(true)
      scrubberRef.current.seek(playhead).catch(() => {})
      if (scrubTimer.current) window.clearTimeout(scrubTimer.current)
      // Hide the canvas after a quiet moment so playback resume looks clean.
      scrubTimer.current = window.setTimeout(() => setScrubbing(false), 250)
    } else if (isPlaying) {
      setScrubbing(false)
    }
  }, [playhead, isPlaying])

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
        <div style={{ marginTop: 6 }}><span className="kbd">Space</span> play · <span className="kbd">⌘B</span> split · <span className="kbd">⌫</span> delete</div>
      </div>
    )
  }

  const url = previewHash ? api.previewURL(sid, previewHash) : api.previewURL(sid)

  return (
    <div ref={wrapRef} style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
      <div style={{ position: 'relative', width: boxSize.w, height: boxSize.h, background: '#000' }}>
        <video
          ref={ref}
          src={url}
          controls={false}
          preload="auto"
          style={{
            width: '100%', height: '100%', objectFit: 'fill',
            // Live transform preview: while a transform slider is being dragged,
            // apply it as a pure CSS transform (GPU-composited, 0ms) instead of
            // waiting on a server re-render. Commits to the real render on release.
            transform: liveTransform
              ? `scale(${liveTransform.scale ?? 1}) rotate(${liveTransform.rotation ?? 0}deg)`
              : undefined,
            opacity: liveTransform?.opacity ?? 1,
            transition: liveTransform ? 'none' : 'transform 60ms linear',
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
            setPlayhead((e.target as HTMLVideoElement).currentTime)
          }}
          onPlay={() => setPlaying(true)}
          onPause={() => setPlaying(false)}
          onLoadedData={() => {
            // The committed transform (Properties.tsx's onChange) is only
            // visible once THIS reload finishes — clearing liveTransform any
            // earlier drops the CSS preview back to the untransformed old
            // frame for the gap between commit and re-render (the "reverts
            // the moment you let go" bug). Clearing it here means the CSS
            // transform stays applied right up until the new, correctly
            // transformed frame is actually on screen.
            if (liveTransform) setLiveTransform(null)
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
        {edl && boxSize.w > 0 && (
          <StickerLayer edl={edl} videoEl={ref.current} width={boxSize.w} height={boxSize.h} />
        )}
        {/* Realtime text overlay — no server roundtrip per edit */}
        {edl && boxSize.w > 0 && (
          <TextLayer edl={edl} videoEl={ref.current} width={boxSize.w} height={boxSize.h} />
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
      <div className="transport">
        <button onClick={() => {
          // Mirror the playPause keyboard command's end-of-timeline rewind
          // (keymap/commands.ts) so clicking this button behaves the same as
          // pressing Space: starting playback from the very end plays a few
          // ms and immediately re-hits the end-clamp otherwise, reading as
          // "does nothing."
          if (!isPlaying && edl.duration > 0 && playhead >= edl.duration - 1 / 30) {
            setPlayhead(0)
          }
          setPlaying(!isPlaying)
        }}>{isPlaying ? '⏸' : '▶'}</button>
        <span style={{ fontSize: 12 }}>{playhead.toFixed(2)} / {edl.duration.toFixed(2)}s</span>
      </div>
    </div>
  )
}
