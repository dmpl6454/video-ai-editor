// Timeline ruler tick labels.
//
// The ruler's tick step shrinks below a second once zoomed in (0.5, 0.2 or
// 0.1 s), but the old label printed whole seconds from 10 s on ("58s") and
// whole seconds after a minute ("6:15"), so every tick of a second read the
// same: "58s 58s 58s", "6:15 6:15 6:15 6:15 6:16". It never showed before
// QA-024 because the ruler went blank at those zooms. The label now carries
// as many decimals as the step needs; at a step of a second or more it is
// unchanged ("1.0s" … "9.0s", "12s", "1:05").

/** Label for the ruler tick at `t` seconds, on a grid of `step` seconds. */
export function rulerLabel(t: number, step: number): string {
  if (step >= 1) {
    if (t < 60) return `${t.toFixed(t < 10 ? 1 : 0)}s`
    const m = Math.floor(t / 60)
    const s = Math.floor(t % 60)
    return `${m}:${s.toString().padStart(2, '0')}`
  }
  const dp = step < 0.1 - 1e-9 ? 2 : 1
  if (t < 60) return `${t.toFixed(dp)}s`
  const unit = 10 ** dp
  const total = Math.round(t * unit)          // integer tenths (or hundredths)
  const m = Math.floor(total / (60 * unit))
  const rest = total - m * 60 * unit
  const s = Math.floor(rest / unit)
  const frac = (rest - s * unit).toString().padStart(dp, '0')
  return `${m}:${s.toString().padStart(2, '0')}.${frac}`
}
