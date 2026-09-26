// Browser-side text overlay layer. Draws all text/captions clips on a
// transparent <canvas> stacked over the <video>. Updates per frame via
// requestAnimationFrame, sampling the current playhead so overlays appear/
// disappear in real time. This avoids a server round-trip for every text
// edit — only video-track changes trigger an ffmpeg re-render.

import { useEffect, useRef, useState } from 'react'
import { useStore } from '../store'
import type { EDL, TextClip } from '../types'
import {
  sampleKF, publishTextBoxes, getOverlayDrag,
  type KFNum, type OverlayBox,
} from '../lib/overlay'
import { emojiImage, emojiGeneration } from '../lib/emojiArt'
import { renderWindow, v1SeamsOf } from '../lib/timelineLayout'
import { animEnvelope } from '../lib/textAnim'
import { inEnableWindow } from '../lib/overlayGate'
import {
  EMOJI_BOX_RATIO, EMOJI_INK_RATIO, SCRIPT_FAMILY, SHADOW_ALPHA, SHADOW_OFFSET,
  WRAP_WIDTH_RATIO, baselineFor, fontFamilyList, isAnimated, lineCenters, outlineLineWidth, pickScript,
  rgbaCss, roleAnchorY, roleStyle, scaleRotationAt, staticValue, LINE_HEIGHT_RATIO, type RGBA,
  BG_PAD_X_RATIO, backgroundRect, captionAnchorY, lineX, type TextAlign,
  letterSpacingOf, trackedUnits, trackedWidth,
} from '../lib/textLayout'

interface Props {
  edl: EDL
  videoEl: HTMLVideoElement | null
  /** The client engine's presented-frame clock (spec §3.5); when absent the
   *  time is `videoEl.currentTime` (server mode), else the store playhead. */
  clock?: { now(): number } | null
  // The element rect to draw within (matches the <video> on screen)
  width: number
  height: number
}

// Role styles, anchors, line height, outline and shadow all come from
// lib/textLayout — the client half of the ONE layout model the export uses
// (QA-015; read the long comment in render/text_overlay.py). This layer used
// to carry its own role table (label anchored at the TOP, a height-fraction
// stroke, a blurred shadow), which is how preview text drifted 20-50 px from
// the delivered file.

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
// EMOJI_BOX_RATIO / EMOJI_INK_RATIO live in lib/textLayout (pinned to the
// export's values by the shared contract fixture).
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

function lineWidth(ctx: CanvasRenderingContext2D, line: string, box: number, spacing = 0): number {
  if (spacing && !baseIsRtl(line)) {
    const units = spacedUnits(ctx, line, box)
    return trackedWidth(units.map((u) => u.w), units.map((u) => u.tracked), spacing)
  }
  let w = 0
  for (const seg of tokenize(line)) w += seg.emoji ? box : ctx.measureText(seg.s).width
  return w
}

/** Rule 8 (QA-078): the line as letter-spacing units — every emoji and every
 *  grapheme of a simple-script run is its own tracked unit, measured alone
 *  (the export shapes each alone too); a complex-script run is one untracked
 *  unit. `trackedWidth` then adds the spacing BETWEEN tracked units. */
type SpacedUnit = { emoji: boolean; s: string; w: number; tracked: boolean }
function spacedUnits(ctx: CanvasRenderingContext2D, line: string, box: number): SpacedUnit[] {
  const out: SpacedUnit[] = []
  for (const seg of tokenize(line)) {
    if (seg.emoji) { out.push({ emoji: true, s: seg.s, w: box, tracked: true }); continue }
    for (const [s, tracked] of trackedUnits(seg.s)) out.push({ emoji: false, s, w: ctx.measureText(s).width, tracked })
  }
  return out
}

// Emoji artwork (fetch + cache + the arrival counter) lives in lib/emojiArt.

