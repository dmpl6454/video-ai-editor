// Gain readouts (QA-088). "-12 dB" was written with an ASCII hyphen-minus,
// which is a line-break opportunity: in the Media > Music card the '-' stayed
// at the end of the slider row and "12 dB" wrapped under "Vol" — a 24 dB
// misread. A real minus sign (U+2212) is not a break opportunity, the number
// and unit are joined by a no-break space, and a half-dB step is shown rather
// than rounded away (the slider moves in 0.5 dB).

const MINUS = '\u2212'
const NBSP = '\u00a0'

export function formatDb(v: number): string {
  if (!Number.isFinite(v)) return `${MINUS}\u221e${NBSP}dB`
  const r = Math.round(v * 10) / 10
  const body = Number.isInteger(r) ? Math.abs(r).toFixed(0) : Math.abs(r).toFixed(1)
  const sign = r < 0 ? MINUS : r > 0 ? '+' : ''
  return `${sign}${body}${NBSP}dB`
}
