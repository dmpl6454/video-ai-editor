// Browser-side text overlay layer. Draws all text/captions clips on a
// transparent <canvas> stacked over the <video>. Updates per frame via
// requestAnimationFrame, sampling the current playhead so overlays appear/
// disappear in real time. This avoids a server round-trip for every text
// edit — only video-track changes trigger an ffmpeg re-render.

import { useEffect, useRef, useState } from 'react'
import type { EDL, TextClip } from '../types'
import {
  sampleKF, publishTextBoxes, getOverlayDrag,
  type KFNum, type OverlayBox,
} from '../lib/overlay'
import { emojiImage, emojiGeneration } from '../lib/emojiArt'

interface Props {
  edl: EDL
  videoEl: HTMLVideoElement | null
  // The element rect to draw within (matches the <video> on screen)
  width: number
  height: number
}

// `size` here is the SAME fixed pixel value render/text_overlay.py's
// ROLE_STYLES uses (`style["size"]`, sized against the EDL canvas — see
// `ImageFont.truetype(..., style["size"])` there). Previously this was a
// hand-tuned fraction of the on-screen preview height (e.g. 0.075 for
// "super"), which only approximated the server's `140 / canvas.h` ratio for
// a canvas.h of ~1920 and drifted for any other canvas size (drifted further
// after set_canvas/set_aspect_ratio/auto_reframe change canvas.h). Drawing
// now computes `fontPx = (size / edl.canvas.h) * height`, i.e. the same
// canvas-relative fraction the server derives, scaled to however big the
// preview box is actually rendered on screen — so the two stay in lockstep
// for any canvas size, not just the common vertical default.
// `stroke` is still a fraction of on-screen height (the server's stroke_w is
// a small fixed px count with no strong visual sensitivity to canvas size,
// so an approximate on-screen fraction is fine here).
const ROLE_STYLES: Record<string, {
  font: string; size: number; weight?: string; stroke: number; upper?: boolean; align: 'top' | 'center' | 'bottom' | 'lower'; opacity?: number;
}> = {
  super:       { font: 'Anton',           size: 140, stroke: 0.005, upper: true,  align: 'lower' },
  hook:        { font: 'Bebas Neue',      size: 170, stroke: 0.006, upper: true,  align: 'center' },
  lower_third: { font: 'Montserrat',      size: 56,  stroke: 0.0025, weight: '700', align: 'lower' },
  caption:     { font: 'Inter',           size: 64,  stroke: 0.004, weight: '900', align: 'bottom' },
  label:       { font: 'Inter',           size: 48,  stroke: 0.0025, weight: '700', align: 'top' },
  watermark:   { font: 'Inter',           size: 32,  stroke: 0.0015, weight: '700', align: 'bottom', opacity: 0.7 },
  default:     { font: 'Inter',           size: 64,  stroke: 0.0025, weight: '700', align: 'lower' },
}

// --- inline emoji, mirroring render/text_overlay.py --------------------------
//
// Emoji used to be stripped from text here AND on the server, so typing them
// into a text clip produced nothing anywhere ("I was unable to apply the
// emojis through the text section"). They are composited as IMAGES now — the
// same Apple/iOS artwork the exporter bakes, fetched from
// /api/emoji/<seq>.png. Drawing them with the browser's own emoji font instead
// would put the OS design in the preview and the fetched set in the delivered
// file: the exact preview/export mismatch stickers already had. (The fetched
// artwork is a pinned release, NOT the local font of the same name — the
// substitution is never safe, on any platform.)
//
// EMOJI_BOX_RATIO must stay equal to text_overlay.py's, or the preview wraps
// differently from the bake.
const EMOJI_BOX_RATIO = 1.0
// Fraction of the box the artwork fills; the rest is side bearing, centred.
// Mirrors text_overlay.py's EMOJI_INK_RATIO and must move with it. A PNG has no
// side bearing of its own, and the sources this chain mixes pad their tiles
// anywhere from 0.000 to 0.131 — so without this, spacing is whatever the
// artwork happened to ship with, and differs emoji-to-emoji within one line.
// Advance is unchanged, so wrapping is unaffected on both sides.
const EMOJI_INK_RATIO = 0.92
const ZWJ = '\u{200D}'
const EMOJI_MOD = new Set(['\u{FE0F}', '\u{20E3}',
  '\u{1F3FB}', '\u{1F3FC}', '\u{1F3FD}', '\u{1F3FE}', '\u{1F3FF}'])
