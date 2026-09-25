// Shaping backend failure text before it reaches a toast or the AI panel.
//
// Two sources feed store.dispatch()'s catch: the sync path throws the
// hardening envelope (already a human sentence), but a job that fails is
// recorded by api/jobs.py as `f"{type(e).__name__}: {e}"`, so an async upscale
// that refuses a text clip arrives as "RuntimeError: upscale only supports
// media clips". The class name is noise to an editor and made every async
// failure read like a stack trace — strip it, leave everything else alone.

export function stripExceptionPrefix(msg: string): string {
  return msg.replace(/^\w+(Error|Exception): /, '')
}

// runDispatchJob throws `${tool} was cancelled` when the job ends `cancelled`
// (store.ts). That is the one failure the user asked for, so callers use this
// to show it neutrally instead of as a red error.
export function isCancelMessage(msg: string): boolean {
  return msg.endsWith(' was cancelled')
}

// Bounds refusals (QA-041) arrive in the dispatcher's own terms —
// "move_clip.new_start must be at most 21600, got 100000": a tool id, an
// argument name and raw seconds. The editor reads "Start must be within 6
// hours". Only this one shape is rewritten; anything else passes through.
const ARG_LABELS: Record<string, string> = {
  new_start: 'Start', start: 'Start', end: 'End', in: 'In point', out: 'Out point',
  time: 'Time', at: 'Time', t_start: 'Start', t_end: 'End', duration: 'Duration',
  db: 'Volume', volume_db: 'Volume', to_db: 'Duck depth', factor: 'Speed', size: 'Text size',
  stroke_w: 'Outline width', x: 'X position', y: 'Y position', lufs: 'Loudness target',
}
const TIME_ARGS = new Set(['new_start', 'start', 'end', 'in', 'out', 'time', 'at', 't_start', 't_end', 'duration'])
const NUM = '(-?[\\d.]+(?:e[+-]?\\d+)?)'
const BOUNDS_RE = new RegExp(
  `^[a-z0-9_]+\\.([a-z0-9_]+) must be (?:between ${NUM} and ${NUM}|at most ${NUM}|at least ${NUM}), got .*$`, 'i')
export const TIMELINE_MAX_SECONDS = 6 * 3600

export function editorValidationMessage(msg: string): string {
  const m = BOUNDS_RE.exec(msg.trim())
  if (!m) return msg
  const [, arg, lo, hi, most, least] = m
  const label = ARG_LABELS[arg] ?? arg.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase())
  const top = Number(hi ?? most)
  if (TIME_ARGS.has(arg) && top === TIMELINE_MAX_SECONDS) return `${label} must be within 6 hours`
  if (lo !== undefined) return `${label} must be between ${lo} and ${hi}`
  if (most !== undefined) return `${label} must be at most ${most}`
  return `${label} must be at least ${least}`
}
