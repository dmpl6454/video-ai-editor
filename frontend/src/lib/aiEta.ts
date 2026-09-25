// "About 2 min left" for a running AI tool (QA-066). The heavy tools now report
// real progress (frames done / frames total), so the time left is this run's
// own rate extrapolated — never a hardcoded speed, which would be off by 20×
// between a 360p clip and a 4K one. Silent until there is enough of the run to
// extrapolate from, so it never flashes a wild first guess.

const MIN_PROGRESS = 0.05
const MIN_ELAPSED_S = 3

export function etaText(elapsedS: number, progress: number): string {
  if (!(progress >= MIN_PROGRESS) || progress >= 1 || !(elapsedS >= MIN_ELAPSED_S)) return ''
  const left = (elapsedS * (1 - progress)) / progress
  if (!Number.isFinite(left) || left < 0) return ''
  if (left < 10) return 'a few seconds left'
  if (left < 60) return `about ${Math.round(left / 5) * 5} s left`
  const min = Math.round(left / 60)
  return min >= 60 ? `about ${Math.round(left / 360) / 10} h left` : `about ${min} min left`
}