// ZWJ / VS16 / keycap are class MEMBERS on purpose: they keep a
// multi-codepoint emoji inside ONE match so emojiClusters() can split it
// correctly. Written as escapes, not literals — invisible characters in a
// regex are unreadable and unreviewable.
// eslint-disable-next-line no-misleading-character-class
const RUN_RE = /[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}\u{1F1E6}-\u{1F1FF}\u{200D}\u{FE0F}\u{20E3}]+/gu

/** Split one matched emoji run into individual renderable emoji, keeping ZWJ
 *  sequences, flag pairs, skin tones and keycaps together. */
function emojiClusters(run: string): string[] {
  const cp = Array.from(run)
  const out: string[] = []
  let i = 0
  const isRI = (ch: string) => ch >= '\u{1F1E6}' && ch <= '\u{1F1FF}'
  while (i < cp.length) {
    const start = i
    const ch = cp[i]; i++
    if (isRI(ch)) {
      if (i < cp.length && isRI(cp[i])) i++
    } else {
      while (i < cp.length && EMOJI_MOD.has(cp[i])) i++
      while (i < cp.length && cp[i] === ZWJ) {
        i++
        if (i < cp.length) i++
        while (i < cp.length && EMOJI_MOD.has(cp[i])) i++
      }
    }
    out.push(cp.slice(start, i).join(''))
  }
  return out
}

type Seg = { emoji: boolean; s: string }

function tokenize(s: string): Seg[] {
  const out: Seg[] = []
  let pos = 0
  for (const m of s.matchAll(RUN_RE)) {
    const at = m.index ?? 0
    if (at > pos) out.push({ emoji: false, s: s.slice(pos, at) })
    for (const cl of emojiClusters(m[0])) out.push({ emoji: true, s: cl })
    pos = at + m[0].length
  }
  if (pos < s.length) out.push({ emoji: false, s: s.slice(pos) })
  return out
}

function lineWidth(ctx: CanvasRenderingContext2D, line: string, box: number): number {
  let w = 0
  for (const seg of tokenize(line)) w += seg.emoji ? box : ctx.measureText(seg.s).width
  return w
}

// Emoji artwork (fetch + cache + the arrival counter) lives in lib/emojiArt.

function isText(c: unknown): c is TextClip {
  return !!c && typeof c === 'object' && 'text' in (c as object) && 'end' in (c as object)
}

// Backend TextStyle defaults act as "use the role style" sentinels — mirror
// of render/text_overlay.py's two-part rule, so preview and export resolve
// per-clip styles identically. Font is unset when EITHER: (a) it's the raw
// schema default 'Inter-Black' (the actual "did the caller touch this
// field" signal — nothing here tracks per-field set-ness), OR (b) it
// matches the RESOLVED ROLE'S OWN font (e.g. caption's own role font
// genuinely IS Inter-Black, so reaffirming it is a semantic no-op). (b)
// alone is wrong: the 'default' role's real font is Inter, not Inter-Black,
// so a default-role clip's default-populated style would misread as an
// explicit override without check (a).
const SENTINEL_COLOR = '#FFFFFF'
const SENTINEL_FONT = 'Inter-Black'
// TextStyle.size schema default — any other value is an explicit size in
// EDL-canvas px (the same coordinate system ROLE_STYLES sizes live in).
const SENTINEL_SIZE = 96
// TextStyle stroke defaults — mirror of the server's stroke sentinels.
const SENTINEL_STROKE = '#000000'
const SENTINEL_STROKE_W = 4
// TextClip's Transform schema default is (x=540, y=1700) — absolute canvas
// px that historically no renderer read. Mirrors the server's
// resolve_anchor_overrides (render/text_overlay.py): a value is an explicit
// anchor only when it can't be a construction-site default —
//   x sentinels: 540 and canvas.w/2 (tool default = hard-coded centering)
//   y sentinels: 1700, canvas.h*0.85 (add_text's no-arg default), and the
//     role's own server-side anchor y (add_super_text/brand_kit write it)
// caption role: transform overrides are ignored entirely (the captions
// block owns caption positioning). Keyframed x/y also resolve as unset.
const SENTINEL_X = 540
const SENTINEL_Y = 1700

