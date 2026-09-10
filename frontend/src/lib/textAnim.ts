/**
 * Animation envelope for a text clip's anim_in / anim_out presets — the same
 * curves the server bakes (render/text_overlay.py): d = min(0.35, 40% of the
 * clip's on-screen length, ≥ 0.1), pop-in overshoots 0.6→1.06→1.0, pop-out
 * shrinks to 0.6, slides travel 4% of the preview height, fades ramp alpha
 * linearly.
 *
 * Runs on the RENDER window `win` (= `timelineLayout.renderWindow(seams,
 * c.start, c.end)`), with `t` the <video>'s render-time clock — exactly the
 * server's `(t − rs) / d`, `(t − (re − d)) / d` and `d` clamped against
 * `(re − rs)`. It used the layout `c.start`/`c.end` against the render-time
 * `t` (TextLayer converted activity to the render clock but not the clip's
 * own clock): after three 0.5 s dissolves a pop-in hook sat frozen at scale
 * 0.6 for 1.5 s and then popped 1.5 s late, and its fade-out never reached
 * the window's end — the export (correct) and the preview disagreed for the
 * whole clip. A lib module rather than an export from TextLayer.tsx so it
 * can be tested in node and Fast Refresh keeps working on the component.
 */
export interface AnimEnvelope {
  alpha: number
  scale: number
  dy: number
}

export function animEnvelope(
  c: { anim_in?: string | null; anim_out?: string | null },
  t: number, height: number, win: { start: number; end: number },
): AnimEnvelope {
  const d = Math.min(0.35, Math.max(0.1, (win.end - win.start) * 0.4))
  const off = height * 0.04
  const qIn = Math.min(1, Math.max(0, (t - win.start) / d))
  const qOut = Math.min(1, Math.max(0, (t - (win.end - d)) / d))
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
