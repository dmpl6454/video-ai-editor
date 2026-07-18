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
  style?: { font?: string; size?: number; color?: string; stroke?: string; stroke_w?: number }
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
}

export function isMediaClip(c: AnyClip): c is Clip {
  return 'src' in c && 'out' in c
}

export function isTextClip(c: AnyClip): c is TextClip {
  return 'text' in c && 'end' in c
}

export function clipDuration(c: AnyClip): number {
  if (isMediaClip(c)) return c.out - c.in
  return c.end - c.start
}

export function clipEnd(c: AnyClip): number {
  if (isMediaClip(c)) return c.start + (c.out - c.in)
  return c.end
}
