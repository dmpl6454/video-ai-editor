// What an AI tool form's choices SAY (QA-101, wave C review). Selects printed
// the backend's enum values and lane ids — Track "v1 | v2", caption style
// "default | ig_chunky | word_emphasis", languages "hi | en | hinglish | es",
// model "large-v3-turbo" — and the clip picker "v1 · name @ 1.0s", a lane id
// plus decimal seconds where the rest of the app speaks lane names and SMPTE
// timecode. The raw value stays the <option value>; only the label changes.

import { formatTimecode } from './timecode'
import { laneName } from './timelineLanes'
import type { Track } from '../types'

/** The standard lanes' names when the timeline is not at hand (edl/schema.py empty_edl). */
const DEFAULT_LANES: Record<string, string> = { v1: 'Main video', v2: 'PIP / overlay video' }
const STYLE_LABELS: Record<string, string> = {
  default: 'Default', ig_chunky: 'Chunky (Instagram)', word_emphasis: 'Word emphasis',
}
const MODEL_LABELS: Record<string, string> = { 'large-v3-turbo': 'Fast (Turbo)', 'large-v3': 'Most accurate' }
const LANGUAGE_FIELD_RE = /^(?:target|(?:\w+_)?lang|(?:\w+_)?language)$/
const LANGUAGE_EXTRA: Record<string, string> = { hinglish: 'Hinglish (Hindi in Latin letters)' }

let displayNames: Intl.DisplayNames | null | undefined
function languageName(code: string): string {
  const extra = LANGUAGE_EXTRA[code.toLowerCase()]
  if (extra) return extra
  if (displayNames === undefined) {
    try { displayNames = new Intl.DisplayNames(['en'], { type: 'language' }) } catch { displayNames = null }
  }
  try {
    const n = displayNames?.of(code)
    if (n && n.toLowerCase() !== code.toLowerCase()) return n
  } catch { /* not a language code */ }
  return code
}

/** The label for one option of an AI form field. */
export function aiOptionLabel(field: string, value: string | number, tracks?: readonly Track[] | null): string {
  const v = String(value)
  if (field === 'track') {
    const t = tracks?.find((x) => x.id === v)
    return t ? laneName(t) : DEFAULT_LANES[v] ?? v
  }
  if (field === 'style') return STYLE_LABELS[v] ?? v
  if (field === 'model') return MODEL_LABELS[v] ?? v
  if (LANGUAGE_FIELD_RE.test(field)) return languageName(v)
  if (field === 'factor') return `${v}×`
  // Any other snake_case enum value ("bottom", "center", "slide_up"): words.
  if (/^[a-z][a-z0-9_]*$/.test(v)) {
    const w = v.replace(/_/g, ' ')
    return w.charAt(0).toUpperCase() + w.slice(1)
  }
  return v
}

/** A clip picker row: "Main video · Beach.mov · 00:00:01:00". */
export function aiClipLabel(track: Track, name: string, start: number, fps: unknown): string {
  return `${laneName(track)} · ${name} · ${formatTimecode(start, fps)}`
}