/** The transform fields TextLayer reads (types.ts omits transform on TextClip). */
type TxAll = { x?: KFNum; y?: KFNum; scale?: KFNum; rotation?: KFNum; opacity?: KFNum }

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
// No font sentinel any more (EDL v3, QA-076): `style.font` is null when unset,
// so every string is a choice — "Inter-Black" included.
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

// Explicit (anchorX, anchorY) in EDL-canvas px, or nulls (role layout).
// Same tolerance (±0.5 canvas px) as the server, absorbing the float noise
// _rescale_overlays_for_canvas_change multiplication introduces.
// The exact values resolveAnchor treats as "unset". Exported through the
// published OverlayBox so the interaction layer can avoid committing a drag
// that would land on one (and silently snap back to the role layout).
// EDL v3 (QA-076): only the schema default of each axis (x 540, y 1700)
// means "role layout" — canvas.w/2, canvas.h·0.85 and the role anchor were
// sentinels in v2, so a typed Y of 918 on 1080p snapped to the anchor.
function xSentinelsFor(): number[] {
  return [SENTINEL_X]
}
function ySentinelsFor(): number[] {
  return [SENTINEL_Y]
}

function resolveAnchor(
  c: TextClip, role: string,
): { ax: number | null; ay: number | null } {
  if (role === 'caption') return { ax: null, ay: null }
  const tx = (c as TextClip & { transform?: { x?: unknown; y?: unknown } }).transform
  if (!tx) return { ax: null, ay: null }
  const pick = (v: unknown, sentinel: number): number | null => {
    // A one-key list is a constant someone set on purpose — honoured (mirror
    // of resolve_anchor_overrides' `_explicit`).
    if (v && typeof v === 'object') return staticValue(v)
    if (typeof v !== 'number' || !Number.isFinite(v)) return null // animated / missing
    return Math.abs(v - sentinel) < 0.5 ? null : v   // this axis never positioned
  }
  return { ax: pick(tx.x, SENTINEL_X), ay: pick(tx.y, SENTINEL_Y) }
}

/** Rule 7 inputs from TextClip.style (QA-078) — text_overlay.resolve_block_overrides. */
function blockStyle(c: TextClip): { background: RGBA | null; align: TextAlign; spacing: number; shadow: boolean | null;
                                    letterSpacing: number } {
  const st = (c.style ?? {}) as { background?: string | null; align?: string; line_spacing?: number; shadow_on?: boolean | null
                                  letter_spacing?: number }
  const align: TextAlign = st.align === 'left' || st.align === 'right' ? st.align : 'center'
  const spacing = typeof st.line_spacing === 'number' && Number.isFinite(st.line_spacing)
    ? Math.min(3, Math.max(0.5, st.line_spacing)) : 1
  return { background: parseHex(st.background ?? null), align, spacing,
           shadow: typeof st.shadow_on === 'boolean' ? st.shadow_on : null,
           letterSpacing: letterSpacingOf(st.letter_spacing) }
}

