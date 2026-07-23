// Browser-side text overlay layer. Draws all text/captions clips on a
// transparent <canvas> stacked over the <video>. Updates per frame via
// requestAnimationFrame, sampling the current playhead so overlays appear/
// disappear in real time. This avoids a server round-trip for every text
// edit — only video-track changes trigger an ffmpeg re-render.

import { useEffect, useRef, useState } from 'react'
import type { EDL, TextClip } from '../types'

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

const EMOJI_RE = /[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}\u{1F1E6}-\u{1F1FF}]/gu

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
    ax: pick(tx.x, [SENTINEL_X, canvasW / 2]),
    ay: pick(tx.y, [SENTINEL_Y, canvasH * 0.85, serverAnchorY(role, canvasH)]),
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

function wrap(ctx: CanvasRenderingContext2D, text: string, maxW: number): string[] {
  const out: string[] = []
  for (const para of text.split('\n')) {
    const words = para.split(/\s+/).filter(Boolean)
    if (!words.length) { out.push(''); continue }
    let cur = words[0]
    for (let i = 1; i < words.length; i++) {
      const trial = `${cur} ${words[i]}`
      if (ctx.measureText(trial).width <= maxW) cur = trial
      else { out.push(cur); cur = words[i] }
    }
    out.push(cur)
  }
  return out
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

    // If there's neither text nor stickers anywhere, skip RAF entirely.
    // Stickers are drawn + manipulated by <StickerLayer>; this layer is text only.
    const hasAnyText = edl.tracks.some((tk) =>
      (tk.type === 'text' || tk.type === 'captions') &&
      tk.clips.some((c) => isText(c))
    )
    if (!hasAnyText) {
      ctx.clearRect(0, 0, width, height)
      return
    }

    let raf = 0
    let lastTime = -1
    const draw = () => {
      const t = videoEl ? videoEl.currentTime : 0
      // Only redraw when the playhead actually advanced (or first frame).
      if (Math.abs(t - lastTime) < 1 / 60 && lastTime >= 0) {
        raf = requestAnimationFrame(draw)
        return
      }
      lastTime = t
      ctx.clearRect(0, 0, width, height)

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
        const fontPx = Math.round((sizeCanvasPx / edl.canvas.h) * height)
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
        ctx.globalAlpha = (s.opacity ?? 1) * env.alpha

        const cleaned = c.text.replace(EMOJI_RE, '').trim()
        if (!cleaned) continue
        const text = s.upper ? cleaned.toUpperCase() : cleaned
        const maxW = width * 0.86
        const lines = wrap(ctx, text, maxW)
        const lineH = fontPx * 1.15
        const totalH = lineH * lines.length

        // Anchor overrides (transform.x / transform.y, EDL-canvas px):
        // non-sentinel values place the text block's center absolutely,
        // scaled by the on-screen-box / canvas ratio — the same
        // canvas→output scale the server applies. Sentinels keep the
        // browser's own historic role layout below.
        const { ax, ay } = resolveAnchor(c, role, edl.canvas.w, edl.canvas.h)
        const anchorX = ax != null ? (ax / edl.canvas.w) * width : width / 2

        let cy: number
        if (ay != null) cy = (ay / edl.canvas.h) * height
        else if (s.align === 'top') cy = height * 0.06 + totalH / 2
        else if (s.align === 'center') cy = height / 2
        else if (s.align === 'lower') cy = height * 0.78
        else if (s.align === 'bottom') cy = height - totalH / 2 - height * 0.10
        else cy = height * 0.78
        cy += env.dy

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
          ctx.strokeText(lines[i], anchorX, ly)
        }
        ctx.restore()
        for (let i = 0; i < lines.length; i++) {
          const ly = cy - totalH / 2 + lineH / 2 + i * lineH
          ctx.fillText(lines[i], anchorX, ly)
        }
        ctx.restore()
        ctx.globalAlpha = 1
      }

      raf = requestAnimationFrame(draw)
    }
    draw()
    return () => cancelAnimationFrame(raf)
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
