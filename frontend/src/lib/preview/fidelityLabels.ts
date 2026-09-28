// Human words for the reasons support.ts gives an APPROX range (review RE):
// what the preview's "≈" chip says when the frame on screen is an
// approximation of the export — "Spin In animation", "Voice effect: Deep",
// "Canvas behind a rotated clip". Unknown codes fall back to the code.
import { ANIM_TABLE } from '../anim/clipAnim'
import { blendLabel } from '../canvasBlend/catalog'
import { voicePreset } from '../voice/voiceFx'

const title = (s: string) => s.replace(/[_-]+/g, ' ').replace(/\b\w/g, (m) => m.toUpperCase())

function animLabel(kind: 'in' | 'out', id: string): string {
  const p = ANIM_TABLE[kind].find((q) => q.id === id)
  return `${p?.label ?? title(id)} ${kind === 'in' ? 'In' : 'Out'} animation`
}

/** One reason code → the words for it. */
export function approxLabel(reason: string): string {
  const [a, b, c] = reason.split(':')
  if (a === 'anim' && (b === 'in' || b === 'out') && c) return animLabel(b, c)
  if (a === 'canvas' && c === 'rotated') return 'Canvas behind a rotated clip'
  if (a === 'audio' && b === 'voice' && c) return `Voice effect: ${voicePreset(c)?.label ?? title(c)}`
  if (a === 'audio' && b === 'tempo') return 'Speed with pitch kept (sound)'
  if (a === 'audio' && b === 'curve') return 'Speed curve (sound)'
  if (a === 'audio' && b === 'reverse-speed') return 'Reversed, sped-up sound'
  if (a === 'audio' && b === 'duck') return 'Music ducking'
  if (a === 'audio' && b === 'loudness') return 'Loudness'
  if (a === 'audio' && b === 'limiting') return 'Limiter on loud sound'
  if (a === 'transition' && b) return `${title(b)} transition`
  if (a === 'effect' && b) return `${title(b)} effect`
  if (a === 'blend' && b) return `Blend: ${blendLabel(b)}`
  return reason
}

/** The chip's accessible name for a set of reasons (deduplicated, in order). */
export function approxSummary(reasons: readonly string[]): string {
  const words = [...new Set(reasons.map(approxLabel))]
  return `Approximate preview: ${words.join(', ')}. The export is exact.`
}
