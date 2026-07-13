import { useEffect, useMemo, useRef, useState } from 'react'
import { useStore } from '../store'
import { api } from '../api'
import { toast } from '../toast'
import { isMediaClip, type AnyClip, type Track } from '../types'

// Lane compatibility: which track TYPES a given clip kind may live on. Media
// clips (video/audio files) belong on video-family or audio-family tracks;
// stickers/text belong on their own dedicated track types. Previously
// neither the frontend drop handlers nor the backend enforced this at all —
// dropping a video clip on the captions row, for instance, silently
// redirected to v1 (reading as "nothing happened" for the row the user
// actually aimed at) and a cross-track DRAG could park a media clip on a
// text/sticker/captions track with no feedback, where the renderer then
// silently ignores it entirely (issues 41/42/43, "anything can be placed
// anywhere").
const VIDEO_FAMILY = new Set(['video'])
const AUDIO_FAMILY = new Set(['audio', 'music', 'vo'])
function laneAcceptsMediaClip(trackType: string): boolean {
  return VIDEO_FAMILY.has(trackType) || AUDIO_FAMILY.has(trackType)
}

// Client-side mirror of dispatch.py's `_first_free_gap` — same algorithm, so
// the frontend can show instant feedback (toast) on drop instead of waiting
// for the round-trip, while the backend in move_clip remains the real
// enforcement (Claude/MCP callers bypass this file entirely). Only ever
// called with media clips (isMediaClip) on video/audio-family tracks — a
// dropped media clip landing on an occupied range used to silently stack on
// top of whatever was already there (no data loss, but the canvas drew both
// with identical fill and no distinction, reading as "merged").
function firstFreeGap(
  track: Track, duration: number, preferredStart: number, ignoreClipId: string
): number {
  const occupied = track.clips
    .filter(isMediaClip)
    .filter((c) => c.id !== ignoreClipId)
    .map((c): [number, number] => [c.start, c.start + (c.out - c.in)])
    .sort((a, b) => a[0] - b[0])
  let candidate = Math.max(0, preferredStart)
  const overlaps = (start: number) => {
    const end = start + duration
    return occupied.some(([oStart, oEnd]) => start < oEnd - 1e-9 && end > oStart + 1e-9)
  }
  if (!overlaps(candidate)) return candidate
  for (const [oStart, oEnd] of occupied) {
    if (candidate < oEnd && candidate + duration > oStart) candidate = oEnd
  }
  for (let i = 0; i < occupied.length; i++) {
    if (!overlaps(candidate)) break
    for (const [oStart, oEnd] of occupied) {
      if (candidate < oEnd && candidate + duration > oStart) candidate = oEnd
    }
  }
  return candidate
}

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
  // Brief flash highlight on a newly-added clip (e.g. a fresh voiceover). The
  // highlight is drawn in the heavy canvas while flashClipId is set; the store
  // clears it after ~600ms, which redraws without it (a flash, no per-frame RAF).
  const flashClipId = useStore((s) => s.flashClipId)

  // drag state for moving / trimming clips
  const dragRef = useRef<null | {
    kind: 'move' | 'trim-l' | 'trim-r' | 'playhead'
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

  // The canvas is sized to the full timeline CONTENT, not the viewport — the
  // wrapper scrolls it natively (overflow:auto). Previously the canvas was
  // sized to the viewport (`size.w`/`size.h`) and anything past that was
  // simply never drawn (`if (x > size.w) break`), so there was nothing to
  // scroll to: only ctrl/meta+wheel zoom worked. `Math.max(size.w, …)` keeps
  // a short timeline filling the visible pane instead of leaving a gap.
  const labelWidth = 80
  const trackHeight = 36
  const headerHeight = 24
  const contentW = Math.max(size.w, labelWidth + ((edl?.duration ?? 0) + 30) * zoom)
  // contentH is the CONTENT height only (no `Math.max(size.h, …)`) — the wrap
  // is `flex:1; overflow:auto` and handles the viewport itself. Clamping to
  // size.h here used to make contentH transiently equal a stale/large
  // viewport height right after a panel resize (or whenever there are fewer
  // rows than fit), which made `wrap.scrollHeight <= wrap.clientHeight` even
  // when the wrap box was genuinely smaller than the rows — i.e. the browser
  // never saw real vertical overflow to scroll, which is part of why plain
  // vertical wheel "did nothing" further down in onWheel.
  const contentH = headerHeight + tracks.length * (trackHeight + 4) + 4

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
    // Sized to the full CONTENT (contentW/contentH), not the viewport — the
    // wrapper's native overflow:auto scrolls it. This is what makes the
    // timeline scrollable at all: previously the canvas was viewport-sized
    // and anything past the visible edge was never drawn in the first place.
    cv.width = contentW * dpr
    cv.height = contentH * dpr
    cv.style.width = `${contentW}px`
    cv.style.height = `${contentH}px`
    const ctx = cv.getContext('2d')!
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    ctx.clearRect(0, 0, contentW, contentH)

    // bg
    ctx.fillStyle = '#16161a'
    ctx.fillRect(0, 0, contentW, contentH)

    // ruler
    ctx.fillStyle = '#1d1d22'
    ctx.fillRect(labelWidth, 0, contentW - labelWidth, headerHeight)
    ctx.font = '10px var(--font-ui)'
    ctx.fillStyle = '#9b9ba5'
    const dur = edl?.duration ?? 0
    const pixelsPerTick = 80
    const tickSec = niceTick(pixelsPerTick / zoom)
    for (let t = 0; t <= dur + 30; t += tickSec) {
      const x = labelWidth + t * zoom
      if (x > contentW) break
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
      ctx.fillRect(labelWidth, y, contentW - labelWidth, trackHeight)
      ctx.strokeStyle = '#22222a'
      ctx.strokeRect(labelWidth + 0.5, y + 0.5, contentW - labelWidth - 1, trackHeight - 1)

      // clips
      const showWaveOn = ['video', 'audio', 'music', 'vo'].includes(t.type)
      // Defense-in-depth: track.clips is sorted by start after every backend
      // mutation and move_clip/add_super_text now actively prevent new
      // overlaps, but legacy data (an EDL saved before this fix, or a
      // same-track/role case the guards don't cover) can still carry two
      // overlapping clips on one track — previously drawn with identical
      // fill and no distinction at all, reading as silently "merged". Track
      // each clip's [start,end) as it's drawn and flag one that overlaps any
      // clip already drawn on this row so it gets a visible warning outline
      // below, instead of being invisible.
      const seenRanges: [number, number][] = []
      for (const c of t.clips) {
        const start = isMediaClip(c) ? c.start : c.start
        const dur = isMediaClip(c) ? c.out - c.in : c.end - c.start
        const end = start + dur
        const overlapsPrior = seenRanges.some(([s, e]) => start < e - 1e-9 && end > s + 1e-9)
        seenRanges.push([start, end])
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
        // Overlap warning: the later clip (in start order) of an overlapping
        // pair on this track gets a dashed amber border so it's never
        // invisibly merged with its neighbor, even for legacy/pre-guard data.
        if (overlapsPrior) {
          ctx.save()
          ctx.strokeStyle = '#f59e0b'
          ctx.lineWidth = 2
          ctx.setLineDash([4, 3])
          ctx.strokeRect(x + 1, y + 4 + 1, w - 2, trackHeight - 8 - 2)
          ctx.restore()
        }
        // new-clip flash: a bright highlight while the clip is flashing. The
        // store clears flashClipId after ~600ms, which redraws without it — so
        // the highlight appears then disappears (a flash) with no per-frame RAF.
        if (c.id === flashClipId) {
          ctx.save()
          ctx.globalAlpha = 0.45
          ctx.fillStyle = '#ffffff'
          roundRect(ctx, x, y + 4, w, trackHeight - 8, 4)
          ctx.fill()
          ctx.globalAlpha = 1
          ctx.lineWidth = 2.5
          ctx.strokeStyle = '#5b8dff'
          roundRect(ctx, x, y + 4, w, trackHeight - 8, 4)
          ctx.stroke()
          ctx.restore()
        }
      }
    }

    // Markers (drawn on the heavy canvas so they live behind the playhead;
    // they don't change every frame).
    const markers = (edl?.markers ?? []) as { id: string; time: number; label: string; color?: string }[]
    for (const m of markers) {
      const mx = labelWidth + m.time * zoom
      if (mx < labelWidth || mx > contentW) continue
      ctx.fillStyle = m.color ?? '#fbbf24'
      ctx.fillRect(mx, headerHeight, 1.5, contentH - headerHeight)
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
      ctx.fillRect(a, headerHeight, b - a, contentH - headerHeight)
      ctx.fillStyle = '#5b8dff'
      if (inMark != null) ctx.fillRect(a, 0, 1.5, contentH)
      if (outMark != null) ctx.fillRect(b, 0, 1.5, contentH)
    }

    // (playhead drawn on a separate cheap overlay canvas — see playheadCanvasRef)
    // (`tracks` is derived from `edl` via useMemo; `edl` is already in deps)
  }, [edl, selection, multiSelection, zoom, size, contentW, contentH, dpr, waveTick, inMark, outMark, flashClipId])

  // Sticky track-label column. The main canvas draws labels at its own x=0,
  // but that canvas is the thing that SCROLLS (contentW-sized) — so once the
  // user scrolls right to see later footage, the labels scroll away with it
  // and there's no way to tell which row is which. This small canvas is
  // exactly labelWidth wide, `position: absolute` and re-translated to track
  // wrapRef.scrollLeft on every scroll (see the effect below `onWheel`), so
  // it stays pinned to the visible left edge while the main canvas scrolls
  // underneath/behind it.
  const labelCanvasRef = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    const cv = labelCanvasRef.current
    if (!cv) return
    cv.width = Math.max(1, Math.round(labelWidth * dpr))
    cv.height = Math.max(1, Math.round(contentH * dpr))
    cv.style.width = `${labelWidth}px`
    cv.style.height = `${contentH}px`
    const ctx = cv.getContext('2d')!
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    ctx.clearRect(0, 0, labelWidth, contentH)
    // Ruler-row corner (matches the main canvas's ruler background so the
    // seam between the two canvases is invisible).
    ctx.fillStyle = '#1d1d22'
    ctx.fillRect(0, 0, labelWidth, headerHeight)
    for (let i = 0; i < tracks.length; i++) {
      const t = tracks[i]
      const y = trackY(i)
      ctx.fillStyle = '#1d1d22'
      ctx.fillRect(0, y, labelWidth, trackHeight)
      ctx.fillStyle = t.muted ? '#5a5a64' : '#9b9ba5'
      ctx.font = '11px var(--font-ui)'
      ctx.fillText(t.label ?? t.id, 22, y + trackHeight / 2 + 4)

      const muteX = 6
      const muteY = y + trackHeight / 2 - 6
      ctx.fillStyle = t.muted ? '#ff4d6d' : '#3a3a44'
      ctx.fillRect(muteX, muteY, 12, 12)
      ctx.fillStyle = t.muted ? '#fff' : '#9b9ba5'
      ctx.font = 'bold 9px var(--font-ui)'
      ctx.fillText('M', muteX + 3, muteY + 9)
    }
  }, [tracks, contentH, dpr])

  // Cheap playhead-only overlay redraw — avoids re-tessellating the whole
  // timeline 60×/s while the video plays. Sized to the same full CONTENT
  // dimensions as the main canvas (not the viewport) so it scrolls in lockstep
  // with it inside the shared wrapper, instead of the two disagreeing about
  // where x=0 is once the wrapper is scrolled.
  const playheadCanvasRef = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    const cv = playheadCanvasRef.current
    if (!cv) return
    cv.width = Math.max(1, Math.round(contentW * dpr))
    cv.height = Math.max(1, Math.round(contentH * dpr))
    cv.style.width = `${contentW}px`
    cv.style.height = `${contentH}px`
    const ctx = cv.getContext('2d')!
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    ctx.clearRect(0, 0, contentW, contentH)
    const ph = labelWidth + playhead * zoom
    ctx.strokeStyle = '#ff4d6d'
    ctx.lineWidth = 1.5
    ctx.beginPath()
    ctx.moveTo(ph, 0)
    ctx.lineTo(ph, contentH)
    ctx.stroke()
    ctx.fillStyle = '#ff4d6d'
    ctx.beginPath()
    ctx.moveTo(ph - 5, 0)
    ctx.lineTo(ph + 5, 0)
    ctx.lineTo(ph, 8)
    ctx.closePath()
    ctx.fill()
  }, [playhead, zoom, contentW, contentH, dpr])


  // mouse → seek / select / drag
  function onMouseDown(e: React.MouseEvent) {
    const rect = (e.target as HTMLCanvasElement).getBoundingClientRect()
    const x = e.clientX - rect.left
    const y = e.clientY - rect.top

    // Click in the ruler, OR within a few px of the current playhead line
    // anywhere in the track area, starts a DRAG (live-scrubbing via
    // onMouseMove below) rather than a single one-shot seek. Previously the
    // playhead could only be moved by a click landing in the 24px-tall ruler
    // strip — a tiny target — and there was no way to drag it at all (the
    // playhead itself is a separate pointer-events:none overlay canvas, and
    // onMouseMove was an intentional no-op). Clamp to [0, duration] so
    // scrubbing past the last clip doesn't send the <video> past its end.
    const playheadX = labelWidth + playhead * zoom
    const nearPlayhead = Math.abs(x - playheadX) <= 5
    if ((y < headerHeight && x > labelWidth) || (x > labelWidth && nearPlayhead)) {
      dragRef.current = {
        kind: 'playhead',
        clipId: '', trackId: '',
        startX: e.clientX, origStart: 0, origIn: 0, origOut: 0,
      }
      const raw = Math.max(0, (x - labelWidth) / zoom)
      const dur = edl?.duration ?? raw
      setPlayhead(Math.min(raw, dur))
      return
    }

    // Note: the mute-toggle click (x < labelWidth) is handled by the sticky
    // label canvas's own onLabelMouseDown — that canvas sits ON TOP of this
    // one (z-index) at the label column regardless of scroll position, so a
    // click there never reaches this handler at all.

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
      // Empty timeline area click also seeks the playhead to the clicked
      // position, matching CapCut/Premiere. Only below the ruler (already
      // guaranteed here — the ruler/near-playhead branch above returns
      // early) and right of the label column, so clicking the label/mute
      // area never seeks. Clip clicks (the `if (hit)` branch above) remain
      // select-only — this is deliberately scoped to empty space per the
      // approved design (seek-on-empty-space-click only, not on-clip).
      if (x > labelWidth) {
        const raw = Math.max(0, (x - labelWidth) / zoom)
        const dur = edl?.duration ?? raw
        setPlayhead(Math.min(raw, dur))
      }
    }
  }

  function onMouseMove(_e: React.MouseEvent) {
    // Clip move/trim previews are commit-on-release (dt computed in onMouseUp
    // from the total drag distance) — deliberately unchanged here. Playhead
    // scrubbing is the one case that needs LIVE feedback while dragging, and
    // is handled by the window-level listener below so it keeps working even
    // if the pointer leaves the canvas mid-drag.
  }

  // Window-level drag listeners: a canvas-only onMouseMove/onMouseUp binding
  // means a drag that leaves the canvas bounds (dragging fast, or wide
  // gestures on a small viewport) never receives its mouseup and gets stuck.
  // Binding to `window` for the lifetime of any active drag fixes both that
  // AND lets playhead scrubbing live-update as the pointer moves.
  useEffect(() => {
    function onWindowMouseMove(e: MouseEvent) {
      const drag = dragRef.current
      if (!drag || drag.kind !== 'playhead' || !canvasRef.current) return
      const rect = canvasRef.current.getBoundingClientRect()
      const x = e.clientX - rect.left
      const raw = Math.max(0, (x - labelWidth) / zoom)
      const dur = edl?.duration ?? raw
      setPlayhead(Math.min(raw, dur))
    }
    function onWindowMouseUp(e: MouseEvent) {
      if (dragRef.current?.kind === 'playhead') {
        dragRef.current = null
        return
      }
      // Non-playhead drags (clip move/trim) are committed by the canvas's own
      // onMouseUp React handler in the normal case; this only catches the
      // case where the pointer was released OUTSIDE the canvas, which the
      // canvas-scoped handler would never see at all.
      if (dragRef.current && canvasRef.current && !canvasRef.current.contains(e.target as Node)) {
        void onMouseUp(e as unknown as React.MouseEvent)
      }
    }
    window.addEventListener('mousemove', onWindowMouseMove)
    window.addEventListener('mouseup', onWindowMouseUp)
    return () => {
      window.removeEventListener('mousemove', onWindowMouseMove)
      window.removeEventListener('mouseup', onWindowMouseUp)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [zoom, edl?.duration])

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
      let newStart = snapTime(rawNewStart, drag.clipId)
      const args: Record<string, unknown> = { clip_id: drag.clipId, new_start: newStart }
      let destTrack = tracks.find((t) => t.id === drag.trackId)
      if (targetTrackId && targetTrackId !== drag.trackId) {
        // Only follow the clip onto a track whose TYPE can actually hold it.
        // The clip's own kind is inferred from its ORIGIN track (a media clip
        // only ever lives on a video/audio-family track to begin with) —
        // dragging it onto e.g. the captions/stickers row used to silently
        // move it there with `move_clip`, where the renderer then just
        // ignores it entirely (collect_text_clips/collect_stickers only
        // look at their own track types), reading as "it vanished".
        const originType = tracks.find((t) => t.id === drag.trackId)?.type
        const targetType = tracks.find((t) => t.id === targetTrackId)?.type
        const originIsMediaFamily = originType ? laneAcceptsMediaClip(originType) : true
        const targetIsCompatible = targetType
          ? (originIsMediaFamily ? laneAcceptsMediaClip(targetType) : targetType === originType)
          : true
        if (targetIsCompatible) {
          args.new_track = targetTrackId
          destTrack = tracks.find((t) => t.id === targetTrackId)
        } else {
          toast.error(`Can't move this clip to the "${targetType}" lane — it stayed on "${originType}".`)
        }
      }
      // Cross-track (or same-track) overlap guard, mirrored client-side for
      // instant feedback. move_clip on the backend is the real enforcement
      // (Claude/MCP callers reach it directly and bypass this file), but
      // without this mirror the UI would show the pre-snap position for one
      // render before the server-computed snapped position arrives — a
      // visible "jump". Only media clips have this check (drag.origIn/origOut
      // are only meaningful for a media Clip — see onMouseDown, where a
      // Sticker/TextClip hit sets them to 0/0); a dropped Sticker/TextClip
      // has no analogous cross-track drop-overlap path.
      const originTrack = tracks.find((t) => t.id === drag.trackId)
      const draggedIsMedia = !!originTrack
        && originTrack.clips.some((c) => c.id === drag.clipId && isMediaClip(c))
      if (destTrack && draggedIsMedia) {
        const dur = drag.origOut - drag.origIn
        const snapped = firstFreeGap(destTrack, dur, newStart, drag.clipId)
        if (Math.abs(snapped - newStart) > 1e-6) {
          newStart = snapped
          args.new_start = newStart
          toast.info(`Snapped to the nearest free gap on "${destTrack.label ?? destTrack.id}" (${newStart.toFixed(2)}s) to avoid overlapping an existing clip.`)
        }
      }
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
    // Find whichever row the drop actually landed on (any type, not just
    // video) so we can tell an incompatible-lane drop apart from "dropped
    // below all rows" and give real feedback instead of a silent redirect.
    let droppedOnTrackId: string | undefined
    let droppedOnTrackType: string | undefined
    for (let i = 0; i < tracks.length; i++) {
      const ty = trackY(i)
      if (y >= ty && y <= ty + trackHeight) {
        droppedOnTrackId = tracks[i].id
        droppedOnTrackType = tracks[i].type
        break
      }
    }
    let trackId = 'v1'
    if (droppedOnTrackType && laneAcceptsMediaClip(droppedOnTrackType)) {
      trackId = droppedOnTrackId!
    } else if (droppedOnTrackType) {
      // Landed on an incompatible row (text/sticker/captions/effect) — say
      // so and fall back to v1, rather than silently placing it there with
      // no indication the drop target was wrong.
      toast.error(`Media can't go on the "${droppedOnTrackType}" lane — added to the main video track instead.`)
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

  // Wheel handling is wired as a NATIVE (non-React) listener with
  // `{ passive: false }`, not a JSX `onWheel` prop. React attaches its
  // synthetic `wheel`/`touchstart`/`touchmove` root listeners as PASSIVE by
  // default (matching the browsers' own scroll-performance intervention),
  // so `e.preventDefault()` inside a JSX `onWheel` handler is a silent
  // no-op — Chrome logs "Unable to preventDefault inside passive event
  // listener invocation" and the native scroll/zoom-page gesture still
  // fires underneath whatever the handler computed. That was already
  // silently broken for the old plain-wheel→horizontal-pan mapping and the
  // ⌘/Ctrl+wheel zoom guard; it would have equally broken the new
  // shift-always-horizontal branch below. A manually-attached listener can
  // opt out of passive mode, so `preventDefault()` actually suppresses the
  // browser's native scroll/page-zoom when we want to fully own the gesture
  // (⌘/Ctrl zoom, shift-pan, and the no-vertical-overflow horizontal-pan
  // fallback) while still allowing it to fall through untouched for the
  // vertical-scroll-wins case (we simply don't call preventDefault there).
  useEffect(() => {
    const cv = canvasRef.current
    if (!cv) return
    function handleWheel(e: WheelEvent) {
      // ⌘/Ctrl+wheel always zooms, regardless of vertical overflow.
      if (e.ctrlKey || e.metaKey) {
        e.preventDefault()
        setZoomStore(zoom * (e.deltaY < 0 ? 1.15 : 1 / 1.15))
        return
      }
      const wrap = wrapRef.current
      if (!wrap) return
      // Shift+wheel is an explicit "pan horizontally" gesture — always honor
      // it, even when rows overflow vertically (matches every NLE's
      // shift-scrub convention).
      if (e.shiftKey) {
        e.preventDefault()
        wrap.scrollLeft += e.deltaY || e.deltaX
        return
      }
      // Previously a plain vertical wheel was UNCONDITIONALLY converted to
      // horizontal `scrollLeft` with `preventDefault()`, so a lower track
      // row (e.g. captions) was reachable only via scrollbar-drag or an
      // accidentally-horizontal trackpad gesture — vertical scroll never
      // won, even when there were more rows than fit. The fix: when the
      // wrap box genuinely overflows vertically AND the gesture is
      // vertical-dominant, let the browser's native vertical scroll happen
      // (no preventDefault) so rows below the fold become reachable. Only
      // fall back to the horizontal-pan mapping when there's nothing to
      // scroll vertically to.
      const canScrollV = wrap.scrollHeight > wrap.clientHeight
      if (canScrollV && Math.abs(e.deltaY) >= Math.abs(e.deltaX)) {
        return // native vertical scroll — do not preventDefault
      }
      // No vertical overflow (or a horizontal-dominant gesture): map
      // vertical wheel → horizontal pan, matching every timeline editor's
      // convention (CapCut, Premiere, FCP all scroll the timeline
      // horizontally on a plain wheel when there's nothing below the fold
      // to scroll to).
      if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
        e.preventDefault()
        wrap.scrollLeft += e.deltaY
      }
    }
    cv.addEventListener('wheel', handleWheel, { passive: false })
    return () => cv.removeEventListener('wheel', handleWheel)
  }, [zoom, setZoomStore])

  // Keep the sticky label canvas pinned to the wrapper's visible left edge as
  // it scrolls horizontally (translateX cancels out scrollLeft). A plain CSS
  // `position: sticky` doesn't work here because the label canvas needs to
  // OVERLAY the main canvas at the same row positions, not stack after it in
  // normal flow — so this is a small manual re-implementation of "sticky"
  // using `position: absolute` + a scroll listener instead.
  useEffect(() => {
    const wrap = wrapRef.current
    const label = labelCanvasRef.current
    if (!wrap || !label) return
    const onScroll = () => { label.style.transform = `translateX(${wrap.scrollLeft}px)` }
    onScroll()
    wrap.addEventListener('scroll', onScroll)
    return () => wrap.removeEventListener('scroll', onScroll)
  }, [contentW])

  // Mute-toggle click on the sticky label canvas. Coordinates here are
  // already relative to the label canvas's own (unscrolled) origin, so no
  // scrollLeft adjustment is needed — unlike the main canvas's onMouseDown.
  function onLabelMouseDown(e: React.MouseEvent) {
    const rect = (e.target as HTMLCanvasElement).getBoundingClientRect()
    const x = e.clientX - rect.left
    const y = e.clientY - rect.top
    if (x < 6 || x > 18) return
    for (let i = 0; i < tracks.length; i++) {
      const ty = trackY(i)
      if (y >= ty + trackHeight / 2 - 6 && y <= ty + trackHeight / 2 + 6) {
        void dispatch('set_track_muted', { track: tracks[i].id, muted: !tracks[i].muted })
        return
      }
    }
  }

  return (
    <>
      <div className="timeline-toolbar">
        <span className="small">Zoom</span>
        <input type="range" min={10} max={600} value={zoom} onChange={(e) => setZoomStore(Number(e.target.value))} style={{ width: 120 }} />
        <span className="small">{zoom}px/s</span>
        <div style={{ flex: 1 }} />
        <span className="small">scroll to pan · ⌘+scroll to zoom · ⌘B split · ⌫ delete · ⌘D duplicate</span>
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
          style={{ display: 'block', cursor: 'crosshair' }}
        />
        <canvas
          ref={playheadCanvasRef}
          style={{ position: 'absolute', top: 0, left: 0, pointerEvents: 'none' }}
        />
        {/* Sticky label column: absolutely positioned and re-translated to
            track wrapRef's scrollLeft on every scroll event (see the effect
            below), so track names + mute toggles stay pinned to the visible
            left edge while the main canvas scrolls underneath. Handles its
            own mousedown for the mute toggle (the only interactive control in
            the label area) since it visually sits on top of the main canvas
            once scrolled. */}
        <canvas
          ref={labelCanvasRef}
          onMouseDown={onLabelMouseDown}
          style={{ position: 'absolute', top: 0, left: 0, display: 'block', zIndex: 1, cursor: 'default' }}
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
