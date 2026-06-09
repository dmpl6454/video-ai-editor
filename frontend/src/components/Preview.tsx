import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'
import { useStore } from '../store'
import { isMediaClip } from '../types'
import { TextLayer } from './TextLayer'
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

  // A fingerprint that changes only for video-relevant edits. Text edits do
  // NOT change this, so the server preview is reused while client overlays
  // update in real time.
  const videoFingerprint = useMemo(() => {
    if (!edl) return ''
    const vidTracks = edl.tracks.filter(t => t.type === 'video' || t.type === 'audio' || t.type === 'music' || t.type === 'vo')
    return JSON.stringify({
      canvas: edl.canvas,
      tracks: vidTracks.map(t => ({
        id: t.id,
        clips: t.clips.map((c) => isMediaClip(c)
          ? { id: c.id, src: c.src, in: c.in, out: c.out, start: c.start }
          : { id: c.id, start: (c as { start: number }).start }),
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
          setError(String(e))
        })
        .finally(() => {
          if (ac.signal.aborted) return
          setRendering(false)
        })
    }, 250)
    return () => {
      if (debounceRef.current) window.clearTimeout(debounceRef.current)
    }
  }, [sid, videoFingerprint, edl?.duration, renderPreview])

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
    if (!ref.current) return
    if (Math.abs(ref.current.currentTime - playhead) > 0.05) {
      ref.current.currentTime = playhead
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

  // Frame-accurate playhead sync via requestVideoFrameCallback. The vanilla
  // `timeupdate` event fires at ~250 ms intervals — way too coarse for a
  // scrubber that needs to land on the right frame. rVFC fires once per
  // decoded frame with the exact mediaTime, so the playhead UI tracks the
  // video down to <1 frame of jitter on Chrome/Safari.
  useEffect(() => {
    const v = ref.current
    if (!v || typeof (v as unknown as { requestVideoFrameCallback?: unknown })
              .requestVideoFrameCallback !== 'function') return
    let handle = 0
    const tick = (_now: number, meta: { mediaTime: number }) => {
      setPlayhead(meta.mediaTime)
      handle = (v as unknown as {
        requestVideoFrameCallback: (cb: typeof tick) => number
      }).requestVideoFrameCallback(tick)
    }
    handle = (v as unknown as {
      requestVideoFrameCallback: (cb: typeof tick) => number
    }).requestVideoFrameCallback(tick)
    return () => {
      const cancelFn = (v as unknown as {
        cancelVideoFrameCallback?: (h: number) => void
      }).cancelVideoFrameCallback
      if (cancelFn) cancelFn(handle)
    }
  }, [setPlayhead])

  // Frame-step shortcuts: `,` back one frame, `.` forward one frame. Hold
  // Shift for a 10-frame jump (matches CapCut/Premiere convention).
  useEffect(() => {
    if (!edl) return
    const fps = edl.canvas.fps || 30
    const dt = 1 / fps
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA') return
      if (e.key === ',' || e.key === '.') {
        e.preventDefault()
        const sign = e.key === ',' ? -1 : 1
        const step = (e.shiftKey ? 10 : 1) * sign * dt
        const v = ref.current
        if (!v) return
        v.pause()
        // Snap to the nearest frame boundary FROM the current frame's
        // start (ceil/floor instead of round) so a tap of `.` is always +1.
        const cur = v.currentTime
        const curFrame = Math.round(cur * fps)
        const newTime = (curFrame + (e.shiftKey ? 10 * sign : sign)) * dt
        v.currentTime = Math.max(0, Math.min(edl.duration, newTime))
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [edl])

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
          onTimeUpdate={(e) => setPlayhead((e.target as HTMLVideoElement).currentTime)}
          onPlay={() => setPlaying(true)}
          onPause={() => setPlaying(false)}
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
        <button onClick={() => setPlaying(!isPlaying)}>{isPlaying ? '⏸' : '▶'}</button>
        <span style={{ fontSize: 12 }}>{playhead.toFixed(2)} / {edl.duration.toFixed(2)}s</span>
      </div>
    </div>
  )
}