function roleFontMatches(role: string, ttf: string): boolean {
  const want = cssFont(ttf)
  const rs = roleStyle(role)
  if (!want) return false
  return want.family === rs.font && want.weight === rs.weight
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

// Animation envelope for anim_in/anim_out: lib/textAnim.ts — runs on the
// clip's RENDER window (the same clock `renderWindow` gates activity with).

function wrap(ctx: CanvasRenderingContext2D, text: string, maxW: number,
              box = 0, spacing = 0): string[] {
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
      const w = box || spacing ? lineWidth(ctx, trial, box, spacing) : ctx.measureText(trial).width
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

/** Draw one wrapped line centred on `cx` (local coords). Text runs sit on the
 *  alphabetic `baseline` that centres the 'H' cap band on `center` (rule 3 of
 *  the shared model); emoji squares centre on `center` itself, exactly where
 *  render_text_png pastes them. `paint` picks the pass. */
function drawLine(ctx: CanvasRenderingContext2D, line: string, cx: number, center: number,
                  baseline: number, box: number, paint: 'stroke' | 'fill', spacing = 0): void {
  if (spacing && !baseIsRtl(line)) {
    drawSpacedLine(ctx, line, cx, center, baseline, box, paint, spacing)
    return
  }
  const segs = tokenize(line)
  let x = cx - lineWidth(ctx, line, box) / 2
  const prevAlign = ctx.textAlign
  const prevDir = ctx.direction
  ctx.textAlign = 'left'
  // Paragraph direction from the first strong character (UBA rule P2), the
  // same base level python-bidi gives the export — a mixed "مرحبا 2026" puts
  // the number on the LEFT of the word in both.
  ctx.direction = baseIsRtl(line) ? 'rtl' : 'ltr'
  for (const seg of segs) {
    if (seg.emoji) {
      // Only on the fill pass: an emoji is artwork, it takes no outline, and
      // painting it twice would double its opacity.
      if (paint === 'fill') {
        const im = emojiImage(seg.s)
        // Drawn at `ink`, centred in the full `box` advance — see EMOJI_INK_RATIO.
        const ink = box * EMOJI_INK_RATIO
        if (im) ctx.drawImage(im, x + (box - ink) / 2, center - ink / 2, ink, ink)
      }
      x += box
      continue
    }
    if (paint === 'stroke') ctx.strokeText(seg.s, x, baseline)
    else ctx.fillText(seg.s, x, baseline)
    x += ctx.measureText(seg.s).width
  }
  ctx.textAlign = prevAlign
  ctx.direction = prevDir
}

/** drawLine with letter spacing (rule 8): unit by unit, left to right. */
function drawSpacedLine(ctx: CanvasRenderingContext2D, line: string, cx: number, center: number,
                        baseline: number, box: number, paint: 'stroke' | 'fill', spacing: number): void {
  const units = spacedUnits(ctx, line, box)
  let x = cx - trackedWidth(units.map((u) => u.w), units.map((u) => u.tracked), spacing) / 2
  const prevAlign = ctx.textAlign
  const prevDir = ctx.direction
  ctx.textAlign = 'left'
  ctx.direction = 'ltr'
  for (const u of units) {
    if (u.emoji) {
      if (paint === 'fill') {
        const im = emojiImage(u.s)
        const ink = box * EMOJI_INK_RATIO
        if (im) ctx.drawImage(im, x + (box - ink) / 2, center - ink / 2, ink, ink)
      }
    } else if (paint === 'stroke') ctx.strokeText(u.s, x, baseline)
    else ctx.fillText(u.s, x, baseline)
    x += u.w + (u.tracked ? spacing : 0)
  }
  ctx.textAlign = prevAlign
  ctx.direction = prevDir
}

/** First strong character is right-to-left (Hebrew/Arabic blocks)? */
function baseIsRtl(s: string): boolean {
  for (const ch of s) {
    const cp = ch.codePointAt(0) ?? 0
    if ((cp >= 0x0590 && cp <= 0x08FF) || (cp >= 0xFB1D && cp <= 0xFDFF) || (cp >= 0xFE70 && cp <= 0xFEFF)) return true
    if (/\p{L}/u.test(ch)) return false
  }
  return false
}

/** `#RRGGBB[AA]` → RGBA, or null. */
function parseHex(s: string | null | undefined): RGBA | null {
  const v = (s ?? '').trim().replace(/^#/, '')
  const full = v.length === 6 ? v + 'FF' : v
  if (!/^[0-9a-fA-F]{8}$/.test(full)) return null
  return [0, 2, 4, 6].map((i) => parseInt(full.slice(i, i + 2), 16)) as unknown as RGBA
}

function stripEmoji(s: string): string {
  return tokenize(s).filter((t) => !t.emoji).map((t) => t.s).join('').trim()
}

export function TextLayer({ edl, videoEl, clock, width, height }: Props) {
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
      // The export's complex-script faces (QA-003/015), at the weights
      // SCRIPT_FONT_WEIGHT renders them — so the preview measures and draws
      // the same glyphs rather than a system Devanagari/Arabic font.
      '700 32px "Noto Sans Devanagari"',
      '900 32px "Noto Sans Devanagari"',
      '700 32px "Noto Sans Arabic"',
      '900 32px "Noto Sans Arabic"',
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
    // Two device-sized scratch layers: offB holds one clip (composited once at
    // the clip's opacity), offA its hard shadow (composited into offB at
    // SHADOW_ALPHA so overlapping stroke+fill don't double the darkness).
    const mkLayer = () => {
      const el = document.createElement('canvas')
      el.width = cv.width
      el.height = cv.height
      return el.getContext('2d')!
    }
    const offA = mkLayer()
    const offB = mkLayer()
    const clearDevice = (g: CanvasRenderingContext2D) => {
      g.setTransform(1, 0, 0, 1, 0, 0)
      g.clearRect(0, 0, g.canvas.width, g.canvas.height)
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

    // `t` below is the <video>'s clock — RENDER time — while every text clip's
    // `start`/`end` is LAYOUT time. The two differ by the overlap the v1
    // transitions before it consumed (`lib/timelineLayout`), and the export
    // now places each overlay at `render_time(start)`; testing raw layout
    // values against the render clock is exactly the drift the export used
    // to have (captions up to 2.4 s late after a dozen dissolves). Computed
    // once per effect run, not per frame: the seam table only changes with
    // the EDL, which is already in this effect's deps.
    const seams = v1SeamsOf(edl)
    // The captions track's position (QA-075) — where every caption cue sits.
    const capPos = (edl.tracks.find((tk) => tk.id === 'captions') as { config?: { position?: string } | null } | undefined)
      ?.config?.position ?? 'bottom'
    let raf = 0
    let lastTime = -1
    let lastDragId: string | null = null
    let lastEmojiGen = -1
    const draw = () => {
      // With no v1 clip, `videoEl` is null — there is no `<video>` to poll for
      // "what time is it". This used to fall back to a hardcoded 0, which
      // pinned every caption/text clip whose `start <= 0` permanently visible
      // and OVERLAPPING regardless of the actual playhead, because the
      // scrubber has no effect on a `t` that is a constant. Reported as
      // garbled stacked captions over a black frame right after deleting the
      // v1 clip. `StickerLayer.tsx` already gets this right
      // (`videoEl ? videoEl.currentTime : useStore.getState().playhead`) —
      // this is the sibling layer catching up to that fix. `getState()` is an
      // imperative read, not a subscription, so it needs no extra prop and
      // cannot go stale inside this self-perpetuating rAF closure the way a
      // captured prop value would.
      // Client engine: the PRESENTED frame's time (spec §3.5), so text is
      // frame-locked to the picture actually on screen.
      const t = clock ? clock.now() : videoEl ? videoEl.currentTime : useStore.getState().playhead
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
      const active: { c: TextClip; role: string; win: { start: number; end: number } }[] = []
      for (const tk of edl.tracks) {
        if (tk.type !== 'text' && tk.type !== 'captions') continue
        for (const c of tk.clips) {
          if (!isText(c)) continue
          // A window the renderer drops (wholly inside a consumed span) is
          // not drawn here either — the preview must not show a caption the
          // export will never contain.
          const w = renderWindow(seams, c.start, c.end)
          // Half-open on the frame grid (QA-016) — mirror of the export's
          // enable_expr, so a cue change never shows both cues on one frame.
          if (!w.dropped && inEnableWindow(w.start, w.end, t, edl.canvas.fps)) {
            active.push({ c, role: (c as TextClip & { role?: string }).role ?? 'default', win: w })
          }
        }
      }

      // Sort by role priority: watermark drawn first (under), hook last (top)
      const order = ['watermark', 'lower_third', 'caption', 'label', 'super', 'hook']
      active.sort((a, b) => order.indexOf(a.role) - order.indexOf(b.role))

      for (const { c, role, win } of active) {
        const s = roleStyle(role)
        // Per-clip style overrides (non-sentinel values only — see cssFont/
        // roleFontMatches above; mirrors the server's resolve_style_overrides).
        const styleColor = c.style?.color && c.style.color.toUpperCase() !== SENTINEL_COLOR
          ? c.style.color : null
        const styleFont = c.style?.font && !roleFontMatches(role, c.style.font)
          ? cssFont(c.style.font) : null
        // style.size (non-sentinel) is an explicit size in EDL-canvas px —
        // exactly the coordinate system the role size lives in. Mirrors the
        // server's resolve_size_override.
        const sizeCanvasPx = typeof c.style?.size === 'number'
          && Number.isFinite(c.style.size) && c.style.size > 0
          && Math.abs(c.style.size - SENTINEL_SIZE) > 1e-6
          ? c.style.size : s.size
        // A live corner-resize scales the drawn glyphs immediately; the EDL
        // only changes on pointer-up (StickerLayer commits style.size then).
        const sizeMul = drag?.id === c.id ? drag.sizeMul : 1

        // Emoji are KEPT and drawn as artwork (see drawLine) — they used to be
        // stripped here and on the server, so typing one produced nothing.
        const cleaned = c.text.trim()
        if (!cleaned) continue
        // ALL CAPS: the clip's explicit `style.upper` wins, else the role's own
        // default. Mirrors text_overlay.resolve_upper_override; `null`/absent
        // means untouched, so existing projects keep their capitals.
        const wantUpper = (c.style as { upper?: boolean | null } | undefined)?.upper
        const text = (typeof wantUpper === 'boolean' ? wantUpper : s.upper)
          ? cleaned.toUpperCase() : cleaned

        // Transform, in CLIP-LOCAL RENDER time (`t - win.start`) — the clock the
        // export's per-frame expressions run on (`t - rs`). QA-036: scale and
        // rotation were accepted by the tools and drawn by neither renderer,
        // and a keyframed x/y resolved to "no override" (centred, static).
        const tx = (c as TextClip & { transform?: TxAll }).transform
        const localT = t - win.start
        const { scale: k, rotation } = scaleRotationAt(tx, role, localT)
        // Canvas px → display px. The export scales its canvas-px PNG to the
        // output with these same ratios.
        const fy = height / edl.canvas.h
        const fx = width / edl.canvas.w
        // Rule 6: the scale multiplies every length. Rounded to whole canvas
        // px where the server rounds (font em, outline), so the glyphs match.
        const emPx = Math.max(1, Math.round(sizeCanvasPx * sizeMul * k))
        const fontPx = emPx * fy
        const script = pickScript(stripEmoji(text) || text)
        const scriptFamily = script ? SCRIPT_FAMILY[script] : undefined
        const family = scriptFamily ?? styleFont?.family ?? s.font
        const weight = scriptFamily ? String(s.scriptWeight) : (styleFont?.weight ?? s.weight)
        const fontSpec = `${weight} ${fontPx}px ${fontFamilyList(family)}`
        // Custom stroke_w is in canvas px like the server's (rounded there).
        const styleStrokeW = typeof c.style?.stroke_w === 'number'
          && Number.isFinite(c.style.stroke_w) && c.style.stroke_w >= 0
          && Math.abs(c.style.stroke_w - SENTINEL_STROKE_W) > 1e-6
          ? Math.round(c.style.stroke_w) : null
        const strokePx = Math.max(0, Math.round((styleStrokeW ?? s.strokeW) * k)) * fy
        const styleStroke = parseHex(c.style?.stroke && c.style.stroke.toUpperCase() !== SENTINEL_STROKE
          ? c.style.stroke : null)
        const strokeRgba: RGBA = styleStroke ?? s.stroke
        const fillHex = parseHex(styleColor)
        // An explicit colour with default alpha inherits the role's alpha
        // (a coloured watermark stays translucent) — resolve_style_overrides.
        const fillRgba: RGBA = fillHex
          ? [fillHex[0], fillHex[1], fillHex[2], fillHex[3] !== 255 ? fillHex[3] : s.fill[3]]
          : s.fill

        const env = animEnvelope(c, t, height, win)
        // transform.opacity, sampled the same way the SERVER resolves it: a
        // scalar or a 1-key list is baked into the PNG alpha, a real (>=2)
        // keyframe list is animated per frame in clip-local render time.
        const rawOpacity = tx?.opacity
        const txOpacity = Math.min(1, Math.max(0, sampleKF(rawOpacity, localT, 1)))

        ctx.font = fontSpec
        offB.font = fontSpec
        offA.font = fontSpec
        const emojiBox = fontPx * EMOJI_BOX_RATIO
        const maxW = edl.canvas.w * WRAP_WIDTH_RATIO * k * fx
        // Rule 7 (QA-078): alignment inside the block, line spacing, a box;
        // rule 8: letter spacing in canvas px × the transform scale.
        const blk = blockStyle(c)
        const trackPx = blk.letterSpacing * k * fy
        const lines = wrap(offB, text, maxW, emojiBox, trackPx)
        const lineH = fontPx * LINE_HEIGHT_RATIO * blk.spacing
        const totalH = lineH * lines.length
        const widths = lines.map((l) => lineWidth(offB, l, emojiBox, trackPx))
        const blockW = widths.reduce((m, w) => Math.max(m, w), 0)
        // The cap band of THIS font, measured here — the server measures 'H'
        // in Pillow; only the measured band is common ground (rule 3).
        const mH = offB.measureText('H')
        const capAsc = mH.actualBoundingBoxAscent ?? fontPx * 0.7
        const capDesc = mH.actualBoundingBoxDescent ?? 0

        // Anchor: an animated axis follows its curve; otherwise the explicit
        // override (non-sentinel) or the ROLE anchor the export uses.
        const { ax, ay } = resolveAnchor(c, role)
        const noTx = role === 'caption'
        const axC = !noTx && isAnimated(tx?.x) ? sampleKF(tx?.x, localT, edl.canvas.w / 2)
          : (ax ?? edl.canvas.w / 2)
        const ayC = !noTx && isAnimated(tx?.y) ? sampleKF(tx?.y, localT, edl.canvas.h / 2)
          : role === 'caption' ? captionAnchorY(capPos, edl.canvas.w, edl.canvas.h)
          : (ay ?? roleAnchorY(role, edl.canvas.h, edl.canvas.w))
        let anchorX = axC * fx
        let cy = ayC * fy + env.dy
        // Live drag offset from <StickerLayer>. Applied AFTER the anchor
        // resolution so a role-positioned clip (no explicit x/y yet) still
        // follows the pointer — the commit on pointer-up makes it explicit.
        if (drag?.id === c.id) { anchorX += drag.dx; cy += drag.dy }

        // Publish the measured box for the interaction layer. Captions are
        // excluded: their position is owned by the captions block server-side
        // (resolveAnchor returns nulls for them), so a drag would commit an
        // x/y the renderer ignores. Same for an animated x/y, whose position is
        // a curve a single drag can't express.
        const kfPositioned = isAnimated(tx?.x) || isAnimated(tx?.y)
        // A caption is published too, SELECT-ONLY (QA-075): a click on it
        // selects the cue (and so its style section) instead of falling
        // through to the video underneath; it still never drags.
        if (role === 'caption' || !kfPositioned) {
          boxes.push({
            id: c.id, kind: 'text', ...(role === 'caption' ? { selectOnly: true } : {}),
            cx: anchorX, cy,
            // Measured from the wrapped lines, so the box hugs the real glyphs
            // rather than a guessed rectangle.
            hw: Math.max(12, blockW / 2 + fontPx * (blk.background ? BG_PAD_X_RATIO : 0.15)),
            hh: Math.max(10, totalH / 2 + fontPx * 0.12),
            rot: (rotation * Math.PI) / 180,
            x: (anchorX / width) * edl.canvas.w,
            y: (cy / height) * edl.canvas.h,
            sizeCanvasPx,
            xSentinels: xSentinelsFor(),
            ySentinels: ySentinelsFor(),
          })
        }

        // Paint the whole clip into offB at full opacity, then composite it
        // once with the clip's opacity — the export multiplies the FINISHED
        // image's alpha, so stroke, shadow and fill must dim as one layer.
        const centers = lineCenters(0, lines.length, fontPx, blk.spacing)
        const place = (g: CanvasRenderingContext2D, ox: number, oy: number) => {
          g.setTransform(dpr, 0, 0, dpr, 0, 0)
          g.translate(anchorX, cy)
          if (rotation) g.rotate((rotation * Math.PI) / 180)
          // pop: scale around the text's own anchor, like the server's
          // overlay-position compensation does.
          if (env.scale !== 1) g.scale(env.scale, env.scale)
          g.translate(ox, oy)
        }
        const paintText = (g: CanvasRenderingContext2D, paint: 'stroke' | 'fill') => {
          lines.forEach((ln, i) => {
            // drawLine centres a line on its x: the aligned left edge + w/2.
            drawLine(g, ln, lineX(blk.align, 0, blockW, widths[i]) + widths[i] / 2, centers[i],
                     baselineFor(centers[i], capAsc, capDesc), emojiBox, paint, trackPx)
          })
        }
        clearDevice(offB)
        offB.lineJoin = 'round'
        offB.lineCap = 'round'
        if (blk.shadow ?? s.shadow) {
          // Rule 5: a HARD copy of outline + fill, offset SHADOW_OFFSET canvas
          // px (scaled with the block), black at SHADOW_ALPHA — no blur.
          clearDevice(offA)
          place(offA, SHADOW_OFFSET[0] * k * fy, SHADOW_OFFSET[1] * k * fy)
          offA.fillStyle = offA.strokeStyle = '#000'
          offA.lineJoin = 'round'
          offA.lineCap = 'round'
          offA.lineWidth = outlineLineWidth(strokePx)
          if (strokePx > 0) paintText(offA, 'stroke')
          paintText(offA, 'fill')
          offB.setTransform(1, 0, 0, 1, 0, 0)
          offB.globalAlpha = SHADOW_ALPHA / 255
          offB.drawImage(offA.canvas, 0, 0)
          offB.globalAlpha = 1
        }
        place(offB, 0, 0)
        if (strokePx > 0) {
          // Rule 4: a 2 x stroke line centred on the path, under the fill.
          offB.strokeStyle = rgbaCss(strokeRgba)
          offB.lineWidth = outlineLineWidth(strokePx)
          paintText(offB, 'stroke')
        }
        // The fill REPLACES what is under it (Pillow's draw semantics), which
        // only matters for a translucent fill such as the watermark's.
        offB.globalCompositeOperation = 'destination-out'
        offB.fillStyle = '#000'
        paintText(offB, 'fill')
        offB.globalCompositeOperation = 'source-over'
        offB.fillStyle = rgbaCss(fillRgba)
        paintText(offB, 'fill')
        if (blk.background) {
          // The box goes BEHIND the finished text (destination-over) — the
          // export composites its box layer under the text the same way.
          const [l, tp, r, b, rad] = backgroundRect(0, centers, blockW, fontPx, blk.spacing)
          offB.globalCompositeOperation = 'destination-over'
          offB.fillStyle = rgbaCss(blk.background)
          offB.beginPath()
          offB.roundRect(l, tp, r - l, b - tp, rad)
          offB.fill()
          offB.globalCompositeOperation = 'source-over'
        }
        offB.setTransform(1, 0, 0, 1, 0, 0)

        ctx.save()
        ctx.setTransform(1, 0, 0, 1, 0, 0)
        ctx.globalAlpha = env.alpha * txOpacity
        ctx.drawImage(offB.canvas, 0, 0)
        ctx.restore()
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
  }, [edl, videoEl, clock, width, height, fontsReady])

  return (
    <canvas
      ref={canvasRef}
      style={{ position: 'absolute', inset: 0, pointerEvents: 'none' }}
    />
  )
}