// The SERVER's role anchor y in canvas coords (_y_for_role with no
// override) — this is what add_super_text/brand_kit persist, so it is the
// value the sentinel comparison must run against. NOT the browser's own
// draw anchors below (those stay authoritative for how a role-positioned
// clip actually draws on screen).
function serverAnchorY(role: string, canvasH: number): number {
  if (role === 'watermark') return canvasH - canvasH * 0.04
  if (role === 'hook') return canvasH * 0.5
  if (role === 'caption') return canvasH - canvasH * 0.16
  if (role === 'lower_third') return canvasH - canvasH * 0.2
  return canvasH * 0.75
}

// Explicit (anchorX, anchorY) in EDL-canvas px, or nulls (role layout).
// Same tolerance (±0.5 canvas px) as the server, absorbing the float noise
// _rescale_overlays_for_canvas_change multiplication introduces.
// The exact values resolveAnchor treats as "unset". Exported through the
// published OverlayBox so the interaction layer can avoid committing a drag
// that would land on one (and silently snap back to the role layout).
function xSentinelsFor(canvasW: number): number[] {
  return [SENTINEL_X, canvasW / 2]
}
function ySentinelsFor(role: string, canvasH: number): number[] {
  return [SENTINEL_Y, canvasH * 0.85, serverAnchorY(role, canvasH)]
}

function resolveAnchor(
  c: TextClip, role: string, canvasW: number, canvasH: number,
): { ax: number | null; ay: number | null } {
  if (role === 'caption') return { ax: null, ay: null }
  const tx = (c as TextClip & { transform?: { x?: unknown; y?: unknown } }).transform
  if (!tx) return { ax: null, ay: null }
  const pick = (v: unknown, sentinels: number[]): number | null => {
    if (typeof v !== 'number' || !Number.isFinite(v)) return null // keyframed / missing
    for (const s of sentinels) if (Math.abs(v - s) < 0.5) return null
    return v
  }
  return {
    ax: pick(tx.x, xSentinelsFor(canvasW)),
    ay: pick(tx.y, ySentinelsFor(role, canvasH)),
  }
}

function roleFontMatches(role: string, ttf: string): boolean {
  const want = cssFont(ttf)
  const roleStyle = ROLE_STYLES[role] ?? ROLE_STYLES.default
  if (!want) return false
  return want.family === roleStyle.font && want.weight === (roleStyle.weight ?? '700')
}

// Bundled ttf name (backend) → CSS family + weight (what @font-face declares).
function cssFont(ttf: string): { family: string; weight: string } | null {
  const stem = ttf.replace(/\.ttf$/i, '')
  const [fam, variant] = stem.split('-')
  const family = { Anton: 'Anton', BebasNeue: 'Bebas Neue', Montserrat: 'Montserrat', Inter: 'Inter' }[fam]
  if (!family) return null // Noto/unknown — let the role default stand
  const weight = variant === 'Black' ? '900' : variant === 'Bold' ? '700' : '400'
  return { family, weight }
}

// Animation envelope for anim_in/anim_out presets — the same curves the
// server bakes (render/text_overlay.py): d = min(0.35, 40% of clip), pop-in
// overshoots 0.6→1.06→1.0, pop-out shrinks to 0.6, slides travel 4% of the
// preview height, fades ramp alpha linearly.
function animEnvelope(c: TextClip, t: number, height: number): { alpha: number; scale: number; dy: number } {
  const d = Math.min(0.35, Math.max(0.1, (c.end - c.start) * 0.4))
  const off = height * 0.04
  const qIn = Math.min(1, Math.max(0, (t - c.start) / d))
  const qOut = Math.min(1, Math.max(0, (t - (c.end - d)) / d))
  let alpha = 1, scale = 1, dy = 0
  if (c.anim_in === 'fade') alpha *= qIn
  if (c.anim_out === 'fade') alpha *= 1 - qOut
  if (c.anim_in === 'pop') scale *= qIn < 0.7 ? 0.6 + 0.657 * qIn : 1.06 - 0.2 * (qIn - 0.7)
  if (c.anim_out === 'pop') scale *= 1 - 0.4 * qOut
  if (c.anim_in === 'slide_up') dy += off * (1 - qIn)
  if (c.anim_in === 'slide_down') dy -= off * (1 - qIn)
  if (c.anim_out === 'slide_up') dy -= off * qOut
  if (c.anim_out === 'slide_down') dy += off * qOut
  return { alpha, scale, dy }
}

