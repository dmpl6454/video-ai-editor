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
        // s.size is a fixed px value against the EDL canvas (matches the
        // server's ROLE_STYLES); rescale it to the on-screen preview box.
        const fontPx = Math.round((s.size / edl.canvas.h) * height)
        ctx.font = `${s.weight ?? 'bold'} ${fontPx}px "${s.font}", system-ui, sans-serif`
        ctx.textBaseline = 'middle'
        ctx.textAlign = 'center'
        ctx.lineJoin = 'round'
        ctx.lineWidth = Math.max(2, Math.round(s.stroke * height))
        ctx.strokeStyle = 'rgba(0,0,0,0.95)'
        ctx.fillStyle = `rgba(255,255,255,${s.opacity ?? 1})`

        const cleaned = c.text.replace(EMOJI_RE, '').trim()
        if (!cleaned) continue
        const text = s.upper ? cleaned.toUpperCase() : cleaned
        const maxW = width * 0.86
        const lines = wrap(ctx, text, maxW)
        const lineH = fontPx * 1.15
        const totalH = lineH * lines.length

        let cy: number
        if (s.align === 'top') cy = height * 0.06 + totalH / 2
        else if (s.align === 'center') cy = height / 2
        else if (s.align === 'lower') cy = height * 0.78
        else if (s.align === 'bottom') cy = height - totalH / 2 - height * 0.10
        else cy = height * 0.78

        // shadow
        ctx.save()
        ctx.shadowColor = 'rgba(0,0,0,0.5)'
        ctx.shadowBlur = Math.max(4, fontPx * 0.06)
        ctx.shadowOffsetY = Math.max(2, fontPx * 0.03)
        for (let i = 0; i < lines.length; i++) {
          const ly = cy - totalH / 2 + lineH / 2 + i * lineH
          ctx.strokeText(lines[i], width / 2, ly)
        }
        ctx.restore()
        for (let i = 0; i < lines.length; i++) {
          const ly = cy - totalH / 2 + lineH / 2 + i * lineH
          ctx.fillText(lines[i], width / 2, ly)
        }
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
