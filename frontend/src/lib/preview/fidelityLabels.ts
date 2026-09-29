// Human words for the reasons support.ts gives an APPROX range (review RE):
// what the preview's "≈" chip says when the frame on screen is an
// approximation of the export — "Spin In animation", "Voice effect: Deep",
// "Canvas behind a rotated clip". Unknown codes fall back to the code (a
// test keeps every code support.ts emits out of that fallback).
import { ANIM_TABLE } from '../anim/clipAnim'
import { blendLabel } from '../canvasBlend/catalog'
import { voicePreset } from '../voice/voiceFx'

const title = (s: string) => s.replace(/[_-]+/g, ' ').replace(/\b\w/g, (m) => m.toUpperCase())

function animLabel(kind: 'in' | 'out', id: string): string {
  const p = ANIM_TABLE[kind].find((q) => q.id === id)
  return `${p?.label ?? title(id)} ${kind === 'in' ? 'In' : 'Out'} animation`
}

/** Codes without a colon (support.ts clipFeatures). */
const PLAIN: Record<string, string> = {
  chromakey: 'Chroma key', mask: 'Mask', matte: 'Background removal', 'motion-track': 'Motion tracking',
  transition: 'Transition',
}

/** One reason code → the words for it. Every code support.ts can emit has
 *  words (fidelityLabels.test.ts reads them from its source): the raw code
 *  on screen was a Final QA r3 finding ("canvas:blur:moved"). */
export function approxLabel(reason: string): string {
  const [a, b, c] = reason.split(':')
  if (b === undefined && PLAIN[a]) return PLAIN[a]
  if (a === 'anim' && (b === 'in' || b === 'out') && c) return animLabel(b, c)
  if (a === 'anim' && b === 'blur') return 'Blur animation'
  if (a === 'canvas' && c === 'rotated') return 'Canvas behind a rotated clip'
  if (a === 'canvas' && c === 'moved') return 'Canvas behind a moved or resized clip'
  if (a === 'canvas' && c === 'pending') return 'Canvas picture loading'
  if (a === 'canvas' && b && !c) return 'Canvas background'
  if (a === 'audio' && b === 'voice' && c) return `Voice effect: ${voicePreset(c)?.label ?? title(c)}`
  if (a === 'audio' && b === 'tempo') return 'Speed with pitch kept (sound)'
  if (a === 'audio' && b === 'varispeed') return 'Speed change without Keep pitch (sound)'
  if (a === 'audio' && b === 'curve') return 'Speed curve (sound)'
  if (a === 'audio' && b === 'reverse-speed') return 'Reversed, sped-up sound'
  if (a === 'audio' && b === 'reverse') return 'Reversed sound'
  if (a === 'audio' && b === 'duck') return 'Music ducking'
  if (a === 'audio' && b === 'loudness') return 'Loudness'
  if (a === 'audio' && b === 'limiting') return 'Limiter on loud sound'
  if (a === 'audio' && b === 'pending') return 'Sound loading'
  if (a === 'transition' && b) return `${title(b)} transition`
  if (a === 'effect' && b) return `${title(b)} effect`
  if (a === 'blend' && b) return `Blend: ${blendLabel(b)}`
  if (a === 'proxy' && b === 'pending') return 'Preview media loading'
  if (a === 'proxy' && b === 'degraded') return 'Preview media unavailable'
  if (a === 'structure') return 'Drawn by the server'
  return reason
}

/** The chip's accessible name for a set of reasons (deduplicated, in order). */
export function approxSummary(reasons: readonly string[]): string {
  const words = [...new Set(reasons.map(approxLabel))]
  return `Approximate preview: ${words.join(', ')}. The export is exact.`
}