function wrap(ctx: CanvasRenderingContext2D, text: string, maxW: number,
              box = 0): string[] {
  const out: string[] = []
  for (const para of text.split('\n')) {
    // Each emoji is its own wrap-word: the text font measures it at ~0px, so
    // gluing it to a neighbour overflows the line by exactly its box width.
    // `glued` records that the SOURCE had no space at that boundary, so the
    // rejoin below can't invent one — mirror of _emoji_words in
    // text_overlay.py, and see its comment for what inventing them looked like.
    const words = box ? wrapUnits(para)
                      : para.split(/\s+/).filter(Boolean).map((w) => [false, w] as const)
    if (!words.length) { out.push(''); continue }
    let cur = words[0][1]
    for (let i = 1; i < words.length; i++) {
      const [glued, word] = words[i]
      const trial = `${cur}${glued ? '' : ' '}${word}`
      const w = box ? lineWidth(ctx, trial, box) : ctx.measureText(trial).width
      if (w <= maxW) cur = trial
      else { out.push(cur); cur = word }
    }
    out.push(cur)
  }
  return out
}

/** Wrap units as [gluedToPrevious, unit]. Mirror of `_emoji_words`. */
function wrapUnits(para: string): (readonly [boolean, string])[] {
  const units: (readonly [boolean, string])[] = []
  let prevWs = true               // start of string separates like whitespace
  for (const seg of tokenize(para)) {
    if (seg.emoji) {
      units.push([units.length > 0 && !prevWs, seg.s] as const)
      prevWs = false
      continue
    }
    const parts = seg.s.split(/\s+/).filter(Boolean)
    if (!parts.length) { prevWs = true; continue }
    parts.forEach((w, i) => units.push(
      [i === 0 && units.length > 0 && !/^\s/.test(seg.s), w] as const))
    prevWs = /\s$/.test(seg.s)
  }
  return units
}

/** Vertical middle of the cap band, in canvas y — what the eye aligns an emoji
 *  to. Mirror of text_overlay.py's `_cap_band_mid`, measured from THIS side's
 *  metrics: `cy` is the em-box middle here (textBaseline 'middle') whereas
 *  Pillow draws from the ascender top, so only the measured band is common
 *  ground. Falls back to `cy` on the (long-obsolete) engines that don't report
 *  actualBoundingBox*. */
function capBandMid(ctx: CanvasRenderingContext2D, cy: number): number {
  const m = ctx.measureText('H')
  const asc = m.actualBoundingBoxAscent, desc = m.actualBoundingBoxDescent
  if (typeof asc !== 'number' || typeof desc !== 'number') return cy
  return cy + (desc - asc) / 2
}

/** Draw one wrapped line centred on `cx`, walking text runs and emoji boxes.
 *  `paint` picks the pass: stroke (shadow/outline) or fill. */
function drawLine(ctx: CanvasRenderingContext2D, line: string, cx: number, cy: number,
                  box: number, paint: 'stroke' | 'fill'): void {
  const segs = tokenize(line)
  let x = cx - lineWidth(ctx, line, box) / 2
  const prevAlign = ctx.textAlign
  ctx.textAlign = 'left'
  // Measured once per line, and only when there IS an emoji to place.
  let capMid: number | null = null
  for (const seg of segs) {
    if (seg.emoji) {
      // Only on the fill pass: an emoji is artwork, it takes no outline, and
      // painting it twice would double its opacity.
      if (paint === 'fill') {
        const im = emojiImage(seg.s)
        if (capMid === null) capMid = capBandMid(ctx, cy)
        // Drawn at `ink`, centred in the full `box` advance — see EMOJI_INK_RATIO.
        const ink = box * EMOJI_INK_RATIO
        if (im) ctx.drawImage(im, x + (box - ink) / 2, capMid - ink / 2, ink, ink)
      }
      x += box
      continue
    }
    if (paint === 'stroke') ctx.strokeText(seg.s, x, cy)
    else ctx.fillText(seg.s, x, cy)
    x += ctx.measureText(seg.s).width
  }
  ctx.textAlign = prevAlign
}

