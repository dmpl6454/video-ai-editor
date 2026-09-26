import { Fragment, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { flushSync } from 'react-dom'
import { useStore } from '../store'
import { api } from '../api'
import { toast } from '../toast'
import { isMediaClip, isTextClip, clipDuration, clipEnd, clipSpeedFactor, type AnyClip, type Track } from '../types'
import * as dragResolve from '../lib/dragResolve'
import * as dv from '../lib/dragVisuals'
import { baseName, isAudioPath } from '../lib/paths'
import { keyframeTimes } from '../lib/overlay'
import {
  drawnSpan, edlTimeFromOutput, layoutTime, renderTime, v1Layout,
  type LayoutClip, type V1Layout,
} from '../lib/timelineLayout'
import { TransitionPopover, type TransitionInfo } from './TransitionPopover'
import { splitTimeFor } from '../lib/splitTargets'
import { freezeAtPlayhead, planFreeze } from '../lib/freezeFrame'
import { useSpeedCatalog } from '../lib/speed/speedCatalog'
import { v1CutPoints } from '../lib/cutPoints'
import { chordLabel, IS_MAC, useKeymapStore } from '../keymap/engine'
import { undoTitle } from '../lib/undoHorizon'
import { contentTransform, spanVisible, viewportCanvasSize, visibleColumns } from '../lib/timelineViewport'
import { cssToken, uiFont } from '../lib/themeTokens'
import { itemsFor, namesBySrc, offlineSrcs, useMediaNames } from '../lib/mediaNames'
import { clipGainAt, waveColumn, type ClipAudio, type WaveData } from '../lib/waveformDraw'
import { channelColumns, channelMode } from '../lib/audioChannels'
import { isHeard, laneNameBaseline, monitorButtons, monitorLabels } from '../lib/trackMonitor'
import { useMenuA11y } from '../lib/useMenuA11y'
import { formatTimecode, rulerLabelsUnder, rulerTicks } from '../lib/timecode'
import { projectFps as projectFpsOf, toFrameGrid } from '../lib/frameStep'
import {
  anchoredScroll, registerTimelineView, sliderToZoom, SLIDER_STEPS, visibleSpanLabel, zoomToSlider,
} from '../lib/timelineZoom'
import {
  clipLabel, ghostTarget, isGhostLane, laneHasSound, laneName, laneRows, type GhostKind,
} from '../lib/timelineLanes'
import { cursorFor, hitTest, type CutMark, type Hit, type HitBox } from '../lib/timelineHit'
import { snapLabel, snapTargets, snapTo, type SnapKind, type SnapResult } from '../lib/snap'
import { filmstripTiles, spriteSlot, spriteUrl, SPRITE_TILES, ThumbQueue, type TileSpec } from '../lib/filmstrip'
import { createPreviewGate } from '../lib/previewGate'
import { isMarqueeDrag, marqueeHits, marqueeRect } from '../lib/marquee'
import { wheelAction } from '../lib/timelineWheel'
import { drawnCuts, hoveredCut } from '../lib/cutHover'
import { uploadGhosts } from '../lib/uploadGhosts'
import { collides, fitLabel, LABEL_PLATE_ALPHA, labelPlate, laneTooltipHead, markerChips, stickyLabelX } from '../lib/timelineLabels'
import { claimFileDrop, isFileDrag, setTimelineFileDragOver } from '../lib/fileDrop'
import { dropFilesOnLane } from '../lib/laneDrop'
import { COMMAND_BY_ID } from '../keymap/commands'
import { TimelineIcon } from './TimelineIcons'
import { drawIcon } from '../lib/icons'
import { Icon } from './Icon'
import { TimecodeField } from './TimecodeField'

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
    .map((c): [number, number] => [c.start, clipEnd(c)])
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
const WAVE_CACHE = new Map<string, WaveData>()
const WAVE_INFLIGHT = new Map<string, Promise<void>>()

// Filmstrip image cache. Tiles come out of SPRITES (QA-059): one
// GET /api/sessions/{sid}/thumbstrip returns SPRITE_TILES frames of one source
// on lib/filmstrip's zoom-stable grid, keyed `S|src|step|page|h` — a 12-min
// clip's whole strip is one or two requests, not one ffmpeg spawn per tile.
// A tile whose window holds no grid point (a clip shorter than one step at
// this zoom) falls back to a single /thumb, keyed `T|src|t|h`. Requests go
// through THUMB_QUEUE: at most two in flight, visible tiles only, a failed
// key never retried in-session, and nothing at all until the preview has
// loaded (lib/previewGate). JPEG, 1 h browser cache.
const THUMB_H = 72          // 2× the 36px row height for retina; server caps at 270
const THUMB_CACHE = new Map<string, HTMLImageElement>()
// key → the source it shows and how many tiles wide the image is.
const THUMB_META = new Map<string, { src: string; tiles: number }>()
let thumbRepaint: (() => void) | null = null
// Once the preview has loaded in this page, later mounts never wait again.
let previewGateOpened = false
const THUMB_QUEUE = new ThumbQueue((key, url, done) => {
  const img = new Image()
  img.decoding = 'async'
  ;(img as unknown as { fetchPriority?: string }).fetchPriority = 'low'
  img.onload = () => {
    THUMB_CACHE.set(key, img)
    const meta = THUMB_META.get(key)
    if (meta && !THUMB_ASPECT.has(meta.src) && img.height > 0) {
      THUMB_ASPECT.set(meta.src, img.width / meta.tiles / img.height)
    }
    done(true)
    thumbRepaint?.()
  }
  img.onerror = () => done(false)
  img.src = url
}, 2)
// src → natural aspect (w/h) of its thumbs, learned from the first loaded
// image so portrait sources get proportionally narrower tiles (no letterbox).
const THUMB_ASPECT = new Map<string, number>()

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

// Human-readable purpose per track — keyed by the standard empty_edl() ids
// first, falling back to type for imported/nonstandard EDLs. A <canvas> row
// can't carry its own `title`, so this feeds the label column's hover tooltip
// and the drop-preview caption instead.
const TRACK_PURPOSE_BY_ID: Record<string, string> = {
  v1: 'Main video — drop footage here',
  v2: 'PIP overlay video — drop a clip here for picture-in-picture',
  a1: 'Main audio',
  music: 'Background music — drop an audio file here or use "Add music…"',
  vo: 'Voiceover — record via the Voiceover panel',
  tx_hook: 'Hook text',
  tx_super: 'Super text',
  tx_lt: 'Lower thirds',
  stickers: 'Stickers — drag an emoji from the Stickers panel',
  captions: 'Captions — generated by Auto captions',
}
const TRACK_PURPOSE_BY_TYPE: Record<string, string> = {
  video: 'Video',
  audio: 'Audio',
  music: 'Background music',
  vo: 'Voiceover',
  text: 'Text overlays',
  sticker: 'Stickers',
  captions: 'Captions',
}
function trackPurpose(t: Track): string {
  return TRACK_PURPOSE_BY_ID[t.id] ?? TRACK_PURPOSE_BY_TYPE[t.type] ?? t.type
}
// types.ts's Track omits `locked` (hand-mirrored schema, incomplete on
// purpose) — read via the repo's established cast pattern.
function isTrackLocked(t: Track): boolean {
  return !!(t as unknown as { locked?: boolean }).locked
}

interface HitClip { trackId: string; clip: AnyClip; x: number; y: number; w: number; h: number }

export function Timeline() {
  const edl = useStore((s) => s.edl)
  const sid = useStore((s) => s.sessionId)
  // QA-045 / QA-095: clip labels use the user's file names (the media
  // library's), and a clip whose file is missing is drawn as offline.
  const mediaLibrary = useMediaNames((s) => itemsFor(s, sid))
  const mediaNames = useMemo(() => namesBySrc(mediaLibrary), [mediaLibrary])
  const offline = useMemo(() => offlineSrcs(mediaLibrary), [mediaLibrary])
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
  // Clamp the context menu into the viewport AFTER it renders (useLayoutEffect
  // below): the raw click point is stored in `contextMenu`, but on lower
  // tracks in a short timeline pane the fixed-position menu's items fall
  // below the viewport. Measuring the real rendered box (multi-select adds
  // items, so the height varies) beats estimating; useLayoutEffect mutates
  // the style before paint, so there is no unclamped flash. The window-level
  // mousedown close listener is attached in a useEffect after open, so this
  // pre-paint adjustment cannot self-close the menu.
  const contextMenuRef = useRef<HTMLDivElement>(null)
  useLayoutEffect(() => {
    if (!contextMenu) return
    const el = contextMenuRef.current
    if (!el) return
    const r = el.getBoundingClientRect()
    const left = Math.max(8, Math.min(contextMenu.x, window.innerWidth - r.width - 8))
    const top = Math.max(8, Math.min(contextMenu.y, window.innerHeight - r.height - 8))
    el.style.left = `${left}px`
    el.style.top = `${top}px`
  }, [contextMenu])

  const wrapRef = useRef<HTMLDivElement>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  // The clip context menu's keyboard contract (lib/useMenuA11y, QA-102): focus
  // enters it, arrows move, Escape closes, and focus returns to the timeline
  // canvas (focusable now — Shift+F10 / the menu key open this menu for the
  // selected clip, see onCanvasKeyDown).
  const ctxA11y = useMenuA11y({
    open: !!contextMenu, mode: 'menu',
    menuRef: contextMenuRef, triggerRef: canvasRef, onClose: () => setContextMenu(null),
  })
  // The content-sized scroll-extent element (QA-024). The canvases are only
  // VIEWPORT-sized now and stick to the visible pane, so this — which scrolls
  // natively — is what pointer positions are measured against: its rect gives
  // CONTENT coordinates, the same space the draw loop and hit-testing use.
  const contentRef = useRef<HTMLDivElement>(null)
  const contentRect = (): DOMRect =>
    (contentRef.current ?? canvasRef.current!).getBoundingClientRect()
  // wrap.scrollLeft, mirrored into state so the viewport canvases redraw the
  // newly visible slice of the timeline on every scroll.
  const [scrollX, setScrollX] = useState(0)
  // Zoom + snap live in the store so keyboard shortcuts can drive them.
  const zoom = useStore((s) => s.timelineZoom)
  const setZoomStore = useStore((s) => s.setTimelineZoom)
  const snapEnabled = useStore((s) => s.snapEnabled)
  // Undo/Redo + transport now live in this toolbar (see the JSX below).
  const undoDepth = useStore((s) => s.undoDepth)
  const redoAvailable = useStore((s) => s.redoAvailable)
  const isPlaying = useStore((s) => s.isPlaying)
  const setPlaying = useStore((s) => s.setPlaying)
  const [dpr] = useState(window.devicePixelRatio || 1)
  const [size, setSize] = useState({ w: 800, h: 240 })
  const [waveTick, setWaveTick] = useState(0)  // bump to force redraw when peaks arrive
  const [thumbTick, setThumbTick] = useState(0)  // bump to force redraw when a filmstrip tile loads
  // Transition popover: opened by clicking a cut-point affordance on v1.
  // x/y are VIEWPORT coords (e.clientX/Y — the popover is position:fixed,
  // same convention as contextMenu below); `at` is the cut's timeline second.
  const [transPopover, setTransPopover] = useState<null | {
    x: number; y: number; at: number; existing: TransitionInfo | null
  }>(null)
  // Brief flash highlight on a newly-added clip (e.g. a fresh voiceover). The
  // highlight is drawn in the heavy canvas while flashClipId is set; the store
  // clears it after ~600ms, which redraws without it (a flash, no per-frame RAF).
  const flashClipId = useStore((s) => s.flashClipId)

  // Bumped on each pointer move during a clip drag to trigger the drag-overlay
  // redraw (rAF-coalesced). 0 when idle so no per-frame work happens unless a
  // drag is active — same "only redraw while interacting" posture as playhead.
  const [dragTick, setDragTick] = useState(0)
  const dragRafRef = useRef<number | null>(null)

  // Live drop position for a native HTML5 drag from the media/sticker panels
  // (separate from pointer-drag `dragRef`). null when no panel drag is over
  // the canvas. Drives the same insertion-line/target-row overlay. `kind`
  // (readable from dataTransfer.types even during dragover, when the data
  // itself isn't) lets the overlay show lane compatibility + a landing caption.
  // `file` is an OS file drag (QA-093): it lands on the lane under it too.
  const dndOverRef = useRef<null | { x: number; y: number; kind: 'media' | 'sticker' | 'file'; audio?: boolean }>(null)

  // drag state for moving / trimming clips
  const dragRef = useRef<null | {
    kind: 'move' | 'trim-l' | 'trim-r' | 'playhead'
    clipId: string
    trackId: string
    startX: number
    origStart: number
    origIn: number
    origOut: number
    offsetX: number          // grab point within the clip (px)
    pointerX: number         // live cursor X (canvas-space px)
    pointerY: number         // live cursor Y (canvas-space px)
    modifier: boolean        // altKey at grab-time (media speed vs trim)
    clipKind: 'media' | 'text' | 'sticker'  // cached from the hit clip
    // A trim grabbed on a cut's transition bowtie (QA-051): released without
    // moving, it opens the transition popover for that cut instead.
    cut?: CutMark | null
    clientX?: number; clientY?: number
    grabX: number            // pointer content x at the grab: every delta is from here
  }>(null)
  // Something is being dragged over the lanes (a clip, a bin item, a file):
  // the "new track" row is drawn only then (QA-014).
  const [ghostOn, setGhostOn] = useState(false)
  // wrap.scrollTop, mirrored so the pinned ruler redraws at the visible top.
  const [scrollY, setScrollY] = useState(0)
  // Hover state for the cursor and the edge-handle highlight (QA-052).
  const hoverRef = useRef<Hit | null>(null)
  // The empty cut (its `at`) whose bowtie shows because the pointer is near
  // it (lib/cutHover, QA-051), or null.
  const cutHoverRef = useRef<number | null>(null)
  // A box selection in progress (lib/marquee, QA-116): the press point, the
  // live corner, whether it has become a box yet, and whether it adds.
  const marqueeRef = useRef<null | { ax: number; ay: number; bx: number; by: number; active: boolean; additive: boolean }>(null)
  // Queued imports, drawn as ghost clips (QA-044).
  const uploads = useStore((s) => s.uploads)
  // The time to keep under a viewport x across the next zoom change (QA-055).
  const zoomAnchorRef = useRef<{ t: number; viewX: number } | null>(null)
  const fps = edl?.canvas?.fps
  const edlDuration = edl?.duration

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

  // The rows to draw (lib/timelineLanes, QA-014): lanes that hold a clip plus
  // Main video, overlays above it and audio below, and the "new track" row
  // while something is dragged. Memoized so unrelated store updates (playhead,
  // drag state, etc.) don't change the identity of the array and trigger a
  // full canvas redraw.
  const tracks = useMemo(() => laneRows(edl?.tracks ?? [], ghostOn), [edl, ghostOn])
  // The real lane a row stands for: itself, or — for the new-track row — the
  // first empty lane that can take `kind` (null when there is none).
  const rowLane = (t: Track | undefined, kind: GhostKind, originId?: string): Track | null => {
    if (!t) return null
    if (!isGhostLane(t)) return t
    return ghostTarget(edl?.tracks ?? [], kind, originId)
  }
  const kindOfClip = (c: AnyClip | undefined): GhostKind => {
    if (!c) return 'video'
    if (isMediaClip(c)) return isAudioPath(c.src) ? 'audio' : 'video'
    return isTextClip(c) ? 'text' : 'sticker'
  }

  // contentW is the scroll EXTENT (the spacer element's width), not a canvas
  // size: the canvases are viewport-sized and draw the visible slice (see
  // lib/timelineViewport — a content-sized canvas blanked the whole timeline
  // past the browser's canvas limit, QA-024). `Math.max(size.w, …)` keeps a
  // short timeline filling the visible pane instead of leaving a gap.
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
    // QA-095: wait for the media library (it says which files are missing),
    // and never ask for the waveform of an offline file — it can only 404.
    if (!mediaLibrary.length) return
    const wantsWave = (type: string) => ['video', 'audio', 'music', 'vo'].includes(type)
    const seen = new Set<string>()
    for (const t of edl.tracks) {
      if (!wantsWave(t.type)) continue
      for (const c of t.clips) {
        if (!isMediaClip(c)) continue
        if (seen.has(c.src) || offline.has(c.src)) continue
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
  }, [sid, edl, mediaLibrary, offline])

  function trackY(i: number): number {
    return headerHeight + i * (trackHeight + 4)
  }

  // How far each v1 clip is pulled left of its `start` by the transitions
  // before it, plus where each seam lands — both in OUTPUT time. Mirrors
  // EDL.transition_overlap(); see lib/timelineLayout.ts. `v1Seams` is also
  // the seam table EVERY OTHER lane is drawn against: the renderer plays
  // captions, text, stickers, PiP, music and VO at `render_time(t) = t −
  // Σ{d_i : seam ≤ t}`, so an overlay after a transition is pulled left by
  // the same overlap its picture is. Before this, overlay lanes were drawn
  // at raw layout time — consistent with a renderer that was itself wrong,
  // and up to 2.4 s off the picture after a dozen transitions.
  const { shift: v1Shift, seams: v1Seams, clips: v1LayoutClips, layout: v1LayoutAll } = useMemo(() => {
    const v1 = (edl?.tracks ?? []).find((t) => t.id === 'v1')
    const emptyLayout: V1Layout = { shift: new Map<string, number>(), seams: [] }
    const empty = {
      shift: emptyLayout.shift, seams: emptyLayout.seams, clips: [] as LayoutClip[],
      layout: emptyLayout,
    }
    if (!v1) return empty
    const trs = (v1 as unknown as { transitions?: TransitionInfo[] }).transitions ?? []
    const lc: LayoutClip[] = v1.clips.filter(isMediaClip).map((c) => ({
      id: c.id, start: c.start, duration: clipDuration(c),
    }))
    const layout = v1Layout(lc, trs.map((tr) => ({ at: tr.at, duration: tr.duration })),
      edl?.canvas?.fps)
    return { ...layout, clips: lc, layout }
  }, [edl])

  const v1Cuts = useMemo(() => {
    // The cut list itself is shared with the Transitions panel
    // (lib/cutPoints.ts) so the seam the panel targets is the seam this
    // canvas draws; only `outAt` — where the affordance is DRAWN, which
    // differs from `at` by the accumulated overlap upstream — is local.
    return v1CutPoints(edl).map((cut) => {
      const seam = v1Seams.find((s) => Math.abs(s.boundary - cut.at) < 1e-6)
      return { at: cut.at, outAt: seam ? seam.outAt : cut.at, tr: cut.tr as TransitionInfo | null }
    })
  }, [edl, v1Seams])

  // Filmstrip tile loader. Returns the image when cached; otherwise asks
  // THUMB_QUEUE for it (visible tiles only, two in flight — QA-059) and
  // returns null so the caller keeps the flat fill for that tile. A load bumps
  // thumbTick, which is in the main draw effect's deps — the exact repaint
  // mechanism the waveform cache already uses (waveTick), not a second loop.
  // `tSec` must arrive already on lib/filmstrip's grid.
  useEffect(() => {
    thumbRepaint = () => setThumbTick((n) => n + 1)
    return () => { thumbRepaint = null }
  }, [])
  // Hold filmstrip requests until the preview <video> has data (QA-059): on
  // a cold open they raced the player for the engine and the connections.
  useEffect(() => {
    if (previewGateOpened || typeof document === 'undefined') return
    THUMB_QUEUE.hold(true)
    const gate = createPreviewGate({
      doc: document,
      onOpen: () => { previewGateOpened = true; THUMB_QUEUE.hold(false) },
    })
    if (gate.isOpen()) { previewGateOpened = true; THUMB_QUEUE.hold(false) }
    // Unmount keeps the hold: releasing here pumped the queued tiles at once
    // (a StrictMode/remount cycle opened the gate before the preview loaded);
    // the next mount's gate releases it.
    return () => gate.dispose()
  }, [])
  /** The image and source rectangle for one filmstrip tile, or null while it
   *  loads (the caller keeps the flat fill). */
  function thumbImage(src: string, tile: TileSpec, srcIn: number, srcOut: number):
      { img: HTMLImageElement; sx: number; sw: number } | null {
    if (!sid) return null
    const slot = spriteSlot(tile, srcIn, srcOut)
    const key = slot ? `S|${src}|${slot.step}|${slot.page}|${THUMB_H}` : `T|${src}|${tile.ts}|${THUMB_H}`
    const img = THUMB_CACHE.get(key)
    if (img) {
      if (!slot) return { img, sx: 0, sw: img.width }
      const sw = img.width / SPRITE_TILES
      return { img, sx: slot.index * sw, sw }
    }
    if (!THUMB_META.has(key)) THUMB_META.set(key, { src, tiles: slot ? SPRITE_TILES : 1 })
    THUMB_QUEUE.want(key, slot
      ? spriteUrl(sid, src, slot, THUMB_H)
      : `/api/sessions/${sid}/thumb?src=${encodeURIComponent(src)}&t=${tile.ts}&h=${THUMB_H}`)
    return null
  }

  // Transition affordances that are drawn — and so hit-testable (QA-051). A
  // cut with a transition always is; an empty cut only when both neighbours
  // are wide enough that the bowtie does not bury them (at fit zoom a 70-clip
  // timeline was half circles).
  const MIN_CUT_NEIGHBOUR_PX = 28
  const v1Row = tracks.findIndex((tk) => tk.id === 'v1')
  const cutMarks: (CutMark & { tr: TransitionInfo | null })[] = []
  if (v1Row >= 0) {
    const v1Clips = (tracks[v1Row].clips.filter(isMediaClip))
    for (const cut of v1Cuts) {
      const prev = v1Clips.find((c) => Math.abs(c.start + clipDuration(c) - cut.at) < 1e-3)
      const next = v1Clips.find((c) => Math.abs(c.start - cut.at) < 1e-3)
      const roomy = !!prev && !!next
        && clipDuration(prev) * zoom >= MIN_CUT_NEIGHBOUR_PX && clipDuration(next) * zoom >= MIN_CUT_NEIGHBOUR_PX
      if (!cut.tr && !roomy) continue
      cutMarks.push({
        at: cut.at, cx: labelWidth + cut.outAt * zoom, cy: trackY(v1Row) + trackHeight / 2,
        hasTransition: !!cut.tr, tr: cut.tr,
      })
    }
  }

  // Build hit list each render so we can do clip hit-testing on click.
  // Uses the same OUTPUT positions the draw loop does — the two must agree or
  // clicks land on a clip other than the one under the pointer.
  const hits: HitClip[] = []
  for (let i = 0; i < tracks.length; i++) {
    const t = tracks[i]
    const y = trackY(i)
    for (const c of t.clips) {
      // `drawnSpan` is the ONE function this list and the draw loop share:
      // v1's per-clip pull, every other lane's render window. Width is the
      // EFFECTIVE timeline width — (out-in)/speed for media. Raw `out-in`
      // made a 2x clip's hit box extend past its drawn rect (shadowing the
      // neighbor: clicks selected the wrong clip, trim handles hit-tested at
      // invisible positions) and a 0.5x clip's right half fell through the
      // hit-test entirely (clicks seeked instead).
      const span = drawnSpan(t.id, c, v1LayoutAll)
      hits.push({
        trackId: t.id, clip: c,
        x: labelWidth + span.start * zoom,
        y,
        w: Math.max(2, span.duration * zoom),
        h: trackHeight,
      })
    }
  }

  // The latest hit list for window-level handlers bound once per zoom/lanes.
  const hitsRef = useRef<HitClip[]>(hits)
  hitsRef.current = hits

  // Draw
  useEffect(() => {
    const cv = canvasRef.current
    if (!cv) return
    // VIEWPORT-sized (QA-024): the backing store covers only the visible pane,
    // and everything below draws in CONTENT coordinates under a −scrollX
    // translate, so the canvas can never outgrow the browser's size limit
    // however long the timeline or deep the zoom.
    const vs = viewportCanvasSize(size.w, contentW, contentH, dpr)
    if (cv.width !== vs.pxW) cv.width = vs.pxW
    if (cv.height !== vs.pxH) cv.height = vs.pxH
    cv.style.width = `${vs.cssW}px`
    cv.style.height = `${vs.cssH}px`
    const viewL = scrollX
    const viewR = scrollX + vs.cssW
    const ctx = cv.getContext('2d')!
    ctx.setTransform(...contentTransform(dpr, scrollX))
    ctx.clearRect(viewL, 0, vs.cssW, contentH)
    THUMB_QUEUE.frame()   // this pass says which thumbnails are still wanted

    // bg
    ctx.fillStyle = '#16161a'
    ctx.fillRect(viewL, 0, vs.cssW, contentH)

    // (The ruler is painted LAST, at the visible top — see "pinned ruler".)
    const dur = edl?.duration ?? 0

    // tracks
    for (let i = 0; i < tracks.length; i++) {
      const t = tracks[i]
      const y = trackY(i)
      // (Lane names, lock and mute live on the sticky label canvas, which
      // always covers this column.)
      if (isGhostLane(t)) {
        // The "new track" row while dragging (QA-014): a dashed drop band.
        ctx.save()
        ctx.strokeStyle = cssToken('--line', '#2c2c34')
        ctx.setLineDash([5, 4])
        ctx.strokeRect(labelWidth + 0.5, y + 0.5, contentW - labelWidth - 1, trackHeight - 1)
        ctx.restore()
        continue
      }

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
        // OUTPUT position, not the EDL's. On v1 a transition makes the
        // renderer overlap this clip with its predecessor, so everything after
        // the first transition plays EARLIER than `start` says — drawing the
        // raw value put the strip up to a second right of the picture and left
        // an unreachable tail past the (correctly shortened) duration. Every
        // OTHER lane is pulled by the same rule (`render_time`), and a span
        // that crosses a seam shrinks by that seam's overlap; one the renderer
        // will drop outright (`span.dropped`) is drawn as a flagged sliver so
        // it can still be selected and dragged out, never as a negative width.
        const span = drawnSpan(t.id, c, v1LayoutAll)
        const start = span.start
        const dur = span.duration
        // The overlap WARNING is about the EDL invariant "two clips must not
        // occupy the same time on a track", so it is tested against the EDL
        // positions — never the drawn ones. Under a transition the drawn rects
        // overlap ON PURPOSE (that is what a cross-dissolve looks like), and
        // testing the drawn values flagged every transition as broken data.
        const rawStartT = c.start
        const rawEndT = c.start + clipDuration(c)
        // Less than HALF A FRAME is not an overlap: every clip renders whole
        // frames (`clip_frames` rounds its footprint) and the v1 ripple puts
        // each start on the nearest frame, so a retimed clip (1.5x, a speed
        // curve, wave D S2) routinely "ends" a fraction of a frame past its
        // neighbour's start — it was drawn dashed as corrupt data.
        const overlapTol = 0.5 / (Number(edl?.canvas?.fps) || 30)
        const overlapsPrior = seenRanges.some(
          ([s, e]) => rawStartT < e - overlapTol && rawEndT > s + overlapTol)
        seenRanges.push([rawStartT, rawEndT])
        const x = labelWidth + start * zoom
        const w = Math.max(2, dur * zoom)
        // Off-screen: nothing of it is visible (the overlap bookkeeping above
        // still ran, so a later on-screen clip is still judged against it).
        if (!spanVisible(x, w, viewL, viewR)) continue
        const isSel = c.id === selection || multiSelection.includes(c.id)
        const color = TRACK_COLORS[t.type] ?? '#5b8dff'
        ctx.fillStyle = color
        ctx.globalAlpha = (isSel ? 1.0 : 0.85) * (t.muted ? 0.35 : 1)
        roundRect(ctx, x, y + 4, w, trackHeight - 8, 4)
        ctx.fill()
        ctx.globalAlpha = 1

        // Filmstrip thumbnails on video-track media clips (lib/filmstrip,
        // QA-059): tiles the frame's own shape (row height × source aspect),
        // only the visible ones, each showing the SOURCE time under its own
        // centre — NOT timeline time. Unloaded/errored tiles fall through to
        // the flat fill above; everything is clipped to the clip's rect.
        // Not for an offline file (QA-095): its thumbnails can only fail.
        if (t.type === 'video' && isMediaClip(c) && w > 24 && sid && mediaLibrary.length
            && !offline.has(c.src)) {
          const clipH = trackHeight - 8
          ctx.save()
          roundRect(ctx, x, y + 4, w, clipH, 4)
          ctx.clip()
          if (t.muted) ctx.globalAlpha = 0.35
          const tiles = filmstripTiles({
            x, w, clipH, aspect: THUMB_ASPECT.get(c.src) ?? 16 / 9,
            srcIn: c.in, srcOut: c.out, viewL, viewR,
          })
          for (const tile of tiles) {
            const th = thumbImage(c.src, tile, c.in, c.out)
            if (!th) continue
            ctx.drawImage(th.img, th.sx, 0, th.sw, th.img.height, tile.x, y + 4, tile.w, clipH)
          }
          ctx.globalAlpha = 1
          ctx.restore()
        }

        // Waveform inside the clip rect
        if (showWaveOn && isMediaClip(c) && w > 12) {
          const wave = WAVE_CACHE.get(c.src)
          if (wave && wave.peaks.length) {
            ctx.save()
            // Clip drawing to the clip rect
            ctx.beginPath()
            roundRect(ctx, x, y + 4, w, trackHeight - 8, 4)
            ctx.clip()
            const baseY = y + trackHeight / 2
            const halfH = (trackHeight - 12) / 2
            // Map [c.in .. c.out] → x..x+w. Each pixel column is the MAX of
            // every source peak it covers, scaled by the gain the renderer
            // applies there (gain_db/keyframes × fade ramps); a muted clip or
            // lane is greyed, not flattened (QA-081, lib/waveformDraw).
            const cols = Math.max(1, Math.floor(w))
            const startSampleSec = isMediaClip(c) ? c.in : 0
            const sampleDur = isMediaClip(c) ? (c.out - c.in) : dur
            const audio = (c as unknown as { audio?: ClipAudio }).audio
            const waveMuted = !!audio?.mute || !isHeard(t, edl?.tracks ?? tracks)
            const waveFill = 'rgba(0,0,0,0.55)'
            const clipFill = cssToken('--accent', '#ff4d6d')
            const effDur = clipDuration(c)
            const chMode = channelMode(audio)
            if (waveMuted) ctx.globalAlpha = 0.35
            // Only the columns on screen: a 12-min clip at 600 px/s is 432k
            // columns, redrawn on every scroll frame otherwise.
            const [px0, px1] = visibleColumns(x, cols, viewL, viewR)
            for (let px = px0; px < px1; px++) {
              const t0 = startSampleSec + (px / cols) * sampleDur
              const t1 = startSampleSec + ((px + 1) / cols) * sampleDur
              // Top half = what the render puts on the LEFT, bottom = the
              // RIGHT, under the clip's channel mode (QA-122): a one-sided
              // recording shows as half a waveform until its mode fills it.
              const side = channelColumns(wave, t0, t1, chMode)
              const g = waveMuted ? 1 : clipGainAt(audio, (px / cols) * effDur, effDur)
              const top = waveColumn(side.top, g, halfH)
              const bot = waveColumn(side.bottom, g, halfH)
              ctx.fillStyle = top.clipped ? clipFill : waveFill
              ctx.fillRect(x + px, baseY - top.h, 1, top.h)
              ctx.fillStyle = bot.clipped ? clipFill : waveFill
              ctx.fillRect(x + px, baseY, 1, bot.h)
            }
            ctx.globalAlpha = 1
            ctx.restore()
          }
        }

        // label inside (drawn on top of the waveform so it stays readable).
        // Over a filmstrip the dark-on-fill text is unreadable on bright
        // frames, so when thumbnails were drawn we first lay a subtle
        // left-edge scrim (clipped to the rounded clip rect) and switch to
        // light text.
        ctx.font = uiFont(10)
        // An emptied text clip is labelled rather than left blank. Clearing the
        // box is now a real edit (see Properties.commitText), so a clip with no
        // text is a normal state to be in — and an unlabelled bar reads as a
        // broken clip, which is what the old "never commit blank" guard was
        // trying to avoid by refusing the edit entirely.
        // A sticker is labelled too (its emoji or file name) — it had none.
        const isOffline = isMediaClip(c) && offline.has(c.src)
        if (isOffline) {
          // QA-095: hatched in the warning colour, labelled "Offline", so a
          // missing file is visible on the timeline itself, not only in the bin.
          ctx.save()
          roundRect(ctx, x, y + 4, w, trackHeight - 8, 4)
          ctx.clip()
          ctx.fillStyle = cssToken('--bg-1', '#16161a')
          ctx.fillRect(x, y + 4, w, trackHeight - 8)
          ctx.strokeStyle = cssToken('--warn', '#fbbf24')
          ctx.globalAlpha = 0.45
          ctx.lineWidth = 2
          for (let hx = x - trackHeight; hx < x + w; hx += 10) {
            ctx.beginPath()
            ctx.moveTo(hx, y + trackHeight - 4)
            ctx.lineTo(hx + trackHeight, y + 4)
            ctx.stroke()
          }
          ctx.globalAlpha = 1
          ctx.restore()
        }
        const label = isOffline ? `Offline · ${clipLabel(c, mediaNames)}` : clipLabel(c, mediaNames)
        // Measured and ellipsized, not sliced at a guessed 6 px a glyph, and
        // pinned to the visible start of a clip scrolled off the left (QA-118).
        let lx = Math.min(stickyLabelX(x, viewL + labelWidth), x + w)
        // Clear of a transition bowtie sitting on this clip's head.
        if (t.id === 'v1' && cutMarks.some((cm) => cm.hasTransition && Math.abs(cm.cx - x) < 9)) {
          lx = Math.min(Math.max(lx, x + 13), x + w)
        }
        const txt = fitLabel(label, x + w - lx - 8, (s) => ctx.measureText(s).width)
        if (txt) {
          // A plate behind the name, always (QA-118 remainder): over a
          // filmstrip, a dark waveform or the bare clip colour alike.
          const plate = labelPlate(lx, ctx.measureText(txt).width, y, trackHeight)
          ctx.save()
          roundRect(ctx, x, y + 4, w, trackHeight - 8, 4)
          ctx.clip()
          ctx.globalAlpha = LABEL_PLATE_ALPHA
          ctx.fillStyle = cssToken('--bg-0', '#0e0e10')
          roundRect(ctx, plate.x, plate.y, plate.w, plate.h, 3)
          ctx.fill()
          ctx.restore()
        }
        ctx.fillStyle = isOffline ? cssToken('--warn', '#fbbf24') : cssToken('--text', '#e6e6eb')
        if (txt) ctx.fillText(txt, lx, y + trackHeight / 2 + 3)
        // selection ring — 2px white so it stays unmistakable over bright
        // filmstrip frames and on tiny (w≈2px min) clips
        if (isSel) {
          ctx.strokeStyle = '#fff'
          ctx.lineWidth = 2
          ctx.strokeRect(x + 1, y + 4 + 1, w - 2, trackHeight - 8 - 2)
          // CapCut-style end handles on the primary selection: white caps
          // marking the 6px edge-drag trim zones that already exist
          // invisibly in onMouseDown.
          if (c.id === selection && w > 20) {
            ctx.fillStyle = '#fff'
            ctx.fillRect(x + 1, y + 4 + 1, 4, trackHeight - 8 - 2)
            ctx.fillRect(x + w - 5, y + 4 + 1, 4, trackHeight - 8 - 2)
            ctx.fillStyle = '#0e0e10'
            ctx.fillRect(x + 2.5, y + trackHeight / 2 - 3, 1, 6)
            ctx.fillRect(x + w - 3.5, y + trackHeight / 2 - 3, 1, 6)
          }
        }
        // Keyframe markers: a small diamond on the clip at each keyframe time.
        // Adding one used to change nothing you could see anywhere ("I can't
        // see any keyframe added in the video") — the panel button lit up and
        // that was the entire feedback. Drawn for every clip, not just the
        // selected one, so an animated clip is identifiable at a glance. Times
        // are CLIP-LOCAL (see EDL schema), hence `c.start + kt`.
        // fps passed so the collapse window here matches the panel's readout and
        // the backend's match tolerance — all three are "half a frame", and the
        // panel and canvas disagreeing on the COUNT is the bug this shares a
        // constant to avoid.
        const kfT = keyframeTimes(c, edl?.canvas.fps)
        if (kfT.length && w > 6) {
          const my = y + trackHeight - 7
          ctx.save()
          for (const kt of kfT) {
            const kx = labelWidth + (start + kt) * zoom
            if (kx < x - 1 || kx > x + w + 1) continue   // trimmed out of view
            ctx.beginPath()
            ctx.moveTo(kx, my - 4); ctx.lineTo(kx + 4, my)
            ctx.lineTo(kx, my + 4); ctx.lineTo(kx - 4, my)
            ctx.closePath()
            ctx.fillStyle = '#fbbf24'
            ctx.fill()
            ctx.strokeStyle = 'rgba(0,0,0,0.75)'
            ctx.lineWidth = 1
            ctx.stroke()
          }
          ctx.restore()
        }
        // Overlap warning: the later clip (in start order) of an overlapping
        // pair on this track gets a dashed amber border so it's never
        // invisibly merged with its neighbor, even for legacy/pre-guard data.
        // The same outline flags an overlay whose whole window sits inside a
        // crossfade's consumed span — the renderer drops it, so an unflagged
        // sliver would read as "on the timeline but never on screen".
        if (overlapsPrior || span.dropped) {
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

      // Transition affordances at v1 cut points: a small circle with a bowtie
      // glyph on the boundary between temporally-adjacent clips. Here only the
      // cuts that HAVE a transition (accent-filled); an empty cut's hollow
      // bowtie is drawn on the overlay while the pointer is near it
      // (lib/cutHover, QA-051) — drawn on every cut they buried the clips and
      // their trim zones. Hit-testing still uses all of `cutMarks`: the hover
      // radius is twice the hit radius, so a clickable bowtie is always shown.
      if (t.id === 'v1') {
        for (const cut of drawnCuts(cutMarks, null)) drawBowtie(ctx, cut.cx, cut.cy, true)
      }
    }

    // Empty-timeline hint: with zero clips the rows are just anonymous gray
    // bands — say what to do (the preview pane has the same posture).
    // Centered in the VIEWPORT width (size.w), not contentW — the wrap
    // starts unscrolled and contentW can exceed the visible pane.
    if (tracks.every((t) => t.clips.length === 0)) {
      ctx.save()
      // The --text-dim token, not a private grey: #5a5a64 was 2.46-2.65:1 (QA-103).
      ctx.fillStyle = cssToken('--text-dim', '#9b9ba5')
      ctx.font = uiFont(12)
      ctx.textAlign = 'center'
      ctx.fillText(
        'Timeline is empty — drop media here, or add files in the Media panel',
        viewL + labelWidth + (vs.cssW - labelWidth) / 2,
        headerHeight + (contentH - headerHeight) / 2 + 4,
      )
      ctx.restore()
    }

    // Markers (drawn on the heavy canvas so they live behind the playhead;
    // they don't change every frame).
    // `m.time` is LAYOUT time (commands.ts stores `layoutPlayhead`), so a
    // marker is drawn where that instant PLAYS, like every other lane.
    const markers = (edl?.markers ?? []) as { id: string; time: number; label: string; color?: string }[]
    for (const m of markers) {
      const mx = labelWidth + renderTime(v1Seams, m.time) * zoom
      if (mx < labelWidth || mx > contentW) continue
      // DASHED, and always labelled. A solid full-height line is exactly what
      // the playhead looks like, so a marker created by an accidental `M`
      // keypress read as "a second red playhead appeared on its own, and I
      // can't get rid of it" — with no UI anywhere that admitted markers exist.
      // Right-click the diamond to remove it (see onContextMenu).
      ctx.save()
      ctx.strokeStyle = m.color ?? '#fbbf24'
      ctx.setLineDash([4, 4])
      ctx.lineWidth = 1.5
      ctx.beginPath()
      ctx.moveTo(mx, headerHeight)
      ctx.lineTo(mx, contentH)
      ctx.stroke()
      ctx.restore()
    }

    // In/out range shading on the lanes
    const inX = labelWidth + (inMark ?? 0) * zoom
    const outX = labelWidth + (outMark ?? (edl?.duration ?? 0)) * zoom
    if (inMark != null || outMark != null) {
      ctx.fillStyle = 'rgba(91,141,255,0.15)'
      ctx.fillRect(inX, headerHeight, outX - inX, contentH - headerHeight)
      ctx.fillStyle = '#5b8dff'
      if (inMark != null) ctx.fillRect(inX, 0, 1.5, contentH)
      if (outMark != null) ctx.fillRect(outX, 0, 1.5, contentH)
    }

    // Pinned ruler (QA-014): painted last, at the VISIBLE top (content y =
    // scrollY), so rows scroll under it instead of taking it with them.
    // SMPTE timecode labels on the frame grid, with a tick on every frame once
    // frames are wide enough to see (QA-048).
    const rTop = scrollY
    ctx.fillStyle = cssToken('--bg-2', '#1d1d22')
    ctx.fillRect(Math.max(labelWidth, viewL), rTop, vs.cssW, headerHeight)
    // Marker labels are chips on the ruler row, and a tick label a chip would
    // cover is not drawn — they printed into each other ("5.0smarker", QA-118).
    ctx.font = uiFont(9)
    const chips = markerChips(markers
      .map((m) => ({ x: labelWidth + renderTime(v1Seams, m.time) * zoom, label: m.label ?? '', color: m.color }))
      .filter((m) => m.x >= labelWidth && m.x <= contentW), (s) => ctx.measureText(s).width)
    ctx.font = uiFont(10)
    ctx.fillStyle = cssToken('--text-dim', '#9b9ba5')
    for (const tk of rulerTicks(viewL, viewR, labelWidth, zoom, fps, dur + 30)) {
      const x = labelWidth + tk.t * zoom
      if (tk.major) {
        ctx.fillRect(x, rTop + headerHeight - 6, 1, 6)
        const tl = formatTimecode(tk.t, fps)
        if (!collides(x + 3, ctx.measureText(tl).width, chips)) ctx.fillText(tl, x + 3, rTop + headerHeight - 9)
      } else {
        ctx.fillRect(x, rTop + headerHeight - 3, 1, 3)
      }
    }
    if (inMark != null || outMark != null) {
      ctx.fillStyle = 'rgba(91,141,255,0.35)'
      ctx.fillRect(inX, rTop + headerHeight - 3, outX - inX, 3)
    }
    for (const m of markers) {
      const mx = labelWidth + renderTime(v1Seams, m.time) * zoom
      if (mx < labelWidth || mx > contentW) continue
      ctx.save()
      ctx.fillStyle = m.color ?? '#fbbf24'
      // diamond at the ruler line — the right-click target
      ctx.beginPath()
      ctx.moveTo(mx, rTop + headerHeight - 6)
      ctx.lineTo(mx + 4, rTop + headerHeight)
      ctx.lineTo(mx, rTop + headerHeight + 6)
      ctx.lineTo(mx - 4, rTop + headerHeight)
      ctx.closePath()
      ctx.fill()
      ctx.restore()
    }
    ctx.font = uiFont(9)
    for (const chip of chips) {
      ctx.fillStyle = cssToken('--bg-0', '#0e0e10')
      roundRect(ctx, chip.x, rTop + 3, chip.w, 13, 3)
      ctx.fill()
      ctx.fillStyle = chip.color ?? cssToken('--warn', '#fbbf24')
      ctx.fillText(chip.text, chip.x + 4, rTop + 13)
    }
    THUMB_QUEUE.pump()

    // (playhead drawn on a separate cheap overlay canvas — see playheadCanvasRef)
    // (`tracks` is derived from `edl` via useMemo; `edl` is already in deps.
    //  `thumbTick` repaints as filmstrip tiles load — waveTick's mechanism;
    //  `sid` feeds thumbImage's URLs; `v1Cuts` derives from `edl` via useMemo.)
  }, [edl, tracks, selection, multiSelection, zoom, size, contentW, contentH, dpr, waveTick, thumbTick, sid, v1Cuts, v1LayoutAll, inMark, outMark, flashClipId, scrollX, scrollY, fps, mediaNames, offline, mediaLibrary])

  // Sticky track-label column. The main canvas draws labels at its own x=0,
  // but that canvas is the thing that SCROLLS (contentW-sized) — so once the
  // user scrolls right to see later footage, the labels scroll away with it
  // and there's no way to tell which row is which. This small canvas is
  // exactly labelWidth wide, `position: absolute` and re-translated to track
  // wrapRef.scrollLeft on every scroll (see the effect below `onWheel`), so
  // it stays pinned to the visible left edge while the main canvas scrolls
  // underneath/behind it.
  const labelCanvasRef = useRef<HTMLCanvasElement>(null)
  const monitorLayerRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const cv = labelCanvasRef.current
    if (!cv) return
    cv.width = Math.max(1, Math.round(labelWidth * dpr))
    cv.height = Math.max(1, Math.round(contentH * dpr))
    cv.style.width = `${labelWidth}px`
    cv.style.height = `${contentH}px`
    const ctx = cv.getContext('2d')!
    // Device-pixel clear (see StickerLayer's note): a CSS-space clear under the
    // dpr transform misses the last device column/row at a fractional dpr. This
    // canvas is TRANSPARENT between its row fills, so stale ink there survives;
    // the main timeline canvas is exempt only because it repaints an opaque
    // background over its whole area immediately after clearing.
    ctx.setTransform(1, 0, 0, 1, 0, 0)
    ctx.clearRect(0, 0, cv.width, cv.height)
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    for (let i = 0; i < tracks.length; i++) {
      const t = tracks[i]
      const y = trackY(i)
      ctx.fillStyle = cssToken('--bg-2', '#1d1d22')
      ctx.fillRect(0, y, labelWidth, trackHeight)
      if (isGhostLane(t)) {
        ctx.fillStyle = cssToken('--text-faint', '#8e8e98')
        ctx.font = uiFont(11)
        ctx.fillText('+ New track', 8, y + trackHeight / 2 + 4)
        continue
      }
      const locked = isTrackLocked(t)
      const sound = laneHasSound(t)
      ctx.fillStyle = t.muted || locked ? cssToken('--text-faint', '#8e8e98') : cssToken('--text-dim', '#9b9ba5')
      ctx.font = uiFont(11)
      // A sound lane's name sits on the line above its Mute / Solo buttons
      // (DOM <button>s over this canvas — .lane-monitors); a silent lane
      // (text, stickers, captions — QA-014) has none, so its name is centred.
      ctx.fillText(laneName(t), 8, sound ? laneNameBaseline(y) : y + trackHeight / 2 + 4,
                   labelWidth - 12 - (locked ? 12 : 0))
      // Locked-state cue: a small monochrome padlock in the corner.
      if (locked) drawIcon(ctx, 'lock', labelWidth - 14, y + 4, 11, cssToken('--text-faint', '#8e8e98'))
    }
    // Ruler-row corner, at the pinned ruler's visible top (matches the main
    // canvas's ruler background so the seam between the canvases is invisible).
    ctx.fillStyle = cssToken('--bg-2', '#1d1d22')
    ctx.fillRect(0, scrollY, labelWidth, headerHeight)
  }, [tracks, contentH, dpr, scrollY])

  // Cheap playhead-only overlay redraw — avoids re-tessellating the whole
  // timeline 60×/s while the video plays. Viewport-sized and translated by the
  // same −scrollX as the main canvas (QA-024), so the two always agree about
  // where content x=0 is.
  const playheadCanvasRef = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    const cv = playheadCanvasRef.current
    if (!cv) return
    const vs = viewportCanvasSize(size.w, contentW, contentH, dpr)
    if (cv.width !== vs.pxW) cv.width = vs.pxW
    if (cv.height !== vs.pxH) cv.height = vs.pxH
    cv.style.width = `${vs.cssW}px`
    cv.style.height = `${vs.cssH}px`
    const ctx = cv.getContext('2d')!
    // Device-pixel clear — this canvas draws only the playhead over a fully
    // transparent field, so a missed column keeps a red line that no later
    // paint covers. Same rule as the label canvas above.
    ctx.setTransform(1, 0, 0, 1, 0, 0)
    ctx.clearRect(0, 0, cv.width, cv.height)
    ctx.setTransform(...contentTransform(dpr, scrollX))
    // Imports in flight, as ghost clips where they will land (QA-044).
    drawUploadGhosts(ctx)
    // The hovered empty cut's transition bowtie (QA-051: hover-only).
    const hc = cutHoverRef.current
    if (hc !== null && !dragRef.current) {
      const cut = cutMarks.find((c) => !c.hasTransition && Math.abs(c.at - hc) < 1e-6)
      if (cut) drawBowtie(ctx, cut.cx, cut.cy, false)
    }
    const ph = labelWidth + playhead * zoom
    ctx.strokeStyle = cssToken('--accent', '#ff4d6d')
    ctx.lineWidth = 1.5
    ctx.beginPath()
    ctx.moveTo(ph, 0)
    ctx.lineTo(ph, contentH)
    ctx.stroke()
    // Head and chip ride the pinned ruler (content y = scrollY).
    const top = scrollY
    ctx.fillStyle = cssToken('--accent', '#ff4d6d')
    ctx.beginPath()
    ctx.moveTo(ph - 5, top)
    ctx.lineTo(ph + 5, top)
    ctx.lineTo(ph, top + 8)
    ctx.closePath()
    ctx.fill()
    // Timecode chip at the playhead head — the transport readout's timeline
    // twin, so the current frame is readable while scrubbing here (SMPTE, so
    // frames 60 and 61 no longer both read "2.0s" — QA-048). Flips to the
    // left side of the line near the content's right edge.
    const tcode = formatTimecode(playhead, fps)
    ctx.font = uiFont(9)
    const tcw = ctx.measureText(tcode).width
    const tcx = ph + 8 + tcw + 6 > Math.min(contentW, scrollX + vs.cssW) ? ph - 8 - tcw - 4 : ph + 8
    // A ruler label under the chip is blanked WHOLE first (in the ruler's own
    // colour), then the chip is drawn opaque: the two used to print on top of
    // each other at 00:00:00:00, and a label showed through the chip.
    ctx.font = uiFont(10)
    ctx.fillStyle = cssToken('--bg-2', '#1d1d22')
    for (const b of rulerLabelsUnder(
      rulerTicks(scrollX, scrollX + vs.cssW, labelWidth, zoom, fps, (edlDuration ?? 0) + 30),
      labelWidth, zoom, fps, (t) => ctx.measureText(t).width, tcx - 3, tcx + tcw + 3)) {
      ctx.fillRect(b.x - 1, top, b.w + 2, headerHeight - 7)
    }
    ctx.font = uiFont(9)
    ctx.fillStyle = cssToken('--bg-0', '#0e0e10')
    ctx.fillRect(tcx - 3, top + 2, tcw + 6, 12)
    ctx.fillStyle = cssToken('--accent', '#ff4d6d')
    ctx.fillText(tcode, tcx, top + 11)
    // Hover affordance (QA-052): the trim handle under the pointer lights up,
    // so the 6 px edge zones are visible before you grab one.
    const hv = hoverRef.current
    if (hv && (hv.kind === 'trim-l' || hv.kind === 'trim-r') && !dragRef.current) {
      const b = hv.box
      const hx = hv.kind === 'trim-l' ? b.x : b.x + b.w - 4
      ctx.fillStyle = cssToken('--selection', '#ffffff')
      ctx.fillRect(hx, b.y + 5, 4, b.h - 10)
    }
  // `edl`/`tracks`: the cut marks and lane ends the ghosts and the hovered
  // bowtie are placed by; `uploads`: each progress step repaints its ghost.
  }, [playhead, zoom, contentW, contentH, dpr, dragTick, size, scrollX, scrollY, fps, edlDuration, edl, tracks, uploads])

  // One dashed, non-interactive rect per queued import that lands on the
  // timeline (lib/uploadGhosts), with its stage and a progress fill.
  function drawUploadGhosts(ctx: CanvasRenderingContext2D) {
    if (!uploads.length) return
    const laneEndX = (laneId: string) => hits.reduce(
      (m, h) => (h.trackId === laneId ? Math.max(m, h.x + h.w) : m), labelWidth)
    const xAt = (laneId: string, t: number) =>
      labelWidth + (laneId === 'v1' ? t : renderTime(v1Seams, t)) * zoom
    const ghosts = uploadGhosts(uploads, tracks.filter((t) => !isGhostLane(t)), laneEndX, xAt)
    if (!ghosts.length) return
    ctx.save()
    ctx.font = uiFont(10)
    for (const g of ghosts) {
      const row = tracks.findIndex((t) => t.id === g.laneId)
      if (row < 0) continue
      const gy = trackY(row) + 4
      const gh = trackHeight - 8
      const u = uploads.find((x) => x.id === g.id)
      ctx.globalAlpha = 0.7
      ctx.fillStyle = cssToken('--bg-3', '#25252c')
      roundRect(ctx, g.x, gy, g.w, gh, 4); ctx.fill()
      if (u && (u.stage === 'uploading' || u.stage === 'processing')) {
        ctx.fillStyle = cssToken('--accent-2-fill', '#3566d6')
        ctx.fillRect(g.x, gy + gh - 3, g.w * Math.min(1, Math.max(0, u.progress)), 3)
      }
      ctx.globalAlpha = 1
      ctx.setLineDash([4, 3])
      ctx.strokeStyle = cssToken('--text-dim', '#9b9ba5')
      ctx.lineWidth = 1
      roundRect(ctx, g.x + 0.5, gy + 0.5, g.w - 1, gh - 1, 4); ctx.stroke()
      ctx.setLineDash([])
      ctx.fillStyle = cssToken('--text', '#e6e6eb')
      const txt = fitLabel(g.label, g.w - 12, (s) => ctx.measureText(s).width)
      if (txt) ctx.fillText(txt, g.x + 6, gy + gh / 2 + 3)
    }
    ctx.restore()
  }

  // Live drag chrome — drawn on the SAME overlay canvas as the playhead (which
  // this effect runs after, so drag chrome layers on top), gated on an active
  // clip drag. Idle (no dragRef) → this contributes nothing; the playhead
  // effect's clearRect already ran. Keyed on dragTick so it re-runs each frame
  // only while dragging. The heavy main canvas is never touched here.
  useEffect(() => {
    const drag = dragRef.current
    const dnd = dndOverRef.current
    const cv = playheadCanvasRef.current
    if (!cv) return
    const mq = marqueeRef.current
    if (mq?.active) {
      // Box selection (QA-116): the box, and an outline on every clip it
      // touches — exactly the set the release will select.
      const c3 = cv.getContext('2d')!
      c3.save(); c3.setTransform(...contentTransform(dpr, scrollX))
      const r = marqueeRect(mq.ax, mq.ay, mq.bx, mq.by)
      const touched = new Set(marqueeHits(r, marqueeBoxes(), 4))
      c3.strokeStyle = cssToken('--accent-2', '#5b8dff')
      c3.lineWidth = 2
      for (const h of hits) if (touched.has(h.clip.id)) c3.strokeRect(h.x + 1, h.y + 5, h.w - 2, h.h - 10)
      c3.globalAlpha = 0.12
      c3.fillStyle = cssToken('--accent-2', '#5b8dff')
      c3.fillRect(r.x0, r.y0, r.x1 - r.x0, r.y1 - r.y0)
      c3.globalAlpha = 1
      c3.lineWidth = 1
      c3.strokeRect(r.x0 + 0.5, r.y0 + 0.5, r.x1 - r.x0, r.y1 - r.y0)
      c3.restore()
      return
    }
    if (dnd && (!drag || drag.kind === 'playhead')) {
      // Native panel drag: target-row wash + insertion line, plus a caption
      // saying where the drop will actually land ("PIP overlay", "Music", or
      // the v1 fallback for an incompatible lane). The drop handler's toast
      // only fires AFTER a bad drop — this says so before release, and the
      // wash goes red instead of always reading "OK" over any row.
      const ctx2 = cv.getContext('2d')!
      ctx2.save(); ctx2.setTransform(...contentTransform(dpr, scrollX))
      let ti = -1
      for (let i = 0; i < tracks.length; i++) {
        const ty = trackY(i)
        if (dnd.y >= ty && dnd.y <= ty + trackHeight) { ti = i; break }
      }
      if (ti >= 0 && dnd.x > labelWidth) {
        const row = tracks[ti]
        // The new-track row stands for the first empty lane of the family.
        const t = rowLane(row, dnd.audio ? 'audio' : 'video')
        // Emoji drops land on the stickers track regardless of hovered row
        // (add_sticker positions on the canvas, not a lane) — always "ok".
        const ok = dnd.kind === 'sticker' || (!!t && laneAcceptsMediaClip(t.type))
        const ty = trackY(ti)
        ctx2.fillStyle = ok ? dv.DROP_OK : dv.DROP_BAD
        ctx2.fillRect(labelWidth, ty, contentW - labelWidth, trackHeight)
        // The insertion line sits on the frame the drop will use (QA-049).
        const tAt = toFrameGrid(Math.max(0, (dnd.x - labelWidth) / zoom), fps)
        const lx = labelWidth + tAt * zoom
        if (ok) {
          ctx2.strokeStyle = dv.ACCENT; ctx2.lineWidth = dv.INSERTION_W
          ctx2.beginPath(); ctx2.moveTo(lx, ty); ctx2.lineTo(lx, ty + trackHeight); ctx2.stroke()
        }
        const name = t ? laneName(t) : 'a new track'
        const where = `${formatTimecode(tAt, fps)}`
        const caption = dnd.kind === 'sticker'
          ? 'Drop to add sticker'
          : !t
            ? 'No empty lane left — will import the usual way'
            : ok
              ? `${isGhostLane(row) ? 'New track: ' : 'Add to '}${name}${t.type === 'video' && t.id !== 'v1' ? ' (PIP overlay)' : ''} · ${where}`
              : `Media can't go on "${name}" — will land on Main video`
        drawCaption(ctx2, caption, lx + 8, ty + 3, ok ? dv.ACCENT : dv.ACCENT_BAD)
      }
      ctx2.restore()
      return
    }
    if (!drag || drag.kind === 'playhead') return
    const ctx = cv.getContext('2d')!
    // The playhead effect already sized + cleared + drew the playhead this
    // frame; do NOT clear (that would erase the playhead). We overlay on top.
    ctx.save()
    ctx.setTransform(...contentTransform(dpr, scrollX))

    const originIdx = tracks.findIndex((t) => t.id === drag.trackId)

    if (drag.kind === 'move') {
      // ONE plan for the preview and the release (QA-050): the ghost and the
      // landing line sit where the clip WILL land — snapped, frame-quantised,
      // free-gap-resolved — and a snap draws its guide.
      const plan = planMove(drag, drag.pointerX, drag.pointerY)
      const destIdx = plan.destIdx
      const ok = !plan.refused
      if (destIdx >= 0) {
        const ty = trackY(destIdx)
        ctx.fillStyle = ok ? dv.DROP_OK : dv.DROP_BAD
        ctx.fillRect(labelWidth, ty, contentW - labelWidth, trackHeight)
        ctx.strokeStyle = ok ? dv.ACCENT : dv.ACCENT_BAD
        ctx.lineWidth = 1
        ctx.strokeRect(labelWidth + 0.5, ty + 0.5, contentW - labelWidth - 1, trackHeight - 1)
      }
      const gw = Math.max(2, plan.durSec * zoom)
      const ghostIdx = destIdx >= 0 ? destIdx : originIdx
      if (ghostIdx >= 0) {
        const gy = trackY(ghostIdx)
        // Refused: the ghost follows the pointer, outlined red, and a caption
        // says why; the clip will not move. Otherwise it sits at the landing.
        const gx = ok ? plan.landX : plan.rawX
        if (ok && plan.gapMoved) {
          ctx.fillStyle = dv.OVERLAP_TINT
          ctx.fillRect(plan.rawX, gy + 4, gw, trackHeight - 8)
        }
        ctx.globalAlpha = dv.GHOST_ALPHA
        ctx.fillStyle = TRACK_COLORS[(plan.dest ?? tracks[ghostIdx]).type] ?? dv.ACCENT
        roundRect(ctx, gx, gy + 4, gw, trackHeight - 8, 4); ctx.fill()
        ctx.globalAlpha = 1
        ctx.strokeStyle = ok ? dv.ACCENT : dv.ACCENT_BAD
        ctx.lineWidth = dv.DRAG_BORDER_W
        roundRect(ctx, gx, gy + 4, gw, trackHeight - 8, 4); ctx.stroke()
        if (ok) {
          ctx.strokeStyle = dv.ACCENT
          ctx.lineWidth = dv.INSERTION_W
          ctx.beginPath(); ctx.moveTo(plan.landX, gy); ctx.lineTo(plan.landX, gy + trackHeight); ctx.stroke()
        }
        const caption = plan.refused
          ?? (plan.laneChange && plan.dest ? `Move to ${laneName(plan.dest)} · ${formatTimecode(plan.newStart, fps)}`
            : formatTimecode(plan.newStart, fps))
        drawCaption(ctx, caption, (ok ? plan.landX : plan.rawX) + 6, gy + 3, ok ? dv.ACCENT : dv.ACCENT_BAD)
      }
      // The snap guide: a full-height line through the target it caught.
      if (ok && plan.snapX != null && plan.snapKind) {
        ctx.save()
        ctx.strokeStyle = cssToken('--warn', '#fbbf24')
        ctx.lineWidth = 1
        ctx.setLineDash([3, 3])
        ctx.beginPath(); ctx.moveTo(plan.snapX, scrollY + headerHeight); ctx.lineTo(plan.snapX, contentH); ctx.stroke()
        ctx.restore()
        drawCaption(ctx, `Snap · ${snapLabel(plan.snapKind)}`, plan.snapX + 4, scrollY + headerHeight + 2, cssToken('--warn', '#fbbf24'))
      }
    } else if (drag.kind === 'trim-l' || drag.kind === 'trim-r') {
      // Edge-drag: live edge line + mode/result label, from the SAME snapped,
      // frame-quantised edge delta the release commits (QA-049/QA-050).
      const oi = originIdx >= 0 ? originIdx : 0
      const ty = trackY(oi)
      const dt = (drag.pointerX - drag.grabX) / zoom
      const side = drag.kind === 'trim-l' ? 'l' : 'r'
      const edgeDelta = trimEdgeDelta(drag, dt)
      let edgeSec: number
      let label: string
      // Read the clip's ACTUAL current speed — same lookup as the release path.
      const c = hits.find((h) => h.clip.id === drag.clipId)?.clip
      const sp = c ? clipSpeedFactor(c) : 1
      if (drag.clipKind === 'media' && drag.modifier) {
        const sourceDur = drag.origOut - drag.origIn
        const factor = dragResolve.resolveMediaSpeed(sourceDur, sp, side, edgeDelta)
        const footprint = sourceDur / factor
        edgeSec = side === 'l' ? drag.origStart : drag.origStart + footprint
        label = `Speed ${factor.toFixed(2)}× · ${formatTimecode(footprint, fps)}`
      } else if (drag.clipKind === 'media') {
        // Trim preview in TIMELINE space: the timeline delta converts to a
        // source delta via the clip's speed (resolveMediaTrim's speed param),
        // and the resulting source span maps back to its footprint.
        const r = dragResolve.resolveMediaTrim({ in: drag.origIn, out: drag.origOut }, side, edgeDelta, sp)
        const footprint = (r.out - r.in) / sp
        edgeSec = side === 'l' ? drag.origStart + (r.in - drag.origIn) / sp : drag.origStart + footprint
        label = `Trim · ${formatTimecode(footprint, fps)}`
      } else {
        const r = dragResolve.resolveOverlayTiming({ start: drag.origStart, end: trimOrigEnd(drag) }, side, edgeDelta)
        // Drawn and labelled in RENDER time — the ruler and the playhead chip
        // this label sits under are render time. Properties shows the EDL's
        // own (layout) values; that is its coordinate space.
        edgeSec = side === 'l' ? r.start : r.end
        label = `${formatTimecode(renderTime(v1Seams, r.start), fps)} → ${formatTimecode(renderTime(v1Seams, r.end), fps)}`
      }
      // Overlay edges are layout values here and the guide must sit on the
      // pulled lane. Media edges stay in the clip's own space: a v1 clip's pull
      // is a per-clip slot (`v1Shift`), not a coordinate.
      if (drag.clipKind !== 'media') edgeSec = renderTime(v1Seams, edgeSec)
      const ex = labelWidth + Math.max(0, edgeSec) * zoom
      ctx.strokeStyle = dv.ACCENT
      ctx.lineWidth = dv.DRAG_BORDER_W
      ctx.beginPath(); ctx.moveTo(ex, ty); ctx.lineTo(ex, ty + trackHeight); ctx.stroke()
      drawCaption(ctx, label, ex + 4, ty + 3, dv.ACCENT)
    }
    ctx.restore()
  }, [dragTick, tracks, zoom, contentW, contentH, dpr, v1Seams, scrollX, scrollY, edl, snapEnabled, playhead, fps])

  // Escape cancels an in-progress clip drag with NO commit (mousedown captured
  // state, but we simply drop it and repaint to clear the ghost). Only active
  // while a non-playhead drag is live.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key !== 'Escape') return
      if (marqueeRef.current) {        // Escape drops a box selection too
        marqueeRef.current = null
        setDragTick((n) => n + 1)
        return
      }
      const drag = dragRef.current
      if (!drag || drag.kind === 'playhead') return
      dragRef.current = null
      setGhostOn(false)
      setDragTick((n) => n + 1)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  // The clip rects a box selection tests (lib/marquee): every drawn clip.
  function marqueeBoxes() {
    return hitsRef.current.map((h) => ({ clipId: h.clip.id, x: h.x, y: h.y, w: h.w, h: h.h }))
  }

  // mouse → seek / select / drag
  // What is under a content point — ONE answer for the gesture below and the
  // hover cursor in onMouseMove (lib/timelineHit, QA-051/QA-052).
  function hitAt(x: number, y: number): Hit {
    const boxes: HitBox[] = hits.map((h) => ({
      trackId: h.trackId, clipId: h.clip.id, x: h.x, y: h.y, w: h.w, h: h.h,
      locked: isTrackLocked(tracks.find((t) => t.id === h.trackId) ?? ({} as Track)),
    }))
    return hitTest(x, y, {
      labelWidth, rulerTop: scrollY, rulerHeight: headerHeight,
      playheadX: labelWidth + playhead * zoom, clipInset: 4,
    }, boxes, cutMarks)
  }

  // A playhead time from a content x: on the frame grid (QA-049), clamped to
  // the timeline — a ruler click at x=401 used to leave the playhead at
  // 4.0125 s, between frames.
  function playheadAtX(x: number): number {
    const raw = Math.max(0, (x - labelWidth) / zoom)
    const dur = edl?.duration ?? raw
    return Math.min(toFrameGrid(raw, fps), dur)
  }

  function openTransition(cut: CutMark, clientX: number, clientY: number) {
    setContextMenu(null)
    const tr = cutMarks.find((c) => c.at === cut.at)?.tr ?? null
    setTransPopover({ x: clientX, y: clientY, at: cut.at, existing: tr })
  }

  function onMouseDown(e: React.MouseEvent) {
    const rect = contentRect()
    const x = e.clientX - rect.left
    const y = e.clientY - rect.top
    // x/y are CONTENT coords (measured against the scroll-extent element,
    // whose bounding rect moves with scrollLeft) — the same space the draw
    // loop uses. Viewport coords for the popover come from e.clientX/Y.
    const hit = hitAt(x, y)

    if (hit.kind === 'transition') {
      // stopPropagation so an ALREADY-OPEN popover's window-level mousedown
      // close listener (which fires AFTER this synthetic handler) doesn't
      // immediately null the popover state we just set when switching
      // between two cut points.
      e.stopPropagation()
      openTransition(hit.cut, e.clientX, e.clientY)
      return
    }

    // The ruler, or within a few px of the playhead line, starts a scrub
    // (live via the window listener below), on the frame grid.
    if (hit.kind === 'ruler' || hit.kind === 'playhead') {
      dragRef.current = {
        kind: 'playhead',
        clipId: '', trackId: '',
        startX: e.clientX, origStart: 0, origIn: 0, origOut: 0,
        offsetX: 0, pointerX: x, pointerY: y, modifier: false, clipKind: 'media', grabX: x,
      }
      setPlayhead(playheadAtX(x))
      return
    }

    // Note: the mute-toggle click (x < labelWidth) is handled by the sticky
    // label canvas's own onLabelMouseDown — that canvas sits ON TOP of this
    // one (z-index) at the label column regardless of scroll position.
    if (hit.kind === 'label') return

    if (hit.kind === 'move' || hit.kind === 'trim-l' || hit.kind === 'trim-r') {
      const hc = hits.find((h) => h.clip.id === hit.box.clipId)
      if (!hc) return
      // Shift-click and ⌘-click (Ctrl on Windows) add or remove a clip, as in
      // every NLE — ⌘-click used to REPLACE the selection (QA-116).
      if (isAdditive(e)) {
        toggleSelection(hc.clip.id)
        return  // a modifier-click only toggles, doesn't start a drag
      }
      setSelection(hc.clip.id)
      // QA-023: a clip on a LOCKED lane is selectable (Properties shows it)
      // but never draggable or trimmable. A real drag attempt says why.
      const hitTrack = tracks.find((t) => t.id === hc.trackId)
      if (hitTrack && isTrackLocked(hitTrack)) {
        const x0 = e.clientX
        const stop = () => {
          window.removeEventListener('mousemove', onLockedMove)
          window.removeEventListener('mouseup', stop)
        }
        const onLockedMove = (ev: MouseEvent) => {
          if (Math.abs(ev.clientX - x0) < 4) return
          toast.info(`Track "${laneName(hitTrack)}" is locked — unlock it to move or trim its clips.`)
          stop()
        }
        window.addEventListener('mousemove', onLockedMove)
        window.addEventListener('mouseup', stop)
        return
      }
      const c = hc.clip
      const clipKind: 'media' | 'text' | 'sticker' = isMediaClip(c)
        ? 'media'
        : (isTextClip(c) ? 'text' : 'sticker')
      dragRef.current = {
        kind: hit.kind,
        clipId: c.id,
        trackId: hc.trackId,
        startX: e.clientX,
        origStart: 'start' in c ? c.start : 0,
        origIn: isMediaClip(c) ? c.in : 0,
        origOut: isMediaClip(c) ? c.out : 0,
        offsetX: x - hc.x,
        pointerX: x,
        pointerY: y,
        modifier: e.altKey,
        clipKind,
        grabX: x,
        // Grabbed on the bowtie: a release without movement opens it (QA-051).
        cut: hit.kind === 'move' ? null : hit.cut,
        clientX: e.clientX, clientY: e.clientY,
      }
      if (hit.kind !== 'move' && hit.cut) e.stopPropagation()
      return
    }

    // Empty lane area: a press that travels becomes a box selection
    // (QA-116); one that does not is a click — deselect and seek there
    // (CapCut/Premiere), on the grid. Decided on release (window listener).
    marqueeRef.current = { ax: x, ay: y, bx: x, by: y, active: false, additive: isAdditive(e) }
  }

  // Shift, ⌘ (mac) or Ctrl (elsewhere): add to the selection. Mac Ctrl-click
  // is the context-menu click, so it is not a selection modifier there.
  function isAdditive(e: { shiftKey: boolean; metaKey: boolean; ctrlKey: boolean }): boolean {
    return e.shiftKey || (IS_MAC ? e.metaKey : e.ctrlKey)
  }

  // A press in the pane BELOW the last lane (the canvas is only as tall as
  // the lanes in use): the same as an empty lane — deselect and seek there.
  function onWrapMouseDown(e: React.MouseEvent) {
    const t = e.target as HTMLElement
    if (t.tagName === 'CANVAS' || e.button !== 0) return
    const x = e.clientX - contentRect().left
    if (x <= labelWidth) return
    setSelection(null)
    setPlayhead(playheadAtX(x))
  }

  // Hover: the cursor says what a press would do, and a trim zone lights its
  // handle (QA-052). The canvas used to be a crosshair everywhere.
  function onMouseMove(e: React.MouseEvent) {
    const cv = e.currentTarget as HTMLCanvasElement
    const drag = dragRef.current
    if (drag) {
      cv.style.cursor = drag.kind === 'move' ? 'grabbing'
        : drag.kind === 'playhead' ? 'col-resize' : 'ew-resize'
      return
    }
    const rect = contentRect()
    const px = e.clientX - rect.left
    const py = e.clientY - rect.top
    const hit = hitAt(px, py)
    const cursor = cursorFor(hit)
    if (cv.style.cursor !== cursor) cv.style.cursor = cursor
    const prev = hoverRef.current
    const key = (h: Hit | null) => (h && (h.kind === 'trim-l' || h.kind === 'trim-r') ? `${h.kind}:${h.box.clipId}` : '')
    hoverRef.current = hit
    // An empty cut shows its transition bowtie only while the pointer is
    // near it on the Main video row (QA-051).
    const near = v1Row >= 0 && !marqueeRef.current ? hoveredCut(px, py, cutMarks, trackY(v1Row), trackHeight) : null
    const nearAt = near ? near.at : null
    const cutChanged = nearAt !== cutHoverRef.current
    cutHoverRef.current = nearAt
    if (key(prev) !== key(hit) || cutChanged) setDragTick((n) => n + 1)
  }

  function onMouseLeave() {
    if ((hoverRef.current || cutHoverRef.current !== null) && !dragRef.current) {
      hoverRef.current = null
      cutHoverRef.current = null
      setDragTick((n) => n + 1)
    }
  }

  // Window-level drag listeners: a canvas-only onMouseMove/onMouseUp binding
  // means a drag that leaves the canvas bounds (dragging fast, or wide
  // gestures on a small viewport) never receives its mouseup and gets stuck.
  // Binding to `window` for the lifetime of any active drag fixes both that
  // AND lets playhead scrubbing live-update as the pointer moves.
  useEffect(() => {
    function onWindowMouseMove(e: MouseEvent) {
      const mq = marqueeRef.current
      if (mq && canvasRef.current) {
        const rect = contentRect()
        mq.bx = e.clientX - rect.left
        mq.by = e.clientY - rect.top
        if (!mq.active && isMarqueeDrag(mq.ax, mq.ay, mq.bx, mq.by)) mq.active = true
        if (mq.active && dragRafRef.current == null) {
          dragRafRef.current = requestAnimationFrame(() => {
            dragRafRef.current = null
            setDragTick((n) => n + 1)
          })
        }
        return
      }
      const drag = dragRef.current
      if (!drag || !canvasRef.current) return
      const rect = contentRect()
      const x = e.clientX - rect.left
      const y = e.clientY - rect.top
      if (drag.kind === 'playhead') {
        setPlayhead(playheadAtX(x))
        return
      }
      // Clip move/trim: record live pointer, request one overlay redraw/frame.
      drag.pointerX = x
      drag.pointerY = y
      // A real clip move shows the "new track" row (QA-014); it is appended
      // BELOW every lane, so no row shifts under the pointer.
      if (drag.kind === 'move' && Math.abs(e.clientX - drag.startX) + Math.abs(e.clientY - (drag.clientY ?? e.clientY)) > 3) {
        setGhostOn(true)
      }
      if (dragRafRef.current == null) {
        dragRafRef.current = requestAnimationFrame(() => {
          dragRafRef.current = null
          setDragTick((n) => n + 1)
        })
      }
    }
    function onWindowMouseUp(e: MouseEvent) {
      const mq = marqueeRef.current
      if (mq) {
        marqueeRef.current = null
        if (mq.active) {
          useStore.getState().selectClips(
            marqueeHits(marqueeRect(mq.ax, mq.ay, mq.bx, mq.by), marqueeBoxes(), 4), mq.additive)
          setDragTick((n) => n + 1)   // clear the box
        } else {
          if (!mq.additive) setSelection(null)
          setPlayhead(playheadAtX(mq.ax))
        }
        return
      }
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
  }, [zoom, edl?.duration, fps, tracks])

  // Snapping (lib/snap, QA-050): the start, the playhead, every MARKER, and
  // every clip edge, within `SNAP_PX` pixels. Every caller passes `t` in
  // LAYOUT time (a v1 drag is a layout delta; an overlay gesture is decoded
  // through `layoutTime` first), and every clip edge and marker is an EDL
  // value — so the targets are layout space. The playhead is the one RENDER-
  // time value: it is the <video>'s clock, which after three 0.5 s dissolves
  // sits 1.5 s LEFT of the layout instant it shows, so it is decoded with the
  // overlay inverse (inside a crossfade window that is the seam's layout
  // time). Off (the magnet button, or N) → no snapping at all.
  const SNAP_PX = 8
  function snapResult(t: number, ignoreClipId?: string): SnapResult {
    if (!snapEnabled) return { t, target: null }
    return snapTo(t, snapTargets(edl, layoutTime(v1Seams, playhead), ignoreClipId), SNAP_PX / zoom)
  }
  function snapTime(t: number, ignoreClipId?: string): number {
    return snapResult(t, ignoreClipId).t
  }

  // The signed LAYOUT delta for an overlay edge the pointer moved by `dt`
  // seconds of RENDER time: the edge's render position plus the drag,
  // decoded through `layoutTime` (inside a crossfade window that is the
  // seam's layout time — the documented snap rule), then snapped against the
  // EDL's own edges. Shared by the live guide and the release path so the
  // preview is the landing.
  function overlayEdgeDelta(origEdge: number, dt: number, ignoreClipId?: string): number {
    const target = layoutTime(v1Seams, renderTime(v1Seams, origEdge) + dt)
    return snapTime(Math.max(0, target), ignoreClipId) - origEdge
  }

  // A trimmed clip's current END in its own space. Media: the EFFECTIVE end
  // (start + (out-in)/speed), where the draw loop places the edge. Overlays:
  // their real `end` (origIn/origOut are 0/0 for them).
  function trimOrigEnd(drag: NonNullable<typeof dragRef.current>): number {
    const c = hits.find((h) => h.clip.id === drag.clipId)?.clip
    if (drag.clipKind !== 'media') return (c as unknown as { end?: number })?.end ?? drag.origStart
    const sp = c ? clipSpeedFactor(c) : 1
    return drag.origStart + (drag.origOut - drag.origIn) / sp
  }

  // The signed edge delta a trim of `dt` seconds commits: snapped to the
  // targets above, then onto the frame grid (QA-049) — the live guide and the
  // release both use this, so the preview is the landing.
  function trimEdgeDelta(drag: NonNullable<typeof dragRef.current>, dt: number): number {
    const side = drag.kind === 'trim-l' ? 'l' : 'r'
    const origEdge = side === 'l' ? drag.origStart : trimOrigEnd(drag)
    const moved = drag.clipKind === 'media'
      ? snapTime(origEdge + dt, drag.clipId)
      : origEdge + overlayEdgeDelta(origEdge, dt, drag.clipId)
    return toFrameGrid(moved, fps) - origEdge
  }

  // Where a clip MOVE lands — computed once for the live preview and the
  // release (QA-049/050/084): the lane (the new-track row resolved to a real
  // empty lane), whether the lane change is refused and why, the snapped,
  // frame-quantised start, the free-gap correction, and the snap it caught.
  // A lane change with less than LANE_MOVE_SLOP_PX of sideways travel is a
  // PURE lane move: same start. A refused lane change moves nothing at all.
  const LANE_MOVE_SLOP_PX = 8
  function planMove(drag: NonNullable<typeof dragRef.current>, px: number, py: number) {
    const origin = tracks.find((t) => t.id === drag.trackId)
    const originIdx = tracks.findIndex((t) => t.id === drag.trackId)
    const draggedClip = origin?.clips.find((c) => c.id === drag.clipId)
    const hc = hits.find((h) => h.clip.id === drag.clipId)
    const family: GhostKind = origin && AUDIO_FAMILY.has(origin.type) ? 'audio'
      : origin?.type === 'video' ? 'video' : kindOfClip(draggedClip)
    let destIdx = -1
    for (let i = 0; i < tracks.length; i++) {
      const ty = trackY(i)
      if (py >= ty && py <= ty + trackHeight) { destIdx = i; break }
    }
    if (destIdx < 0) destIdx = originIdx
    const row = tracks[destIdx]
    let dest: Track | undefined = origin
    let refused: string | null = null
    if (row && origin && row.id !== origin.id) {
      const lane = rowLane(row, family, origin.id)
      const originIsMediaFamily = laneAcceptsMediaClip(origin.type)
      if (!lane) {
        refused = 'No empty lane can take this clip — drop it on an existing lane.'
      } else if (VIDEO_FAMILY.has(lane.type) && draggedClip && isMediaClip(draggedClip) && isAudioPath(draggedClip.src)) {
        // An audio-only clip on a video lane would break every render; the
        // backend's move_clip refuses it too.
        refused = `“${baseName(draggedClip.src)}” is audio — it can't go on the "${laneName(lane)}" lane.`
      } else if (originIsMediaFamily ? !laneAcceptsMediaClip(lane.type) : lane.type !== origin.type) {
        refused = `Can't move this clip to the "${laneName(lane)}" lane — it stays on "${laneName(origin)}".`
      } else {
        dest = lane
      }
    }
    const laneChange = !refused && !!dest && dest.id !== drag.trackId
    let dxPx = px - drag.grabX
    if (laneChange && Math.abs(dxPx) < LANE_MOVE_SLOP_PX) dxPx = 0
    const dt = dxPx / zoom
    const durSec = draggedClip ? clipDuration(draggedClip) : drag.origOut - drag.origIn
    const isMedia = drag.clipKind === 'media'
    const raw = isMedia
      ? Math.max(0, drag.origStart + dt)
      : Math.max(0, layoutTime(v1Seams, renderTime(v1Seams, drag.origStart) + dt))
    // Snap whichever edge is nearer a target: the start, or the end.
    let snapped = raw
    let snapT: number | null = null
    let snapKind: SnapKind | null = null
    if (dxPx !== 0) {
      const a = snapResult(raw, drag.clipId)
      const b = snapResult(raw + durSec, drag.clipId)
      const da = a.target ? Math.abs(a.t - raw) : Infinity
      const db = b.target ? Math.abs(b.t - (raw + durSec)) : Infinity
      if (a.target && da <= db) { snapped = a.t; snapT = a.t; snapKind = a.target.kind }
      else if (b.target) { snapped = b.t - durSec; snapT = b.t; snapKind = b.target.kind }
    }
    let newStart = refused ? drag.origStart : toFrameGrid(Math.max(0, snapped), fps)
    const onV1 = isMedia && (origin?.id === 'v1' || dest?.id === 'v1')
    const willReorder = onV1 && dest?.id === 'v1' && dest.id === origin?.id
    let gapMoved = false
    if (!refused && dest && isMedia && !willReorder) {
      const g = firstFreeGap(dest, durSec, newStart, drag.clipId)
      if (Math.abs(g - newStart) > 1e-6) { newStart = g; gapMoved = true; snapT = null; snapKind = null }
    }
    // Drawn positions (content x). v1 draws a clip `v1Shift` left of its
    // start; every other lane draws at render time.
    const drawnOf = (t: number) => (dest?.id === 'v1'
      ? t - (v1Shift.get(drag.clipId) ?? 0)
      : renderTime(v1Seams, t))
    const landT = willReorder && dest ? dragResolve.reorderLanding(dest, newStart, drag.clipId) : newStart
    const rawX = (hc ? hc.x : labelWidth + drag.origStart * zoom) + (px - drag.grabX)
    const moved = !refused && (laneChange || Math.abs(newStart - drag.origStart) > 1e-9)
    return {
      destIdx, dest, refused, laneChange, newStart, durSec, gapMoved, willReorder,
      closeGap: onV1, moved,
      landX: labelWidth + drawnOf(landT) * zoom,
      rawX,
      snapX: snapT == null ? null : labelWidth + renderTime(v1Seams, snapT) * zoom,
      snapKind,
      // Did the pointer try to change lanes at all (even if refused)?
      triedLane: !!row && !!origin && row.id !== origin.id,
    }
  }

  async function onMouseUp(e: React.MouseEvent) {
    const drag = dragRef.current
    if (!drag) return
    dragRef.current = null
    setGhostOn(false)
    setDragTick((n) => n + 1)  // repaint overlay (clears drag chrome)
    const rect = contentRect()
    const px = e.clientX - rect.left
    const py = e.clientY - rect.top
    const dxPx = px - drag.grabX

    if (drag.kind === 'move') {
      const plan = planMove(drag, px, py)
      // A press-and-release on the same lane is a click (select), not a move.
      if (Math.abs(dxPx) < 3 && !plan.triedLane) return
      if (plan.refused) {
        toast.error(plan.refused)
        return
      }
      if (!plan.moved) return
      const args: Record<string, unknown> = { clip_id: drag.clipId, new_start: plan.newStart }
      if (plan.laneChange && plan.dest) args.new_track = plan.dest.id
      // A DRAG means "reorder", so close the slot the clip vacated on v1
      // (the backend applies the same restriction; every other lane holds
      // elements positioned against v1's picture). Sent whenever v1 is EITHER
      // end of the drag, because the hole to close is on the lane the clip LEFT.
      if (plan.closeGap) args.close_gap = true
      if (plan.gapMoved && plan.dest) {
        toast.info(`Moved to the nearest free gap on "${laneName(plan.dest)}" (${formatTimecode(plan.newStart, fps)}) to avoid overlapping a clip.`)
      }
      await dispatch('move_clip', args)
      return
    }

    if (drag.kind === 'trim-l' || drag.kind === 'trim-r') {
      if (Math.abs(dxPx) < 3) {
        // Grabbed on a cut's bowtie and released in place: that was a click
        // on the transition affordance, not a trim (QA-051).
        if (drag.cut) openTransition(drag.cut, drag.clientX ?? e.clientX, drag.clientY ?? e.clientY)
        return
      }
      const side: 'l' | 'r' = drag.kind === 'trim-l' ? 'l' : 'r'
      const isOverlay = drag.clipKind === 'text' || drag.clipKind === 'sticker'
      const trimClip = hits.find((h) => h.clip.id === drag.clipId)?.clip
      const trimSpeed = trimClip ? clipSpeedFactor(trimClip) : 1
      // The signed edge delta AFTER snapping and frame-quantising the moved
      // edge — exactly the `deltaSec` contract every resolve* function expects
      // (positive = edge moved right), and exactly what the live guide drew.
      const edgeDelta = trimEdgeDelta(drag, dxPx / zoom)
      if (isOverlay) {
        // Overlay clips have no source to trim — edge-drag retimes the window
        // (start/end) via set_clip_timing. resolveOverlayTiming clamps end>start.
        const r = dragResolve.resolveOverlayTiming(
          { start: drag.origStart, end: trimOrigEnd(drag) }, side, edgeDelta)
        if (side === 'l') await dispatch('set_clip_timing', { clip_id: drag.clipId, start: r.start })
        else await dispatch('set_clip_timing', { clip_id: drag.clipId, end: r.end })
      } else if (drag.modifier) {
        // Alt + edge-drag on media = speed retime (keep whole source, change
        // the timeline footprint).
        const sourceDur = drag.origOut - drag.origIn
        const factor = dragResolve.resolveMediaSpeed(sourceDur, trimSpeed, side, edgeDelta)
        await dispatch('set_speed', { clip_id: drag.clipId, factor })
      } else {
        // Plain media edge-drag = trim. `edgeDelta` is TIMELINE-space, so pass
        // the clip's speed and let resolveMediaTrim convert to source space.
        const r = dragResolve.resolveMediaTrim(
          { in: drag.origIn, out: drag.origOut }, side, edgeDelta, trimSpeed)
        // Off the magnetic Main video lane a head trim keeps the kept frames
        // where they play (`move_start`); without it the edge snapped back and
        // the clip's content slid earlier by the trimmed length.
        if (side === 'l') {
          await dispatch('trim_clip', { clip_id: drag.clipId, in: r.in,
                                        ...(drag.trackId !== 'v1' ? { move_start: true } : {}) })
        } else await dispatch('trim_clip', { clip_id: drag.clipId, out: r.out })
      }
    }
  }

  // Drop from the media bin → add a clip; from the sticker panel → add a
  // sticker; from Finder → import onto the lane and time under it (QA-093).
  const dndClearRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  function endPanelDrag() {
    if (dndClearRef.current) { clearTimeout(dndClearRef.current); dndClearRef.current = null }
    dndOverRef.current = null
    setGhostOn(false)
    setTimelineFileDragOver(false)
    setDragTick((n) => n + 1)
  }
  function onCanvasDragOver(e: React.DragEvent) {
    const types = e.dataTransfer.types
    const file = isFileDrag(types)
    if (file || types.includes('application/x-vai-src')
        || types.includes('application/x-vai-emoji')
        || types.includes('text/plain')) {
      e.preventDefault()
      e.dataTransfer.dropEffect = 'copy'
      if (dndClearRef.current) { clearTimeout(dndClearRef.current); dndClearRef.current = null }
      // Measure against the CANVAS rect, not e.currentTarget (the wrap): the
      // drag handlers live on the viewport-fixed wrap, but the overlay draw +
      // row hit-testing use canvas-CONTENT coords — the wrap rect is off by
      // scrollLeft/scrollTop once the timeline is scrolled (same convention
      // as onMouseDown, whose rect comes from the scrolling canvas itself).
      const rect = contentRect()
      const items = Array.from(e.dataTransfer.items ?? [])
      dndOverRef.current = {
        x: e.clientX - rect.left,
        y: e.clientY - rect.top,
        kind: file ? 'file' : types.includes('application/x-vai-emoji') ? 'sticker' : 'media',
        audio: file && items.length > 0 && items.every((it) => it.type.startsWith('audio/')),
      }
      if (file) setTimelineFileDragOver(true)
      if (!types.includes('application/x-vai-emoji')) setGhostOn(true)
      if (dragRafRef.current == null) {
        dragRafRef.current = requestAnimationFrame(() => {
          dragRafRef.current = null
          setDragTick((n) => n + 1)
        })
      }
    }
  }
  function onCanvasDragLeave() {
    // dragleave also fires moving between the wrap's own children; the next
    // dragover cancels this, so the new-track row does not flicker.
    if (dndClearRef.current) clearTimeout(dndClearRef.current)
    dndClearRef.current = setTimeout(endPanelDrag, 120)
  }

  // The lane a panel/file drop lands on: the row under `y` (the new-track
  // row resolved to a real empty lane of `kind`), or undefined for none.
  function dropRowAt(y: number): Track | undefined {
    for (let i = 0; i < tracks.length; i++) {
      const ty = trackY(i)
      if (y >= ty && y <= ty + trackHeight) return tracks[i]
    }
    return undefined
  }
  // EDL `start` for a drop at render time `tDrop` on `laneId`: v1 decodes its
  // per-clip pull, every other lane the overlay inverse; then snap, then the
  // frame grid (QA-049).
  function dropStart(laneId: string, tDrop: number): number {
    const t = laneId === 'v1'
      ? edlTimeFromOutput(snapTime(tDrop), v1LayoutClips, v1Shift)
      : snapTime(layoutTime(v1Seams, tDrop))
    return toFrameGrid(Math.max(0, t), fps)
  }

  async function onFilesDropped(e: React.DragEvent, y: number, tDrop: number): Promise<void> {
    const row = dropRowAt(y)
    if (!row || !sid) return  // not on a lane: the window importer handles it
    // This drop is ours: the window-level importer must not import it again.
    claimFileDrop(e.nativeEvent)
    const files = Array.from(e.dataTransfer.files)
    const audio = files.length > 0 && files.every((f) => isAudioPath(f.name) || f.type.startsWith('audio/'))
    const lane = rowLane(row, audio ? 'audio' : 'video')
    if (!lane) toast.info('No empty lane left for this file — importing it the usual way.')
    else if (!laneAcceptsMediaClip(lane.type)) toast.info(`Media can't go on the "${laneName(lane)}" lane — importing it the usual way.`)
    const st = useStore.getState()
    const v1 = (edl?.tracks ?? []).find((t) => t.id === 'v1')
    const start = lane ? dropStart(lane.id, tDrop) : 0
    // ONE import pipeline with the Media panel (QA-044/093): each file is a
    // queued import — placeholder row, Cancel, the panel's busy state — and
    // is placed from what the server answered (a photo is a 5 s clip, an
    // audio-only .mp4 goes to an audio lane). The queue owns `uploading`.
    await dropFilesOnLane(files, {
      lane: lane ? { id: lane.id, type: lane.type } : null,
      start,
      lanes: (edl?.tracks ?? []).map((t) => ({ id: t.id, type: t.type })),
      mainEmpty: !v1 || v1.clips.length === 0,
    }, {
      enqueue: (f, as, place) => (as === 'audio'
        ? st.uploadAudio(f, place ? { place } : undefined)
        : st.upload(f, place ? { place } : undefined)),
      notice: (m) => toast.info(m),
    }, projectFpsOf(fps))
  }

  async function onCanvasDrop(e: React.DragEvent) {
    e.preventDefault()
    endPanelDrag()  // repaint overlay now — drop fires no dragleave
    // Canvas rect, not the wrap's — content coords, see onCanvasDragOver.
    // With the wrap rect a drop on a scrolled timeline landed shifted by
    // scrollLeft/scrollTop: wrong time, and potentially the wrong ROW.
    const rect = contentRect()
    const x = e.clientX - rect.left
    const y = e.clientY - rect.top
    if (x < labelWidth) return
    const tDrop = Math.max(0, (x - labelWidth) / zoom)

    if (isFileDrag(e.dataTransfer.types) && e.dataTransfer.files.length) {
      await onFilesDropped(e, y, tDrop)
      return
    }

    // Emoji drop from the StickerPanel — drops a 3-second sticker centered.
    const emoji = e.dataTransfer.getData('application/x-vai-emoji')
    if (emoji) {
      const w = edl?.canvas.w ?? 1080
      const h = edl?.canvas.h ?? 1920
      // The pointer is in RENDER time; the stickers lane is pulled like every
      // overlay lane, so decode to layout first (a drop inside a dissolve
      // lands on the seam) and snap against the EDL's edges there.
      const stickerStart = dropStart('stickers', tDrop)
      await dispatch('add_sticker', {
        emoji,
        start: stickerStart,
        end: stickerStart + 3.0,
        position: [w / 2, h * 0.55],
      })
      return
    }

    const src = e.dataTransfer.getData('application/x-vai-src')
              || e.dataTransfer.getData('text/plain')
    if (!src) return
    // An audio-only source can never live on a video lane: the render builds a
    // video filtergraph for v1 clips, so an mp3 there kills EVERY preview and
    // export with "[i:v] … matches no streams". Route audio to the Music lane
    // instead, honouring the promise MediaBin's own tooltip makes ("audio
    // lands on the Music track"). The backend enforces this too.
    const srcIsAudio = isAudioPath(src)
    // Whichever row the drop landed on (the new-track row resolves to the
    // first empty lane of the right family), so an incompatible-lane drop
    // gets real feedback instead of a silent redirect.
    const row = dropRowAt(y)
    const dropped = rowLane(row, srcIsAudio ? 'audio' : 'video') ?? undefined
    const musicTrackId = (edl?.tracks ?? []).find((t) => t.type === 'music')?.id
    let trackId = srcIsAudio ? (musicTrackId ?? 'music') : 'v1'
    if (dropped && laneAcceptsMediaClip(dropped.type)) {
      if (srcIsAudio && VIDEO_FAMILY.has(dropped.type)) {
        toast.info(`“${baseName(src)}” is audio — added to the Music lane instead.`)
      } else {
        trackId = dropped.id
      }
    } else if (dropped) {
      toast.error(`Media can't go on the "${laneName(dropped)}" lane — added to the ${srcIsAudio ? 'Music' : 'main video'} track instead.`)
    } else if (row) {
      toast.info(`No empty lane left — added to the ${srcIsAudio ? 'Music' : 'main video'} track.`)
    }
    // The media bin is a library (QA-010) and sends the source's real length
    // with the drag; failing that, reuse the length of a clip of this source.
    const draggedDur = Number(e.dataTransfer.getData('application/x-vai-duration'))
    const hasDraggedDur = Number.isFinite(draggedDur) && draggedDur > 0
    let dur = hasDraggedDur ? draggedDur : 30
    for (const tk of hasDraggedDur ? [] : edl?.tracks ?? []) {
      for (const c of tk.clips) {
        if (isMediaClip(c) && c.src === src) {
          dur = c.out - c.in
          break
        }
      }
    }
    // `tDrop` is where the pointer is in OUTPUT time, but `add_clip` takes an
    // EDL `start` — `dropStart` decodes it per lane (see there).
    await dispatch('add_clip', {
      track: trackId, src, in: 0.0, out: dur, start: dropStart(trackId, tDrop),
    })
  }

  function onContextMenu(e: React.MouseEvent) {
    const rect = contentRect()
    const x = e.clientX - rect.left
    const y = e.clientY - rect.top
    // Right-clicking a marker's diamond removes it. This is the ONLY way to get
    // rid of a marker — `M` created them silently and nothing in the UI could
    // remove one, which is why an accidental keypress read as a permanent
    // "extra red playhead". It goes through dispatch, so Undo restores it.
    const markers = (edl?.markers ?? []) as { id: string; time: number }[]
    // The ruler is pinned at the visible top (content y = scrollY).
    if (y - scrollY <= headerHeight + 8) {
      const near = markers.find((m) => Math.abs((labelWidth + renderTime(v1Seams, m.time) * zoom) - x) <= 6)
      if (near) {
        e.preventDefault()
        void dispatch('remove_marker', { marker_id: near.id })
        toast.info(`Marker removed (${chordLabel('Mod+KeyZ')} to undo)`)
        return
      }
    }
    const hit = hits.find((h) => x >= h.x && x <= h.x + h.w && y >= h.y + 4 && y <= h.y + h.h - 4)
    if (!hit) return
    e.preventDefault()
    setSelection(hit.clip.id)
    setContextMenu({ x: e.clientX, y: e.clientY, clipId: hit.clip.id, trackId: hit.trackId })
  }

  // The keyboard route to the clip menu (QA-102): with the timeline focused,
  // Shift+F10 or the context-menu key opens it on the selected clip, where a
  // right-click would. Without it the menu was mouse-only.
  function onCanvasKeyDown(e: React.KeyboardEvent<HTMLCanvasElement>) {
    if (!(e.key === 'ContextMenu' || (e.shiftKey && e.key === 'F10'))) return
    const hit = hits.find((h) => h.clip.id === selection)
    if (!hit) return
    e.preventDefault()
    const rect = contentRect()
    setContextMenu({
      x: rect.left + hit.x + Math.min(hit.w / 2, 24), y: rect.top + hit.y + hit.h / 2,
      clipId: hit.clip.id, trackId: hit.trackId,
    })
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
  // On the WRAP, not the canvas: with only the lanes in use drawn (QA-014)
  // the canvas can be shorter than the pane, and ⌘-wheel below the last lane
  // must zoom too.
  useEffect(() => {
    const cv = wrapRef.current
    if (!cv) return
    function handleWheel(e: WheelEvent) {
      const wrap = wrapRef.current
      if (!wrap) return
      // ONE rule (lib/timelineWheel, QA-117), stated in Help: ⌘/Ctrl-wheel
      // or a pinch zooms, Shift-wheel pans, a sideways swipe pans natively,
      // a plain wheel scrolls the tracks (or pans when every track fits).
      const act = wheelAction(e, wrap.scrollHeight > wrap.clientHeight)
      if (act.kind === 'native') return        // the browser's own scroll
      e.preventDefault()
      if (act.kind === 'pan') { wrap.scrollLeft += act.dx; return }
      // Zoom anchored on the POINTER (QA-055): the time under it stays there.
      const viewX = e.clientX - wrap.getBoundingClientRect().left
      zoomAnchorRef.current = { t: Math.max(0, (wrap.scrollLeft + viewX - labelWidth) / zoom), viewX }
      setZoomStore(zoom * act.factor)
    }
    cv.addEventListener('wheel', handleWheel, { passive: false })
    return () => cv.removeEventListener('wheel', handleWheel)
  }, [zoom, setZoomStore])

  // Every zoom change keeps one instant fixed on screen (QA-055): the pointer's
  // time for ⌘-wheel, the playhead when it is in view (buttons, slider, keys),
  // else the view's centre. Runs after the spacer has its new width, before
  // paint. The playhead-follow effect below no longer re-centres on zoom.
  const prevZoomRef = useRef(zoom)
  useLayoutEffect(() => {
    const wrap = wrapRef.current
    const prevZoom = prevZoomRef.current
    prevZoomRef.current = zoom
    if (!wrap || prevZoom === zoom) { zoomAnchorRef.current = null; return }
    let anchor = zoomAnchorRef.current
    zoomAnchorRef.current = null
    if (!anchor) {
      const phX = labelWidth + playhead * prevZoom - wrap.scrollLeft
      anchor = phX >= labelWidth && phX <= wrap.clientWidth
        ? { t: playhead, viewX: phX }
        : { t: Math.max(0, (wrap.scrollLeft + (wrap.clientWidth + labelWidth) / 2 - labelWidth) / prevZoom),
            viewX: (wrap.clientWidth + labelWidth) / 2 }
    }
    wrap.scrollLeft = anchoredScroll(anchor.t, anchor.viewX, zoom, labelWidth)
    setScrollX(wrap.scrollLeft)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [zoom])

  const laneW = Math.max(0, size.w - labelWidth)
  const hasClips = (edl?.tracks ?? []).some((t) => t.clips.length > 0)
  // Freeze frame (wave D S2, lib/freezeFrame): enabled only where it would
  // land; the disabled title says why. The hold is the server's default.
  const freezePlan = planFreeze(edl, selection, playhead)
  const speedCat = useSpeedCatalog()
  const freezeHold = speedCat.status === 'ready' ? ` for ${speedCat.catalog.freeze_default} s` : ''
  // Zoom from the toolbar: anchored on the playhead / view centre (above).
  function zoomTo(z: number) { setZoomStore(z) }
  function zoomBy(factor: number) { setZoomStore(zoom * factor) }

  // Zoom to fit (⌘\ and the toolbar's Fit) measures THIS pane (QA-054).
  useEffect(() => {
    registerTimelineView({
      laneWidth: () => Math.max(0, (wrapRef.current?.clientWidth ?? size.w) - labelWidth),
      fitTo: (z) => {
        zoomAnchorRef.current = { t: 0, viewX: labelWidth }
        setZoomStore(z)
        if (wrapRef.current) wrapRef.current.scrollLeft = 0
      },
    })
    return () => registerTimelineView(null)
  }, [setZoomStore, size.w])

  // The first import into an empty project fits the timeline (QA-053): it sat
  // at 80 px/s with ~11 s of an 85 s clip visible. Opening a project, or
  // adding to one, leaves the zoom alone.
  const fitSidRef = useRef<{ sid: string | null; had: boolean }>({ sid: null, had: true })
  useEffect(() => {
    const has = hasClips
    const prev = fitSidRef.current
    if (prev.sid !== sid) { fitSidRef.current = { sid, had: has || !edl }; return }
    if (!edl) return
    if (!prev.had && has) {
      fitSidRef.current = { sid, had: true }
      requestAnimationFrame(() => void COMMAND_BY_ID.zoomFit.run(useStore.getState()))
      return
    }
    if (prev.had !== has) fitSidRef.current = { sid, had: has }
  }, [edl, sid, hasClips])

  // Keep the sticky label canvas pinned to the wrapper's visible left edge as
  // it scrolls horizontally (translateX cancels out scrollLeft). A plain CSS
  // `position: sticky` doesn't work here because the label canvas needs to
  // OVERLAY the main canvas at the same row positions, not stack after it in
  // normal flow — so this is a small manual re-implementation of "sticky"
  // using `position: absolute` + a scroll listener instead.
  // Viewport canvases redraw the visible slice on every horizontal scroll, and
  // the pinned ruler on every vertical one (QA-014). flushSync so the redraw
  // lands in the SAME frame as the native scroll — a deferred render would
  // show the sticky canvas's stale slice for a frame.
  useEffect(() => {
    const wrap = wrapRef.current
    if (!wrap) return
    const onScroll = () => {
      const x = wrap.scrollLeft
      const y = wrap.scrollTop
      flushSync(() => {
        setScrollX((prev) => (prev === x ? prev : x))
        setScrollY((prev) => (prev === y ? prev : y))
      })
    }
    setScrollX(wrap.scrollLeft)
    setScrollY(wrap.scrollTop)
    wrap.addEventListener('scroll', onScroll, { passive: true })
    return () => wrap.removeEventListener('scroll', onScroll)
  }, [])

  useEffect(() => {
    const wrap = wrapRef.current
    const label = labelCanvasRef.current
    if (!wrap || !label) return
    const onScroll = () => {
      label.style.transform = `translateX(${wrap.scrollLeft}px)`
      if (monitorLayerRef.current) monitorLayerRef.current.style.transform = `translateX(${wrap.scrollLeft}px)`
    }
    onScroll()
    wrap.addEventListener('scroll', onScroll)
    return () => wrap.removeEventListener('scroll', onScroll)
  }, [contentW])

  // A new import's ghost clip is scrolled into view once (QA-044): at fit
  // zoom the end of Main video is the pane's right edge, so the ghost that
  // says "your file is coming" sat just out of sight.
  const ghostSeenRef = useRef<Set<string>>(new Set())
  useEffect(() => {
    const wrap = wrapRef.current
    if (!wrap || !uploads.length) return
    const fresh = uploads.filter((u) => !ghostSeenRef.current.has(u.id))
    if (!fresh.length) return
    for (const u of fresh) ghostSeenRef.current.add(u.id)
    const laneEndX = (laneId: string) => hitsRef.current.reduce(
      (m, h) => (h.trackId === laneId ? Math.max(m, h.x + h.w) : m), labelWidth)
    const xAt = (laneId: string, t: number) =>
      labelWidth + (laneId === 'v1' ? t : renderTime(v1Seams, t)) * zoom
    const g = uploadGhosts(fresh, tracks.filter((t) => !isGhostLane(t)), laneEndX, xAt)[0]
    if (!g) return
    const right = g.x + g.w + 16
    if (right > wrap.scrollLeft + wrap.clientWidth) wrap.scrollLeft = right - wrap.clientWidth
    else if (g.x < wrap.scrollLeft + labelWidth) wrap.scrollLeft = Math.max(0, g.x - labelWidth - 16)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [uploads])

  // A newly selected clip — a fresh text, sticker or voiceover included —
  // is scrolled into view vertically (QA-014): new lanes used to land below
  // the fold with nothing on screen to say so.
  const revealedRef = useRef<string | null>(null)
  useEffect(() => {
    const wrap = wrapRef.current
    // Once per selection: scrolling away afterwards is the user's choice.
    if (!wrap || !selection || revealedRef.current === selection) return
    const row = tracks.findIndex((t) => t.clips.some((c) => c.id === selection))
    if (row < 0) return
    revealedRef.current = selection
    const top = trackY(row)
    const visTop = wrap.scrollTop + headerHeight
    const visBottom = wrap.scrollTop + wrap.clientHeight
    if (top < visTop) wrap.scrollTop = Math.max(0, top - headerHeight - 4)
    else if (top + trackHeight > visBottom) wrap.scrollTop = top + trackHeight - wrap.clientHeight + 4
  }, [selection, tracks])

  // Follow the playhead during playback. Without this the playhead simply left
  // the visible window and the timeline sat still while the video played on.
  //
  // Band check, not centre-on-every-frame: only scroll when the playhead crosses
  // outside the middle 60% of the viewport, then re-centre. `dragRef.current`
  // is checked so an in-progress drag is never yanked. Zoom is deliberately
  // NOT a dependency (QA-055): a zoom change keeps its own anchor (above), and
  // re-centring on the playhead there threw away the time under the cursor.
  const zoomNowRef = useRef(zoom)
  zoomNowRef.current = zoom
  useEffect(() => {
    const wrap = wrapRef.current
    if (!wrap || dragRef.current) return
    const x = labelWidth + playhead * zoomNowRef.current
    const viewLeft = wrap.scrollLeft
    const viewW = wrap.clientWidth
    if (viewW <= 0) return
    const lo = viewLeft + labelWidth + viewW * 0.2
    const hi = viewLeft + viewW * 0.8
    if (x < lo || x > hi) {
      const target = Math.max(0, x - viewW / 2)
      // Only move if it's a real jump — sub-pixel corrections every frame would
      // make the view jitter.
      if (Math.abs(target - viewLeft) > 8) wrap.scrollLeft = target
    }
  }, [playhead, labelWidth])


  // Hover feedback for the label column: a <canvas> can't give each row its
  // own `title`, so set the canvas's title to the hovered track's purpose +
  // state. Runs only while the mouse is over the 80px label strip.
  function onLabelMouseMove(e: React.MouseEvent) {
    const cv = e.currentTarget as HTMLCanvasElement
    const rect = cv.getBoundingClientRect()
    const y = e.clientY - rect.top
    let tip = ''
    for (let i = 0; i < tracks.length; i++) {
      const ty = trackY(i)
      if (y >= ty && y <= ty + trackHeight) {
        const t = tracks[i]
        if (isGhostLane(t)) { tip = 'New track — drop here to put it on a new lane'; break }
        const state = [t.muted ? 'muted' : '', t.solo ? 'soloed' : '', isTrackLocked(t) ? 'locked' : '']
          .filter(Boolean).join(' · ')
        const sound = laneHasSound(t)
        tip = `${laneTooltipHead(laneName(t), trackPurpose(t))}${state ? `\n(${state})` : ''}${sound ? '\nMute silences this track; Solo plays only soloed tracks' : ''}`
        break
      }
    }
    if (cv.title !== tip) cv.title = tip
  }

  // Track under the open context menu — drives the Mute/Unmute + Lock/Unlock
  // item labels so the menu states the action's direction, not a blind toggle.
  const menuTrack = contextMenu ? tracks.find((t) => t.id === contextMenu.trackId) : undefined
  // The active keymap's ripple-delete key, for the Delete tooltips (it is
  // Shift+Delete in the Premiere preset, where Delete lifts — QA-115).
  const rippleKeys = useKeymapStore((st) => (st.overrides.rippleDelete ?? st.effectiveMap().rippleDelete ?? [])
    .slice(0, 1).map(chordLabel).join(''))
  // Mute only means something on a track that actually carries audio. Text,
  // sticker, effect and caption tracks have no audio field at all — showing
  // "Mute clip"/"Mute track" there wasn't just confusing, "Mute clip" was
  // actively broken: set_volume's clip branch only writes `c.audio.gain_db`
  // when the target `isinstance(c, Clip)`, so on a TextClip/Sticker it did
  // nothing to the clip AND STILL committed an undo snapshot claiming
  // "Set clip volume → -60.0 dB" — a fake history entry for an edit that
  // never happened. Lock stays available everywhere: it's a real, working,
  // track-type-agnostic flag (Timeline.tsx's own drag guards check it
  // regardless of type), useful for pinning captions/stickers in place while
  // editing the video underneath.
  const menuTrackHasAudio = !!menuTrack &&
    (['video', 'audio', 'music', 'vo'] as const).includes(
      menuTrack.type as 'video' | 'audio' | 'music' | 'vo')
  // The clip under the menu's own mute flag (`audio.mute`), so the item states
  // its direction: Mute clip / Unmute clip (QA-080).
  const menuClip = contextMenu ? menuTrack?.clips.find((c) => c.id === contextMenu.clipId) : undefined
  const menuClipMuted = !!(menuClip as unknown as { audio?: { mute?: boolean } } | undefined)?.audio?.mute

  return (
    <>
      {/* Undo/Redo and the transport live HERE, not over the video and not in
          the top bar. Requested: "The undo, redo button and the time which is on
          the video needs to come down, these should be placed above the layers
          where zoom and key shortcuts currently placed."

          Both moved for the same reason they were wrong where they were: the
          transport floated ON the picture, covering the bottom-centre of the
          frame — exactly where captions and lower-thirds sit, so the controls
          obscured the thing being edited. Undo/Redo sat in `.topbar-scroll`,
          which scrolls horizontally once enough presets are added, so the two
          most-used buttons in the app could end up off-screen.

          The shortcut hint is the compressible half (it is a reminder, not a
          control), so it takes `min-width: 0` + ellipsis and gives way first on
          a narrow window; the buttons and the clock never shrink. */}
      <div className="timeline-toolbar">
        {/* Icons, like the rest of the toolbar (wave-B review): the two text
            buttons took the room that kept the zoom steps visible at 1024. */}
        <button
          className="tb-icon"
          onClick={() => dispatch('undo')}
          disabled={undoDepth === 0}
          title={undoTitle(undoDepth, chordLabel('Mod+KeyZ'))}
          aria-label="Undo" aria-keyshortcuts="Meta+Z"
        ><TimelineIcon name="undo" /></button>
        <button
          className="tb-icon"
          onClick={() => dispatch('redo')}
          disabled={!redoAvailable}
          title={redoAvailable ? `Redo (${chordLabel('Mod+Shift+KeyZ')})` : 'Nothing to redo — Redo re-applies an edit you just undid'}
          aria-label="Redo" aria-keyshortcuts="Meta+Shift+Z"
        ><TimelineIcon name="redo" /></button>
        <span className="tb-sep" />
        <button
          onClick={() => {
            // Same two store actions the floating transport used. The third
            // thing it did — writing <video>.currentTime and the rAF clock
            // directly — was documented there as "defense in depth" on top of
            // the playback effect's own proximity check, and it is not reachable
            // from here: the element belongs to Preview. replayFromStart() moves
            // the playhead through the store, which is what that effect follows.
            useStore.getState().replayFromStart()
            setPlaying(!isPlaying)
          }}
          title={isPlaying ? 'Pause (Space)' : 'Play (Space)'}
          style={{ minWidth: 30 }}
          aria-label={isPlaying ? 'Pause' : 'Play'}
          aria-keyshortcuts="Space"
          className="tb-icon"
        ><TimelineIcon name={isPlaying ? 'pause' : 'play'} /></button>
        {/* SMPTE clock (QA-048): the playhead's timecode, typed into to jump. */}
        <span className="tb-clock">
          <TimecodeField value={playhead} fps={fps} min={0} max={edl?.duration ?? undefined}
            ariaLabel="Playhead timecode" className="tb-clock-field"
            title="Playhead — type a timecode (HH:MM:SS:FF), seconds or frames and press Enter to jump"
            onCommit={(t) => { setPlaying(false); setPlayhead(t) }} />
          <span className="tb-clock-dur" aria-label="Duration">/ {formatTimecode(edl?.duration ?? 0, fps)}</span>
        </span>
        <span className="tb-sep" />
        {/* The icon toolbar (QA-053): the edit actions as buttons, wired to the
            same keymap commands as their shortcuts. */}
        <button className="tb-icon" onClick={() => void COMMAND_BY_ID.split.run(useStore.getState())}
          disabled={!hasClips}
          title={`Split at playhead (${chordLabel('Mod+KeyB')})`} aria-label="Split at playhead">
          <TimelineIcon name="split" /></button>
        <button className="tb-icon" onClick={() => void COMMAND_BY_ID.freezeFrame.run(useStore.getState())}
          disabled={freezePlan.kind !== 'freeze'}
          title={freezePlan.kind === 'freeze'
            ? `Freeze frame — hold the frame at the playhead${freezeHold}`
            : `Freeze frame — ${freezePlan.message}`}
          aria-label="Freeze frame"><TimelineIcon name="freeze" /></button>
        <button className="tb-icon" onClick={() => void COMMAND_BY_ID.rippleDelete.run(useStore.getState())}
          disabled={!selection && multiSelection.length === 0}
          title={`Delete selection${rippleKeys ? ` (${rippleKeys})` : ''} — Main video closes the gap; other lanes keep their times`}
          aria-label="Delete selection"><TimelineIcon name="delete" /></button>
        <button className="tb-icon" onClick={() => void COMMAND_BY_ID.duplicate.run(useStore.getState())}
          disabled={!selection && multiSelection.length === 0}
          title={`Duplicate selection (${chordLabel('Mod+KeyD')})`} aria-label="Duplicate selection">
          <TimelineIcon name="duplicate" /></button>
        <span className="tb-sep" />
        {/* Snap with visible state (QA-050): N used to flip it silently. */}
        <button className={`tb-icon tb-toggle${snapEnabled ? ' is-on' : ''}`} aria-pressed={snapEnabled}
          onClick={() => useStore.getState().toggleSnap()}
          title={`Snapping ${snapEnabled ? 'on' : 'off'} — clips snap to the playhead, markers and clip edges (N)`}
          aria-label="Snapping"><TimelineIcon name="snap" /></button>
        <div style={{ flex: 1, minWidth: 8 }} />
        <button className="tb-icon tb-zoomstep" onClick={() => zoomBy(1 / 1.25)} title="Zoom out" aria-label="Zoom out">
          <TimelineIcon name="zoomOut" /></button>
        <input type="range" className="tb-zoom" min={0} max={SLIDER_STEPS} value={zoomToSlider(zoom)}
          onChange={(e) => zoomTo(sliderToZoom(Number(e.target.value)))}
          aria-label="Timeline zoom" aria-valuetext={`${visibleSpanLabel(zoom, laneW)} visible`}
          title={`${visibleSpanLabel(zoom, laneW)} visible`} />
        <button className="tb-icon tb-zoomstep" onClick={() => zoomBy(1.25)} title="Zoom in" aria-label="Zoom in">
          <TimelineIcon name="zoomIn" /></button>
        <button className="tb-icon" onClick={() => void COMMAND_BY_ID.zoomFit.run(useStore.getState())}
          disabled={!hasClips}
          title={`Zoom to fit the whole timeline (${chordLabel('Mod+Backslash')})`} aria-label="Zoom to fit">
          <TimelineIcon name="fit" /></button>
      </div>
      <div
        className="timeline-canvas-wrap"
        ref={wrapRef}
        style={{ position: 'relative' }}
        onMouseDown={onWrapMouseDown}
        onDragOver={onCanvasDragOver}
        onDragLeave={onCanvasDragLeave}
        onDrop={onCanvasDrop}
      >
        {/* Scroll extent (contentW × contentH) with a STICKY viewport layer
            inside it: the canvases are only as wide as the visible pane and
            stay pinned to it while this element scrolls natively (QA-024). */}
        <div ref={contentRef} style={{ position: 'relative', width: contentW, height: contentH }}>
          <div style={{ position: 'sticky', left: 0, width: Math.min(size.w, contentW), height: contentH }}>
            <canvas
              ref={canvasRef}
              onMouseDown={onMouseDown}
              onMouseMove={onMouseMove}
              onMouseLeave={onMouseLeave}
              onMouseUp={onMouseUp}
              onContextMenu={onContextMenu}
              onKeyDown={onCanvasKeyDown}
              tabIndex={0}
              role="application"
              aria-roledescription="timeline"
              aria-label="Timeline"
              aria-keyshortcuts="Shift+F10"
              style={{ display: 'block', cursor: 'default' }}
            />
            <canvas
              ref={playheadCanvasRef}
              aria-hidden="true"
              style={{ position: 'absolute', top: 0, left: 0, pointerEvents: 'none' }}
            />
          </div>
        </div>
        {/* Sticky label column: absolutely positioned and re-translated to
            track wrapRef's scrollLeft on every scroll event (see the effect
            below), so track names + mute toggles stay pinned to the visible
            left edge while the main canvas scrolls underneath. Handles its
            own mousedown for the mute toggle (the only interactive control in
            the label area) since it visually sits on top of the main canvas
            once scrolled. */}
        <canvas
          ref={labelCanvasRef}
          aria-hidden="true"
          onMouseMove={onLabelMouseMove}
          style={{ position: 'absolute', top: 0, left: 0, display: 'block', zIndex: 1, cursor: 'default' }}
        />
        {/* Mute / Solo per sound lane (QA-086; wave C review): real buttons
            over the label canvas — keyboard-reachable, aria-pressed, lucide
            icons — where 12 px canvas boxes with 9 px "M"/"S" used to be.
            Translated with the label canvas on horizontal scroll. */}
        <div ref={monitorLayerRef} className="lane-monitors"
             style={{ width: labelWidth, height: contentH }}>
          {tracks.map((t, i) => {
            if (isGhostLane(t) || !laneHasSound(t)) return null
            const b = monitorButtons(trackY(i), trackHeight)
            const names = monitorLabels(laneName(t))
            return (
              <Fragment key={t.id}>
                <button type="button" className="lane-monitor" data-monitor="mute" aria-pressed={!!t.muted}
                  aria-label={names.mute} title={t.muted ? `Unmute ${laneName(t)}` : names.mute}
                  style={{ left: b.mute.x, top: b.mute.y, width: b.mute.w, height: b.mute.h }}
                  onClick={() => void dispatch('set_track_muted', { track: t.id, muted: !t.muted })}>
                  <Icon name={t.muted ? 'laneMuted' : 'laneAudible'} />
                </button>
                <button type="button" className="lane-monitor" data-monitor="solo" aria-pressed={!!t.solo}
                  aria-label={names.solo} title={t.solo ? `Stop soloing ${laneName(t)}` : `${names.solo} — hear only soloed tracks`}
                  style={{ left: b.solo.x, top: b.solo.y, width: b.solo.w, height: b.solo.h }}
                  onClick={() => void dispatch('set_track_solo', { track: t.id, solo: !t.solo })}>
                  <Icon name="solo" />
                </button>
              </Fragment>
            )
          })}
        </div>
      </div>
      {transPopover && sid && (
        <TransitionPopover
          x={transPopover.x}
          y={transPopover.y}
          at={transPopover.at}
          existing={transPopover.existing}
          sessionId={sid}
          onApply={(type, duration) => {
            const at = transPopover.at
            setTransPopover(null)
            // add_transition REPLACES any transition within 0.05s of the same
            // cut rather than appending, so re-applying here is a genuine
            // update and leaves no superseded entry behind. (It used to
            // append, with last-match-wins at render time — the stale entries
            // then made the EDL disagree with the picture.)
            void dispatch('add_transition', { at, type, duration })
          }}
          onRemove={() => {
            const at = transPopover.at
            setTransPopover(null)
            // Sweeps EVERY entry within 0.05s of the cut, not just one:
            // legacy projects and MCP callers can still hold a stack there.
            void dispatch('remove_transition', { at })
          }}
          onClose={() => setTransPopover(null)}
        />
      )}
      {contextMenu && (
        <div
          ref={contextMenuRef}
          role="menu"
          aria-label="Clip actions"
          data-keymap-ignore
          onKeyDown={ctxA11y.onKeyDown}
          onMouseDown={(e) => e.stopPropagation()}
          style={{
            position: 'fixed', left: contextMenu.x, top: contextMenu.y, zIndex: 100,
            background: 'var(--bg-2)', border: '1px solid var(--line)', borderRadius: 6,
            boxShadow: '0 8px 24px rgba(0,0,0,0.5)', minWidth: 180, padding: 4,
          }}
        >
          {[
            // "Split here" actually split at the PLAYHEAD, not the click point
            // — name it what it does and say so in the tooltip.
            { label: 'Split at playhead',
              title: `Cut the clip under the playhead in two (${chordLabel('Mod+KeyB')}). Move the playhead to where you want the cut first.`,
              // Decoded per lane (lib/splitTargets): `split_at` takes layout
              // time and the playhead is render time — same path as ⌘B.
              action: () => useStore.getState().splitTrackAt(
                contextMenu.trackId, splitTimeFor(edl, contextMenu.trackId, playhead)) },
            ...(contextMenu.trackId === 'v1' && menuClip && isMediaClip(menuClip)
              ? [{ label: 'Freeze frame',
                   title: `Hold the frame at the playhead${freezeHold}; the rest of the clip follows it`,
                   action: () => { void freezeAtPlayhead(useStore.getState(), toast.info, contextMenu.clipId) } }]
              : []),
            { label: 'Duplicate',
              title: `Add a copy of this clip right after it (${chordLabel('Mod+KeyD')})`,
              action: () => dispatch('duplicate_clip', { clip_id: contextMenu.clipId }) },
            { label: 'Delete',
              title: contextMenu.trackId === 'v1'
                ? `Remove this clip and close the gap${rippleKeys ? ` (${rippleKeys})` : ''}`
                : `Remove this clip — the clips around it keep their times${rippleKeys ? ` (${rippleKeys})` : ''}`,
              action: () => dispatch('ripple_delete', { clip_id: contextMenu.clipId }) },
            ...(menuTrackHasAudio
              ? [
                  // The real clip mute flag (QA-080): set_volume −60 dB threw
                  // away the clip's gain and could never be undone from here.
                  { label: menuClipMuted ? 'Unmute clip' : 'Mute clip',
                    title: menuClipMuted ? 'Bring this clip\'s sound back (its volume is unchanged)'
                      : 'Silence just this clip — its volume setting is kept',
                    action: () => dispatch('set_clip_muted', { clip_id: contextMenu.clipId, muted: !menuClipMuted }) },
                  // QA-086: J/L cuts — the picture keeps playing, its sound
                  // becomes an audio clip that trims and moves on its own.
                  ...(menuTrack?.type === 'video' && !menuClipMuted && menuClip && isMediaClip(menuClip)
                    ? [{ label: 'Detach audio',
                         title: 'Move this clip\'s sound to an audio track so picture and sound can be cut separately (J and L cuts)',
                         action: () => dispatch('detach_audio', { clip_id: contextMenu.clipId }) }]
                    : []),
                  { sep: true },
                  { label: menuTrack?.muted ? 'Unmute track' : 'Mute track',
                    title: 'Toggle sound for the whole track — the M box on its label does the same',
                    action: () => dispatch('set_track_muted', { track: contextMenu.trackId }) },
                  { label: menuTrack?.solo ? 'Unsolo track' : 'Solo track',
                    title: 'Hear only soloed tracks — the S box on its label does the same',
                    action: () => dispatch('set_track_solo', { track: contextMenu.trackId }) },
                ]
              : [{ sep: true }]),
            { label: menuTrack && isTrackLocked(menuTrack) ? 'Unlock track' : 'Lock track',
              title: 'Mark the track locked — a padlock appears on its label',
              action: () => dispatch('set_track_locked', { track: contextMenu.trackId }) },
            ...(multiSelection.length || (selection && selection !== contextMenu.clipId)
              ? [
                  { sep: true },
                  { label: `Delete ${(selection ? 1 : 0) + multiSelection.length + (selection === contextMenu.clipId ? 0 : 1)} selected`,
                    title: 'Remove every selected clip (shift-click selects more)',
                    action: () => {
                      const ids = Array.from(new Set([
                        contextMenu.clipId, selection, ...multiSelection,
                      ].filter(Boolean) as string[]))
                      dispatch('bulk_delete', { clip_ids: ids })
                  } },
                  { label: 'Duplicate selected',
                    title: 'Add a copy of every selected clip',
                    action: () => {
                      const ids = Array.from(new Set([
                        contextMenu.clipId, selection, ...multiSelection,
                      ].filter(Boolean) as string[]))
                      dispatch('bulk_duplicate', { clip_ids: ids })
                  } },
                ]
              : []),
          ].map((item, i) => (
            'sep' in item ? (
              <div key={`sep-${i}`} role="separator" style={{ height: 1, background: 'var(--line)', margin: '4px 0' }} />
            ) : (
              // <button role="menuitem"> (QA-102): these were click-only <div>s.
              <button
                type="button"
                role="menuitem"
                className="menu-item"
                key={item.label}
                title={item.title}
                onClick={() => { item.action(); ctxA11y.close() }}
              >
                {item.label}
              </button>
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

// The transition affordance on a cut (QA-051): accent-filled with a white
// glyph when the cut has a transition, hollow with a dim glyph when it can
// take one. Colours are the theme tokens, like every other canvas glyph.
function drawBowtie(ctx: CanvasRenderingContext2D, cx: number, cy: number, hasTransition: boolean) {
  ctx.save()
  ctx.beginPath()
  ctx.arc(cx, cy, 7, 0, Math.PI * 2)
  ctx.fillStyle = hasTransition ? cssToken('--accent-2', '#5b8dff') : cssToken('--bg-1', '#16161a')
  ctx.fill()
  ctx.strokeStyle = hasTransition ? cssToken('--selection', '#ffffff') : cssToken('--text-dim', '#9b9ba5')
  ctx.lineWidth = hasTransition ? 1 : 1.25
  ctx.stroke()
  ctx.fillStyle = hasTransition ? cssToken('--selection', '#ffffff') : cssToken('--text-dim', '#9b9ba5')
  ctx.beginPath()
  ctx.moveTo(cx - 4, cy - 3); ctx.lineTo(cx - 1, cy); ctx.lineTo(cx - 4, cy + 3)
  ctx.closePath(); ctx.fill()
  ctx.beginPath()
  ctx.moveTo(cx + 4, cy - 3); ctx.lineTo(cx + 1, cy); ctx.lineTo(cx + 4, cy + 3)
  ctx.closePath(); ctx.fill()
  ctx.restore()
}

// A small dark caption chip on the drag overlay (landing time, snap target,
// refusal reason), clamped so it never starts left of the lane area.
function drawCaption(ctx: CanvasRenderingContext2D, text: string, x: number, y: number, color: string) {
  ctx.save()
  ctx.font = uiFont(10)
  const w = ctx.measureText(text).width
  const cx = Math.max(84, x)
  ctx.fillStyle = 'rgba(14,14,16,0.85)'
  ctx.fillRect(cx - 4, y, w + 8, 14)
  ctx.fillStyle = color
  ctx.fillText(text, cx, y + 10)
  ctx.restore()
}

