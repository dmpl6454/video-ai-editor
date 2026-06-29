import { useEffect, useMemo, useRef, useState } from 'react'
import { useStore } from '../store'
import { api } from '../api'
import { isMediaClip, type AnyClip } from '../types'

// Per-source waveform cache: src path → peaks array. Fetched once, reused on
// every redraw. The peaks themselves are independent of timeline placement.
const WAVE_CACHE = new Map<string, { peaks: number[]; peaks_per_sec: number; duration: number }>()
const WAVE_INFLIGHT = new Map<string, Promise<void>>()

const TRACK_COLORS: Record<string, string> = {
  video: '#5b8dff',
  audio: '#4ade80',
  music: '#a78bfa',
  vo: '#22d3ee',
  text: '#fbbf24',
  sticker: '#f472b6',
  effect: '#f97316',
  captions: '#f472b6',
}

interface HitClip { trackId: string; clip: AnyClip; x: number; y: number; w: number; h: number }

export function Timeline() {
  const edl = useStore((s) => s.edl)
  const sid = useStore((s) => s.sessionId)
  const selection = useStore((s) => s.selection)
  const multiSelection = useStore((s) => s.multiSelection)
  const setSelection = useStore((s) => s.setSelection)
  const toggleSelection = useStore((s) => s.toggleSelection)
  const inMark = useStore((s) => s.inMark)
  const outMark = useStore((s) => s.outMark)
  const playhead = useStore((s) => s.playhead)
  const setPlayhead = useStore((s) => s.setPlayhead)
  const dispatch = useStore((s) => s.dispatch)
  const [contextMenu, setContextMenu] = useState<{ x: number; y: number; clipId: string; trackId: string } | null>(null)

  const wrapRef = useRef<HTMLDivElement>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  // Zoom + snap live in the store so keyboard shortcuts can drive them.
  const zoom = useStore((s) => s.timelineZoom)
  const setZoomStore = useStore((s) => s.setTimelineZoom)
  const snapEnabled = useStore((s) => s.snapEnabled)
  const [dpr] = useState(window.devicePixelRatio || 1)
  const [size, setSize] = useState({ w: 800, h: 240 })
  const [waveTick, setWaveTick] = useState(0)  // bump to force redraw when peaks arrive

  // drag state for moving / trimming clips
  const dragRef = useRef<null | {
    kind: 'move' | 'trim-l' | 'trim-r'
    clipId: string
    trackId: string
    startX: number
    origStart: number
    origIn: number
    origOut: number
  }>(null)

  // Resize observer
  useEffect(() => {
    if (!wrapRef.current) return
    const ro = new ResizeObserver((entries) => {
      const r = entries[0].contentRect
      setSize({ w: r.width, h: r.height })
    })
    ro.observe(wrapRef.current)
    return () => ro.disconnect()
  }, [])

  // Tracks to render (only ones with content or core tracks). Memoized so
  // unrelated store updates (playhead, drag state, etc.) don't change the
  // identity of the array and trigger a full canvas redraw.
  const tracks = useMemo(
    () => (edl?.tracks ?? []).filter((t) =>
      ['video', 'audio', 'music', 'vo', 'text', 'sticker', 'captions'].includes(t.type)
    ),
    [edl]
  )

  // Kick off waveform fetches for any clip srcs we haven't loaded yet. Audio/
  // music/vo always show waveforms; video tracks show them too because their
  // mp4s carry an audio stream.
  useEffect(() => {
    if (!sid || !edl) return
    const wantsWave = (type: string) => ['video', 'audio', 'music', 'vo'].includes(type)
    const seen = new Set<string>()
    for (const t of edl.tracks) {
      if (!wantsWave(t.type)) continue
      for (const c of t.clips) {
        if (!isMediaClip(c)) continue
        if (seen.has(c.src)) continue
        seen.add(c.src)
        if (WAVE_CACHE.has(c.src) || WAVE_INFLIGHT.has(c.src)) continue
        const p = api.waveform(sid, c.src, 50)
          .then((data) => {
            WAVE_CACHE.set(c.src, data)
            setWaveTick((n) => n + 1)
          })
          .catch(() => {
            // Cache a sentinel empty so we don't retry forever.
            WAVE_CACHE.set(c.src, { peaks: [], peaks_per_sec: 50, duration: 0 })
          })
          .finally(() => WAVE_INFLIGHT.delete(c.src))
        WAVE_INFLIGHT.set(c.src, p)
      }
    }
  }, [sid, edl])

  const trackHeight = 36
  const headerHeight = 24
  const labelWidth = 80

  function trackY(i: number): number {
    return headerHeight + i * (trackHeight + 4)
  }

  // Build hit list each render so we can do clip hit-testing on click.
  const hits: HitClip[] = []
  for (let i = 0; i < tracks.length; i++) {
    const t = tracks[i]
    const y = trackY(i)
    for (const c of t.clips) {
      const start = isMediaClip(c) ? c.start : c.start
      const dur = isMediaClip(c) ? c.out - c.in : c.end - c.start
      hits.push({
        trackId: t.id, clip: c,
        x: labelWidth + start * zoom,
        y,
        w: Math.max(2, dur * zoom),
        h: trackHeight,
      })
    }
  }

  // Draw
  useEffect(() => {
    const cv = canvasRef.current
    if (!cv) return
    cv.width = size.w * dpr
    cv.height = size.h * dpr
    cv.style.width = `${size.w}px`
    cv.style.height = `${size.h}px`
    const ctx = cv.getContext('2d')!
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    ctx.clearRect(0, 0, size.w, size.h)

    // bg
    ctx.fillStyle = '#16161a'
    ctx.fillRect(0, 0, size.w, size.h)

    // ruler
    ctx.fillStyle = '#1d1d22'
    ctx.fillRect(labelWidth, 0, size.w - labelWidth, headerHeight)
    ctx.font = '10px var(--font-ui)'
    ctx.fillStyle = '#9b9ba5'
    const dur = edl?.duration ?? 0
    const pixelsPerTick = 80
    const tickSec = niceTick(pixelsPerTick / zoom)
    for (let t = 0; t <= dur + 30; t += tickSec) {
      const x = labelWidth + t * zoom
      if (x > size.w) break
      ctx.fillRect(x, headerHeight - 4, 1, 4)
      ctx.fillText(formatTime(t), x + 3, headerHeight - 7)
    }

    // tracks
    for (let i = 0; i < tracks.length; i++) {
      const t = tracks[i]
      const y = trackY(i)
      // label
      ctx.fillStyle = '#1d1d22'
      ctx.fillRect(0, y, labelWidth, trackHeight)
      ctx.fillStyle = t.muted ? '#5a5a64' : '#9b9ba5'
      ctx.font = '11px var(--font-ui)'
      ctx.fillText(t.label ?? t.id, 22, y + trackHeight / 2 + 4)

      // mute toggle (small icon in the label area)
      const muteX = 6
      const muteY = y + trackHeight / 2 - 6
      ctx.fillStyle = t.muted ? '#ff4d6d' : '#3a3a44'
      ctx.fillRect(muteX, muteY, 12, 12)
      ctx.fillStyle = t.muted ? '#fff' : '#9b9ba5'
      ctx.font = 'bold 9px var(--font-ui)'
      ctx.fillText('M', muteX + 3, muteY + 9)
      ctx.font = '11px var(--font-ui)'

      // track row bg
      ctx.fillStyle = t.muted ? '#15151a' : '#1a1a1f'
      ctx.fillRect(labelWidth, y, size.w - labelWidth, trackHeight)
      ctx.strokeStyle = '#22222a'
      ctx.strokeRect(labelWidth + 0.5, y + 0.5, size.w - labelWidth - 1, trackHeight - 1)

      // clips
      const showWaveOn = ['video', 'audio', 'music', 'vo'].includes(t.type)
      for (const c of t.clips) {
        const start = isMediaClip(c) ? c.start : c.start
        const dur = isMediaClip(c) ? c.out - c.in : c.end - c.start
        const x = labelWidth + start * zoom
        const w = Math.max(2, dur * zoom)
        const isSel = c.id === selection || multiSelection.includes(c.id)
        const color = TRACK_COLORS[t.type] ?? '#5b8dff'
        ctx.fillStyle = color
        ctx.globalAlpha = (isSel ? 1.0 : 0.85) * (t.muted ? 0.35 : 1)
        roundRect(ctx, x, y + 4, w, trackHeight - 8, 4)
        ctx.fill()
        ctx.globalAlpha = 1

        // Waveform inside the clip rect
        if (showWaveOn && isMediaClip(c) && w > 12) {
          const wave = WAVE_CACHE.get(c.src)
          if (wave && wave.peaks.length) {
            ctx.save()
            // Clip drawing to the clip rect
            ctx.beginPath()
            roundRect(ctx, x, y + 4, w, trackHeight - 8, 4)
            ctx.clip()
            ctx.fillStyle = 'rgba(0,0,0,0.55)'
            const baseY = y + trackHeight / 2
            const halfH = (trackHeight - 12) / 2
            // Map [c.in .. c.out] → x..x+w. Each pixel column samples one peak.
            const cols = Math.max(1, Math.floor(w))
            const startSampleSec = isMediaClip(c) ? c.in : 0
            const sampleDur = isMediaClip(c) ? (c.out - c.in) : dur
            for (let px = 0; px < cols; px++) {
              const tSec = startSampleSec + (px / cols) * sampleDur
              const idx = Math.floor(tSec * wave.peaks_per_sec)
              if (idx < 0 || idx >= wave.peaks.length) continue
              const p = wave.peaks[idx]
              const h = Math.max(0.5, p * halfH)
              ctx.fillRect(x + px, baseY - h, 1, h * 2)
            }
            ctx.restore()
          }
        }

        // label inside (drawn on top of the waveform so it stays readable)
        ctx.fillStyle = t.type === 'video' ? '#0e0e10' : 'rgba(0,0,0,0.85)'
        ctx.font = '10px var(--font-ui)'
        const label = isMediaClip(c) ? (c.src.split('/').pop() ?? '') : ('text' in c ? c.text : '')
        const txt = label.slice(0, Math.max(0, Math.floor(w / 6)))
        if (txt) ctx.fillText(txt, x + 6, y + trackHeight / 2 + 3)
        // selection ring
        if (isSel) {
          ctx.strokeStyle = '#fff'
          ctx.lineWidth = 1.5
          ctx.strokeRect(x + 0.5, y + 4 + 0.5, w - 1, trackHeight - 8 - 1)
        }
      }
    }

    // Markers (drawn on the heavy canvas so they live behind the playhead;
    // they don't change every frame).
    const markers = (edl?.markers ?? []) as { id: string; time: number; label: string; color?: string }[]
    for (const m of markers) {
      const mx = labelWidth + m.time * zoom
      if (mx < labelWidth || mx > size.w) continue
      ctx.fillStyle = m.color ?? '#fbbf24'
      ctx.fillRect(mx, headerHeight, 1.5, size.h - headerHeight)
      // diamond at the ruler line
      ctx.beginPath()
      ctx.moveTo(mx, headerHeight - 6)
      ctx.lineTo(mx + 4, headerHeight)
      ctx.lineTo(mx, headerHeight + 6)
      ctx.lineTo(mx - 4, headerHeight)
      ctx.closePath()
      ctx.fill()
      if (m.label) {
        ctx.fillStyle = m.color ?? '#fbbf24'
        ctx.font = '9px var(--font-ui)'
        ctx.fillText(m.label.slice(0, 12), mx + 6, headerHeight - 8)
      }
    }

    // In/out range shading on the ruler
    if (inMark != null || outMark != null) {
      const a = labelWidth + (inMark ?? 0) * zoom
      const b = labelWidth + (outMark ?? (edl?.duration ?? 0)) * zoom
      ctx.fillStyle = 'rgba(91,141,255,0.15)'
      ctx.fillRect(a, headerHeight, b - a, size.h - headerHeight)
      ctx.fillStyle = '#5b8dff'
      if (inMark != null) ctx.fillRect(a, 0, 1.5, size.h)
      if (outMark != null) ctx.fillRect(b, 0, 1.5, size.h)
    }

    // (playhead drawn on a separate cheap overlay canvas — see playheadCanvasRef)
    // (`tracks` is derived from `edl` via useMemo; `edl` is already in deps)
  }, [edl, selection, multiSelection, zoom, size, dpr, waveTick, inMark, outMark])

  // Cheap playhead-only overlay redraw on RAF — avoids re-tessellating the
  // whole timeline 60 times per second while the video plays.
  const playheadCanvasRef = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    const cv = playheadCanvasRef.current
    if (!cv) return
    cv.width = Math.max(1, Math.round(size.w * dpr))
    cv.height = Math.max(1, Math.round(size.h * dpr))
    cv.style.width = `${size.w}px`
    cv.style.height = `${size.h}px`
    const ctx = cv.getContext('2d')!
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    ctx.clearRect(0, 0, size.w, size.h)
    const ph = labelWidth + playhead * zoom
    ctx.strokeStyle = '#ff4d6d'
    ctx.lineWidth = 1.5
    ctx.beginPath()
    ctx.moveTo(ph, 0)
    ctx.lineTo(ph, size.h)
    ctx.stroke()
    ctx.fillStyle = '#ff4d6d'
    ctx.beginPath()
    ctx.moveTo(ph - 5, 0)
    ctx.lineTo(ph + 5, 0)
    ctx.lineTo(ph, 8)
    ctx.closePath()
    ctx.fill()
  }, [playhead, zoom, size, dpr])

  // mouse → seek / select / drag
  function onMouseDown(e: React.MouseEvent) {
    const rect = (e.target as HTMLCanvasElement).getBoundingClientRect()
    const x = e.clientX - rect.left
    const y = e.clientY - rect.top

    // Click in ruler area → seek. Clamp to [0, duration] so clicking the
    // empty ruler past the last clip doesn't send the <video> past its end
    // (which renders as solid black).
    if (y < headerHeight && x > labelWidth) {
      const raw = Math.max(0, (x - labelWidth) / zoom)
      const dur = edl?.duration ?? raw
      setPlayhead(Math.min(raw, dur))
      return
    }

    // Click on the mute square in a track label
    if (x >= 6 && x <= 18) {
      for (let i = 0; i < tracks.length; i++) {
        const ty = trackY(i)
        if (y >= ty + trackHeight / 2 - 6 && y <= ty + trackHeight / 2 + 6) {
          const t = tracks[i]
          void dispatch('set_track_muted', { track: t.id, muted: !t.muted })
          return
        }
      }
    }

    // Hit-test clips
    const hit = hits.find((h) => x >= h.x && x <= h.x + h.w && y >= h.y + 4 && y <= h.y + h.h - 4)
    if (hit) {
      if (e.shiftKey) {
        toggleSelection(hit.clip.id)
        return  // shift-click only toggles, doesn't start a drag
      }
      setSelection(hit.clip.id)
      const edge = 6
      const right = hit.x + hit.w
      let kind: 'move' | 'trim-l' | 'trim-r' = 'move'
      if (x < hit.x + edge) kind = 'trim-l'
      else if (x > right - edge) kind = 'trim-r'
      const c = hit.clip
      dragRef.current = {
        kind,
        clipId: c.id,
        trackId: hit.trackId,
        startX: e.clientX,
        origStart: 'start' in c ? c.start : 0,
        origIn: isMediaClip(c) ? c.in : 0,
        origOut: isMediaClip(c) ? c.out : 0,
      }
    } else {
      setSelection(null)
    }
  }

  function onMouseMove(_e: React.MouseEvent) {
    // Optimistic in-flight drag preview is M1-deferred; we commit on mouseup.
  }

  // Collect all snap targets: clip start, clip end, the playhead. Snap edges
  // to within `snapPx` pixels; converts to seconds via the current zoom.
  const SNAP_PX = 8
  function snapTime(t: number, ignoreClipId?: string): number {
    if (!snapEnabled) return t   // snapping toggled off (keyboard shortcut)
    const candidates: number[] = [0, playhead]
    for (const tk of edl?.tracks ?? []) {
      for (const c of tk.clips) {
        if (ignoreClipId && c.id === ignoreClipId) continue
        const cs = (c as { start?: number }).start ?? 0
        const ce = isMediaClip(c) ? (cs + (c.out - c.in)) : ((c as { end?: number }).end ?? cs)
        candidates.push(cs, ce)
      }
    }
    const snapSec = SNAP_PX / zoom
    let best = t
    let bestDist = snapSec
    for (const cand of candidates) {
      const d = Math.abs(t - cand)
      if (d < bestDist) {
        best = cand
        bestDist = d
      }
    }
    return best
  }

  async function onMouseUp(e: React.MouseEvent) {
    const drag = dragRef.current
    if (!drag) return
    const dx = e.clientX - drag.startX
    const dt = dx / zoom
    dragRef.current = null
    if (Math.abs(dx) < 3) return

    // Figure out target track from the drop Y (cross-track drag)
    const rect = (e.target as HTMLElement).getBoundingClientRect()
    const y = e.clientY - rect.top
    let targetTrackId: string | undefined
    for (let i = 0; i < tracks.length; i++) {
      const ty = trackY(i)
      if (y >= ty && y <= ty + trackHeight) {
        targetTrackId = tracks[i].id
        break
      }
    }

    if (drag.kind === 'move') {
      const rawNewStart = Math.max(0, drag.origStart + dt)
      const newStart = snapTime(rawNewStart, drag.clipId)
      const args: Record<string, unknown> = { clip_id: drag.clipId, new_start: newStart }
      if (targetTrackId && targetTrackId !== drag.trackId) args.new_track = targetTrackId
      await dispatch('move_clip', args)
    } else if (drag.kind === 'trim-l') {
      // Trim-left snaps the visible clip start (= origStart + delta) to neighbors.
      const newClipStart = snapTime(drag.origStart + dt, drag.clipId)
      const deltaApplied = newClipStart - drag.origStart
      const newIn = Math.max(0, drag.origIn + deltaApplied)
      await dispatch('trim_clip', { clip_id: drag.clipId, in: newIn })
    } else if (drag.kind === 'trim-r') {
      const origRight = drag.origStart + (drag.origOut - drag.origIn)
      const newRight = snapTime(origRight + dt, drag.clipId)
      const deltaApplied = newRight - origRight
      const newOut = Math.max(drag.origIn + 0.1, drag.origOut + deltaApplied)
      await dispatch('trim_clip', { clip_id: drag.clipId, out: newOut })
    }
  }

  // Drop from the media bin → add a clip; from the sticker panel → add a sticker.
  function onCanvasDragOver(e: React.DragEvent) {
    if (e.dataTransfer.types.includes('application/x-vai-src')
        || e.dataTransfer.types.includes('application/x-vai-emoji')
        || e.dataTransfer.types.includes('text/plain')) {
      e.preventDefault()
      e.dataTransfer.dropEffect = 'copy'
    }
  }

  async function onCanvasDrop(e: React.DragEvent) {
    e.preventDefault()
    const rect = (e.currentTarget as HTMLElement).getBoundingClientRect()
    const x = e.clientX - rect.left
    const y = e.clientY - rect.top
    if (x < labelWidth) return
    const tDrop = Math.max(0, (x - labelWidth) / zoom)

    // Emoji drop from the StickerPanel — drops a 3-second sticker centered.
    const emoji = e.dataTransfer.getData('application/x-vai-emoji')
    if (emoji) {
      const w = edl?.canvas.w ?? 1080
      const h = edl?.canvas.h ?? 1920
      await dispatch('add_sticker', {
        emoji,
        start: snapTime(tDrop),
        end: snapTime(tDrop) + 3.0,
        position: [w / 2, h * 0.55],
      })
      return
    }

    const src = e.dataTransfer.getData('application/x-vai-src')
              || e.dataTransfer.getData('text/plain')
    if (!src) return
    // Track from y: if the user dropped on a video row (v1 OR v2/etc.), use it;
    // otherwise default to v1.
    let trackId = 'v1'
    for (let i = 0; i < tracks.length; i++) {
      const ty = trackY(i)
      if (y >= ty && y <= ty + trackHeight && tracks[i].type === 'video') {
        trackId = tracks[i].id
        break
      }
    }
    // Probe duration via a quick HEAD-ish request: we don't have one, so we
    // pass out=0 and let the backend default to the source duration if it can,
    // OR just use a placeholder of 5s — better fallback: pass `out=0` only if
    // backend supports it. For safety, dispatch add_clip with a generous dur
    // and let the user trim. Using duration 30s is a reasonable default.
    // Better: query the EDL — if this src is already on the timeline, reuse
    // its duration.
    let dur = 30
    for (const tk of edl?.tracks ?? []) {
      for (const c of tk.clips) {
        if (isMediaClip(c) && c.src === src) {
          dur = c.out - c.in
          break
        }
      }
    }
    await dispatch('add_clip', {
      track: trackId, src, in: 0.0, out: dur, start: snapTime(tDrop),
    })
  }

  function onContextMenu(e: React.MouseEvent) {
    const rect = (e.currentTarget as HTMLElement).getBoundingClientRect()
    const x = e.clientX - rect.left
    const y = e.clientY - rect.top
    const hit = hits.find((h) => x >= h.x && x <= h.x + h.w && y >= h.y + 4 && y <= h.y + h.h - 4)
    if (!hit) return
    e.preventDefault()
    setSelection(hit.clip.id)
    setContextMenu({ x: e.clientX, y: e.clientY, clipId: hit.clip.id, trackId: hit.trackId })
  }

  // Close context menu on outside click or Escape
  useEffect(() => {
    if (!contextMenu) return
    const close = () => setContextMenu(null)
    const onKey = (e: KeyboardEvent) => { if (e.code === 'Escape') close() }
    window.addEventListener('mousedown', close)
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('mousedown', close)
      window.removeEventListener('keydown', onKey)
    }
  }, [contextMenu])

  function onWheel(e: React.WheelEvent) {
    if (e.ctrlKey || e.metaKey) {
      e.preventDefault()
      setZoomStore(zoom * (e.deltaY < 0 ? 1.15 : 1 / 1.15))
    } else if (wrapRef.current) {
      wrapRef.current.scrollLeft += e.deltaX
    }
  }

  return (
    <>
      <div className="timeline-toolbar">
        <span className="small">Zoom</span>
        <input type="range" min={10} max={600} value={zoom} onChange={(e) => setZoomStore(Number(e.target.value))} style={{ width: 120 }} />
        <span className="small">{zoom}px/s</span>
        <div style={{ flex: 1 }} />
        <span className="small">⌘+scroll to zoom · ⌘B split · ⌫ delete · ⌘D duplicate</span>
      </div>
      <div
        className="timeline-canvas-wrap"
        ref={wrapRef}
        style={{ position: 'relative' }}
        onDragOver={onCanvasDragOver}
        onDrop={onCanvasDrop}
      >
        <canvas
          ref={canvasRef}
          onMouseDown={onMouseDown}
          onMouseMove={onMouseMove}
          onMouseUp={onMouseUp}
          onContextMenu={onContextMenu}
          onWheel={onWheel}
          style={{ display: 'block', cursor: 'crosshair' }}
        />
        <canvas
          ref={playheadCanvasRef}
          style={{ position: 'absolute', inset: 0, pointerEvents: 'none' }}
        />
      </div>
      {contextMenu && (
        <div
          onMouseDown={(e) => e.stopPropagation()}
          style={{
            position: 'fixed', left: contextMenu.x, top: contextMenu.y, zIndex: 100,
            background: 'var(--bg-2)', border: '1px solid var(--line)', borderRadius: 6,
            boxShadow: '0 8px 24px rgba(0,0,0,0.5)', minWidth: 180, padding: 4,
          }}
        >
          {[
            { label: 'Split here',   action: () => dispatch('split_at', { track: contextMenu.trackId, time: playhead }) },
            { label: 'Duplicate',    action: () => dispatch('duplicate_clip', { clip_id: contextMenu.clipId }) },
            { label: 'Delete',       action: () => dispatch('ripple_delete', { clip_id: contextMenu.clipId }) },
            { label: 'Mute clip',    action: () => dispatch('set_volume', { target: contextMenu.clipId, db: -60 }) },
            { sep: true },
            { label: 'Mute track',   action: () => dispatch('set_track_muted', { track: contextMenu.trackId }) },
            { label: 'Lock track',   action: () => dispatch('set_track_locked', { track: contextMenu.trackId }) },
            ...(multiSelection.length || (selection && selection !== contextMenu.clipId)
              ? [
                  { sep: true },
                  { label: `Delete ${(selection ? 1 : 0) + multiSelection.length + (selection === contextMenu.clipId ? 0 : 1)} selected`, action: () => {
                      const ids = Array.from(new Set([
                        contextMenu.clipId, selection, ...multiSelection,
                      ].filter(Boolean) as string[]))
                      dispatch('bulk_delete', { clip_ids: ids })
                  } },
                  { label: 'Duplicate selected', action: () => {
                      const ids = Array.from(new Set([
                        contextMenu.clipId, selection, ...multiSelection,
                      ].filter(Boolean) as string[]))
                      dispatch('bulk_duplicate', { clip_ids: ids })
                  } },
                ]
              : []),
          ].map((item, i) => (
            'sep' in item ? (
              <div key={`sep-${i}`} style={{ height: 1, background: 'var(--line)', margin: '4px 0' }} />
            ) : (
              <div
                key={item.label}
                onClick={() => { item.action(); setContextMenu(null) }}
                style={{ padding: '6px 10px', cursor: 'pointer', fontSize: 12, borderRadius: 3 }}
                onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--bg-3)')}
                onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
              >
                {item.label}
              </div>
            )
          ))}
        </div>
      )}
    </>
  )
}

function roundRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  r = Math.min(r, w / 2, h / 2)
  ctx.beginPath()
  ctx.moveTo(x + r, y)
  ctx.arcTo(x + w, y, x + w, y + h, r)
  ctx.arcTo(x + w, y + h, x, y + h, r)
  ctx.arcTo(x, y + h, x, y, r)
  ctx.arcTo(x, y, x + w, y, r)
  ctx.closePath()
}

function niceTick(approx: number): number {
  const candidates = [0.1, 0.2, 0.5, 1, 2, 5, 10, 30, 60, 300, 600]
  for (const c of candidates) if (c >= approx) return c
  return 600
}

function formatTime(t: number): string {
  if (t < 60) return `${t.toFixed(t < 10 ? 1 : 0)}s`
  const m = Math.floor(t / 60)
  const s = Math.floor(t % 60)
  return `${m}:${s.toString().padStart(2, '0')}`
}
