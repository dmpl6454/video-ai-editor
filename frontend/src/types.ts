// EDL types — match backend pydantic schema (camelCase fields preserved as snake_case to mirror Python)

export interface Canvas {
  w: number
  h: number
  fps: number
  bg: string
}

export interface Clip {
  id: string
  src: string
  in: number
  out: number
  start: number
  // M1 frontend ignores transform/effects/etc.
}

export interface TextClip {
  id: string
  text: string
  start: number
  end: number
  role?: string
  // Per-clip style overrides; backend defaults ('#FFFFFF' / 'Inter-Black')
  // mean "use the role style" — TextLayer mirrors that sentinel rule.
  // `upper` is TRI-STATE, not a plain boolean: null/absent means "use the role's
  // own default" (super and hook are capitalised as a house style), so existing
  // projects keep their capitals and only an explicit false lowercases one.
  style?: { font?: string; size?: number; color?: string; stroke?: string
            stroke_w?: number; upper?: boolean | null }
  anim_in?: string | null
  anim_out?: string | null
  speaker?: string | null
}

export type AnyClip = Clip | TextClip

export interface Track {
  id: string
  type: string
  z: number
  label?: string
  clips: AnyClip[]
  muted?: boolean
}

export interface Marker {
  id: string
  time: number
  label: string
  color?: string
}

export interface EDL {
  version: number
  duration: number
  canvas: Canvas
  tracks: Track[]
  markers?: Marker[]
}

export interface Op {
  seq: number
  ts: number
  tool: string
  args: Record<string, unknown>
  summary: string
  edl_hash_before: string
  edl_hash_after: string
  by: string
}

export interface SessionInfo {
  id: string
  name: string
  summary: {
    duration: number
    canvas: Canvas
    tracks: { id: string; type: string; label?: string; clips: number }[]
    edl_hash: string
    ops: number
  }
  ops: Op[]
  // Mirrors the on-disk redo_stack.json (main.py:286). Optional so a response
  // from an older backend still typechecks; store.ts coerces it to a boolean.
  redo_available?: boolean
}

export function isMediaClip(c: AnyClip): c is Clip {
  return 'src' in c && 'out' in c
}

export function isTextClip(c: AnyClip): c is TextClip {
  return 'text' in c && 'end' in c
}

/** Scalar playback speed of a media clip (1 for unset/curve dicts) —
 * mirrors backend Clip.speed_factor. `speed` isn't declared on the frontend
 * Clip interface (M1 mirror), so read it via a cast like Properties does. */
export function clipSpeedFactor(c: AnyClip): number {
  const sp = (c as unknown as { speed?: number | object | null }).speed
  return typeof sp === 'number' && sp > 0 ? sp : 1
}

/** TIMELINE seconds a clip occupies — (out-in)/speed for media, mirroring
 * backend Clip.effective_duration. Using raw out-in drew a 2x clip at its
 * source length, overlapping the neighbours the backend had rippled left. */
export function clipDuration(c: AnyClip): number {
  if (isMediaClip(c)) return (c.out - c.in) / clipSpeedFactor(c)
  return c.end - c.start
}

export function clipEnd(c: AnyClip): number {
  if (isMediaClip(c)) return c.start + clipDuration(c)
  return c.end
}
