// Browser-side text overlay layer. Draws all text/captions clips on a
// transparent <canvas> stacked over the <video>. Updates per frame via
// requestAnimationFrame, sampling the current playhead so overlays appear/
// disappear in real time. This avoids a server round-trip for every text
// edit — only video-track changes trigger an ffmpeg re-render.

import { useEffect, useRef } from 'react'
import type { EDL, TextClip } from '../types'

interface Props {
  edl: EDL
  videoEl: HTMLVideoElement | null
  // The element rect to draw within (matches the <video> on screen)
  width: number
  height: number
}

const ROLE_STYLES: Record<string, {
  font: string; size: number; weight?: string; stroke: number; upper?: boolean; align: 'top' | 'center' | 'bottom' | 'lower'; opacity?: number;
}> = {
  super:       { font: 'Anton',           size: 0.075, stroke: 0.005, upper: true,  align: 'lower' },
  hook:        { font: 'Bebas Neue',      size: 0.085, stroke: 0.006, upper: true,  align: 'center' },
  lower_third: { font: 'Montserrat',      size: 0.030, stroke: 0.0025, weight: '700', align: 'lower' },
  caption:     { font: 'Inter',           size: 0.034, stroke: 0.004, weight: '900', align: 'bottom' },
  label:       { font: 'Inter',           size: 0.026, stroke: 0.0025, weight: '700', align: 'top' },
  watermark:   { font: 'Inter',           size: 0.018, stroke: 0.0015, weight: '700', align: 'bottom', opacity: 0.7 },
  default:     { font: 'Inter',           size: 0.034, stroke: 0.0025, weight: '700', align: 'lower' },
}

const EMOJI_RE = /[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}\u{1F1E6}-\u{1F1FF}]/gu

function isText(c: unknown): c is TextClip {
  return !!c && typeof c === 'object' && 'text' in (c as object) && 'end' in (c as object)
}

interface KFSpec { keyframes: [number, number][]; interp?: string }
type KFNum = number | KFSpec

interface StickerClip {
  id: string
  src: string
  start: number
  end: number
  label?: string | null
  transform?: { x?: KFNum; y?: KFNum; scale?: KFNum; rotation?: KFNum; opacity?: KFNum }
}
function isSticker(c: unknown): c is StickerClip {
  if (!c || typeof c !== 'object') return false
  const o = c as Record<string, unknown>
  return typeof o.id === 'string' && typeof o.src === 'string' && typeof o.end === 'number'
}

function sampleKF(v: KFNum | undefined, t: number, fallback: number): number {
  if (typeof v === 'number') return v
  if (v && typeof v === 'object' && Array.isArray(v.keyframes) && v.keyframes.length) {
    const pts = [...v.keyframes].sort((a, b) => a[0] - b[0])
    if (t <= pts[0][0]) return pts[0][1]
    if (t >= pts[pts.length - 1][0]) return pts[pts.length - 1][1]
    for (let i = 0; i < pts.length - 1; i++) {
      const [t0, v0] = pts[i]
      const [t1, v1] = pts[i + 1]
      if (t0 <= t && t <= t1) {
        const f = (t - t0) / Math.max(1e-9, t1 - t0)
        const interp = v.interp ?? 'linear'
        let g = f
        if (interp === 'ease-in') g = f * f
        else if (interp === 'ease-out') g = 1 - (1 - f) ** 2
        else if (interp === 'ease-in-out') g = 3 * f * f - 2 * f * f * f
        else if (interp === 'back-out') g = 1 - (1 - f) ** 3
        else if (interp === 'step') g = 0
        return v0 + (v1 - v0) * g
      }
    }
    return pts[pts.length - 1][1]
  }
  return fallback
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
    const hasAnyText = edl.tracks.some((tk) =>
      (tk.type === 'text' || tk.type === 'captions') &&
      tk.clips.some((c) => isText(c))
    )
    const hasAnyStickers = edl.tracks.some((tk) =>
      tk.type === 'sticker' && tk.clips.length > 0
    )
    if (!hasAnyText && !hasAnyStickers) {
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

      // Active stickers — drawn under the highest text but over watermark
      const stickers: StickerClip[] = []
      for (const tk of edl.tracks) {
        if (tk.type !== 'sticker') continue
        for (const c of tk.clips) {
          if (!isSticker(c)) continue
          if (c.start <= t && t <= c.end) stickers.push(c as StickerClip)
        }
      }

      // Canvas size in EDL coords → display scale factor
      const canvasW = edl.canvas.w
      const canvasH = edl.canvas.h
      const sx = width / canvasW
      const sy = height / canvasH

      // Draw stickers (emoji label preferred, falls back to silhouette dot if none)
      for (const sk of stickers) {
        const tx = sk.transform ?? {}
        const localT = t - sk.start
        const cx = sampleKF(tx.x, localT, canvasW / 2) * sx
        const cy = sampleKF(tx.y, localT, canvasH / 2) * sy
        const scale = sampleKF(tx.scale, localT, 1)
        const rot = sampleKF(tx.rotation, localT, 0) * Math.PI / 180
        const opa = sampleKF(tx.opacity, localT, 1)
        const baseSize = Math.min(canvasW, canvasH) * 0.22 * scale
        const fontPx = Math.max(20, baseSize * Math.min(sx, sy))

        ctx.save()
        ctx.translate(cx, cy)
        ctx.rotate(rot)
        ctx.globalAlpha = opa
        if (sk.label) {
          // Native emoji glyph from system font — Apple Color Emoji on macOS,
          // matches what the picker showed.
          ctx.font = `${fontPx}px "Apple Color Emoji", "Segoe UI Emoji", "Noto Color Emoji", sans-serif`
          ctx.textBaseline = 'middle'
          ctx.textAlign = 'center'
          ctx.fillText(sk.label, 0, 0)
        } else {
          // Generic PNG sticker — silhouette circle as a placeholder.
          ctx.fillStyle = 'rgba(255,255,255,0.6)'
          ctx.beginPath()
          ctx.arc(0, 0, fontPx / 2, 0, Math.PI * 2)
          ctx.fill()
        }
        ctx.restore()
      }

      for (const { c, role } of active) {
        const s = ROLE_STYLES[role] ?? ROLE_STYLES.default
        const fontPx = Math.round(s.size * height)
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
  }, [edl, videoEl, width, height])

  return (
    <canvas
      ref={canvasRef}
      style={{ position: 'absolute', inset: 0, pointerEvents: 'none' }}
    />
  )
}