export function TextLayer({ edl, videoEl, width, height }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  // `ctx.font = '"Anton"'` does NOT trigger the browser to actually fetch the
  // @font-face file — the canvas just silently falls back to system sans
  // until something else (e.g. text laid out in the DOM) forces the load.
  // We explicitly kick off the load for every bundled family/weight used by
  // ROLE_STYLES and gate the first draw on `document.fonts.ready`, so the
  // preview never draws a frame or two of the wrong font before swapping —
  // which would itself look like a (transient) preview↔export mismatch.
  const [fontsReady, setFontsReady] = useState(false)

  useEffect(() => {
    let cancelled = false
    const specs = [
      '400 32px Anton',
      '400 32px "Bebas Neue"',
      '700 32px Montserrat',
      '700 32px Inter',
      '900 32px Inter',
    ]
    Promise.all(specs.map((spec) => document.fonts.load(spec)))
      .catch(() => { /* best-effort: fall through to fonts.ready below */ })
      .then(() => document.fonts.ready)
      .then(() => { if (!cancelled) setFontsReady(true) })
    return () => { cancelled = true }
  }, [])

  useEffect(() => {
    const cv = canvasRef.current
    if (!cv) return
    const dpr = window.devicePixelRatio || 1
    cv.width = Math.max(1, Math.round(width * dpr))
    cv.height = Math.max(1, Math.round(height * dpr))
    cv.style.width = `${width}px`
    cv.style.height = `${height}px`
    const ctx = cv.getContext('2d')!
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)

    // CLEAR IN DEVICE PIXELS — the same rule, and the same reason, as
    // StickerLayer's draw loop (read the long note there). `cv.width` is
    // `Math.round(width * dpr)` while a CSS-space `clearRect(0,0,width,height)`
    // under the dpr transform only reaches `width * dpr`, so at a FRACTIONAL
    // dpr the last device column and row are never erased and hold their ink
    // forever.
    //
    // This layer is the SIBLING of the one the "phantom colour strip" was
    // fixed in, drawing over the same video with the same sizing, so it had the
    // identical latent defect — it simply went unreported because glyph ink
    // reaches the far edge less often than selection chrome does. Fixing one
    // canvas and leaving the other is why the strip survived three earlier
    // diagnoses. Fractional dpr is not Windows-only: 125% scaling makes it
    // routine there, and a Mac on a scaled Retina mode (e.g. 1.7647) hits it
    // just as well.
    const clearAll = () => {
      ctx.setTransform(1, 0, 0, 1, 0, 0)
      ctx.clearRect(0, 0, cv.width, cv.height)
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    }

    // If there's neither text nor stickers anywhere, skip RAF entirely.
    // Stickers are drawn + manipulated by <StickerLayer>; this layer is text only.
    const hasAnyText = edl.tracks.some((tk) =>
      (tk.type === 'text' || tk.type === 'captions') &&
      tk.clips.some((c) => isText(c))
    )
    if (!hasAnyText) {
      clearAll()
      publishTextBoxes([])
      return
    }

    let raf = 0
    let lastTime = -1
    let lastDragId: string | null = null
    let lastEmojiGen = -1
    const draw = () => {
      const t = videoEl ? videoEl.currentTime : 0
      const drag = getOverlayDrag()
      // Only redraw when the playhead actually advanced (or first frame) — but
      // ALWAYS redraw while a drag is live, or the text would sit frozen at its
      // pre-drag position on a paused preview and the gesture would look dead.
      // Emoji artwork arriving counts as a change too: it is fetched during a
      // draw and lands after it, so on a PAUSED preview nothing else would ever
      // trigger the repaint that actually paints it (see emojiGeneration()).
      const dragActive = !!drag || lastDragId !== null
      lastDragId = drag?.id ?? null
      if (!dragActive && emojiGeneration() === lastEmojiGen
          && Math.abs(t - lastTime) < 1 / 60 && lastTime >= 0) {
        raf = requestAnimationFrame(draw)
        return
      }
      lastTime = t
      lastEmojiGen = emojiGeneration()
      clearAll()
      const boxes: OverlayBox[] = []

      // Collect active text clips
      const active: { c: TextClip; role: string }[] = []
      for (const tk of edl.tracks) {
        if (tk.type !== 'text' && tk.type !== 'captions') continue
        for (const c of tk.clips) {
          if (!isText(c)) continue
          if (c.start <= t && t <= c.end) active.push({ c, role: (c as TextClip & { role?: string }).role ?? 'default' })
        }
      }

      // Sort by role priority: watermark drawn first (under), hook last (top)
      const order = ['watermark', 'lower_third', 'caption', 'label', 'super', 'hook']
      active.sort((a, b) => order.indexOf(a.role) - order.indexOf(b.role))

      for (const { c, role } of active) {
        const s = ROLE_STYLES[role] ?? ROLE_STYLES.default
        // Per-clip style overrides (non-sentinel values only — see cssFont/
        // roleFontMatches above; mirrors the server's resolve_style_overrides).
        const styleColor = c.style?.color && c.style.color.toUpperCase() !== SENTINEL_COLOR
          ? c.style.color : null
        const styleFont = c.style?.font && c.style.font !== SENTINEL_FONT
          && !roleFontMatches(role, c.style.font)
          ? cssFont(c.style.font) : null
        // style.size (non-sentinel) is an explicit size in EDL-canvas px —
        // exactly the coordinate system s.size (ROLE_STYLES) lives in, so
        // both rescale to the on-screen preview box the same way. Mirrors
        // the server's resolve_size_override.
        const sizeCanvasPx = typeof c.style?.size === 'number'
          && Number.isFinite(c.style.size) && c.style.size > 0
          && Math.abs(c.style.size - SENTINEL_SIZE) > 1e-6
          ? c.style.size : s.size
        // A live corner-resize scales the drawn glyphs immediately; the EDL
        // only changes on pointer-up (StickerLayer commits style.size then).
        const sizeMul = drag?.id === c.id ? drag.sizeMul : 1
        const fontPx = Math.round((sizeCanvasPx * sizeMul / edl.canvas.h) * height)
        const family = styleFont?.family ?? s.font
        const weight = styleFont?.weight ?? s.weight ?? 'bold'
        ctx.font = `${weight} ${fontPx}px "${family}", system-ui, sans-serif`
        ctx.textBaseline = 'middle'
        ctx.textAlign = 'center'
        ctx.lineJoin = 'round'
        // Custom stroke_w is in canvas px like the server's; the role
        // fallback keeps the historic on-screen-height fraction.
        const styleStrokeW = typeof c.style?.stroke_w === 'number'
          && Number.isFinite(c.style.stroke_w) && c.style.stroke_w >= 0
          && Math.abs(c.style.stroke_w - SENTINEL_STROKE_W) > 1e-6
          ? c.style.stroke_w : null
        ctx.lineWidth = styleStrokeW != null
          ? Math.max(1, Math.round((styleStrokeW / edl.canvas.h) * height))
          : Math.max(2, Math.round(s.stroke * height))

        const env = animEnvelope(c, t, height)
        const styleStroke = c.style?.stroke
          && c.style.stroke.toUpperCase() !== SENTINEL_STROKE
          && /^#[0-9a-fA-F]{6}/.test(c.style.stroke)
          ? c.style.stroke.slice(0, 7) : null
        ctx.strokeStyle = styleStroke ?? 'rgba(0,0,0,0.95)'
        ctx.fillStyle = styleColor ?? `rgba(255,255,255,1)`
        // transform.opacity, sampled the same way the SERVER resolves it, so
        // preview predicts export in all three shapes: a scalar and a
        // degenerate 1-keyframe list are baked into the PNG's alpha by
        // resolve_opacity_override/_scalar_or_last, and a real (>=2)
        // keyframe list is animated per-frame by the geq path in CLIP-LOCAL
        // time (`T - clip.start`) — hence `t - c.start` here, matching
        // animEnvelope's own time base. types.ts deliberately omits
        // transform on the mirrored Clip interfaces, hence the cast (same
        // pattern as resolveAnchor above).
        const rawOpacity = (c as TextClip & { transform?: { opacity?: KFNum } }).transform?.opacity
        const txOpacity = Math.min(1, Math.max(0, sampleKF(rawOpacity, t - c.start, 1)))
        ctx.globalAlpha = (s.opacity ?? 1) * env.alpha * txOpacity

        // Emoji are KEPT and drawn as artwork below (see drawLine) — they
        // used to be stripped here and on the server, so typing one into a
        // text clip produced nothing at all.
        const cleaned = c.text.trim()
        if (!cleaned) continue
        // ALL CAPS: the clip's explicit `style.upper` wins, else the role's own
        // default. It used to read ONLY the role table, so a lowercase hook or
        // super was unreachable — "Text layer only shows capital alphabets and
        // doesn't support the small alphabets". Mirrors
        // text_overlay.resolve_upper_override; `null`/absent means untouched, so
        // existing projects keep their capitals.
        const wantUpper = (c.style as { upper?: boolean | null } | undefined)?.upper
        const text = (typeof wantUpper === 'boolean' ? wantUpper : s.upper)
          ? cleaned.toUpperCase() : cleaned
        const maxW = width * 0.86
        const emojiBox = fontPx * EMOJI_BOX_RATIO
        const lines = wrap(ctx, text, maxW, emojiBox)
        const lineH = fontPx * 1.15
        const totalH = lineH * lines.length

        // Anchor overrides (transform.x / transform.y, EDL-canvas px):
        // non-sentinel values place the text block's center absolutely,
        // scaled by the on-screen-box / canvas ratio — the same
        // canvas→output scale the server applies. Sentinels keep the
        // browser's own historic role layout below.
        const { ax, ay } = resolveAnchor(c, role, edl.canvas.w, edl.canvas.h)
        let anchorX = ax != null ? (ax / edl.canvas.w) * width : width / 2

        let cy: number
        if (ay != null) cy = (ay / edl.canvas.h) * height
        else if (s.align === 'top') cy = height * 0.06 + totalH / 2
        else if (s.align === 'center') cy = height / 2
        else if (s.align === 'lower') cy = height * 0.78
        else if (s.align === 'bottom') cy = height - totalH / 2 - height * 0.10
        else cy = height * 0.78
        cy += env.dy

        // Live drag offset from <StickerLayer>. Applied AFTER the anchor
        // resolution so a role-positioned clip (no explicit x/y yet) still
        // follows the pointer — the commit on pointer-up is what makes it
        // explicit.
        if (drag?.id === c.id) { anchorX += drag.dx; cy += drag.dy }

        // Publish the measured box for the interaction layer. Captions are
        // excluded: their position is owned by the captions block server-side
        // (resolveAnchor returns nulls for them), so a drag would commit an
        // x/y the renderer ignores — a control that does nothing is worse than
        // no control. Same for a keyframed/motion-tracked clip, whose position
        // is a curve a single drag can't express.
        const kfPositioned = typeof (c as TextClip & { transform?: { x?: KFNum } })
          .transform?.x === 'object'
        if (role !== 'caption' && !kfPositioned) {
          boxes.push({
            id: c.id, kind: 'text',
            cx: anchorX, cy,
            // Measured from the wrapped lines, so the box hugs the real glyphs
            // rather than a guessed rectangle.
            hw: Math.max(12, lines.reduce((m, l) => Math.max(m, lineWidth(ctx, l, emojiBox)), 0) / 2 + fontPx * 0.15),
            hh: Math.max(10, totalH / 2 + fontPx * 0.12),
            rot: 0,
            x: (anchorX / width) * edl.canvas.w,
            y: (cy / height) * edl.canvas.h,
            sizeCanvasPx,
            xSentinels: xSentinelsFor(edl.canvas.w),
            ySentinels: ySentinelsFor(role, edl.canvas.h),
          })
        }

        ctx.save()
        // pop: scale around the text's own anchor, like the server's
        // overlay-position compensation does.
        if (env.scale !== 1) {
          ctx.translate(anchorX, cy)
          ctx.scale(env.scale, env.scale)
          ctx.translate(-anchorX, -cy)
        }
        // shadow
        ctx.save()
        ctx.shadowColor = 'rgba(0,0,0,0.5)'
        ctx.shadowBlur = Math.max(4, fontPx * 0.06)
        ctx.shadowOffsetY = Math.max(2, fontPx * 0.03)
        for (let i = 0; i < lines.length; i++) {
          const ly = cy - totalH / 2 + lineH / 2 + i * lineH
          drawLine(ctx, lines[i], anchorX, ly, emojiBox, 'stroke')
        }
        ctx.restore()
        for (let i = 0; i < lines.length; i++) {
          const ly = cy - totalH / 2 + lineH / 2 + i * lineH
          drawLine(ctx, lines[i], anchorX, ly, emojiBox, 'fill')
        }
        ctx.restore()
        ctx.globalAlpha = 1
      }

      publishTextBoxes(boxes)
      raf = requestAnimationFrame(draw)
    }
    draw()
    return () => {
      cancelAnimationFrame(raf)
      publishTextBoxes([])
    }
    // fontsReady is included so the effect re-runs (resetting `lastTime`,
    // which forces an immediate redraw) once the real bundled fonts finish
    // loading — otherwise a frame already drawn with the system-font
    // fallback would linger until the next playhead move.
  }, [edl, videoEl, width, height, fontsReady])

  return (
    <canvas
      ref={canvasRef}
      style={{ position: 'absolute', inset: 0, pointerEvents: 'none' }}
    />
  )
}
