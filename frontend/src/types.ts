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
  // Per-clip style overrides. `font` null = the role's own font (EDL v3,
  // QA-076 — "Inter-Black" is no longer an "unset" sentinel); color '#FFFFFF'
  // still means "use the role style" — TextLayer mirrors the rule.
  // `upper` is TRI-STATE, not a plain boolean: null/absent means "use the role's
  // own default" (super and hook are capitalised as a house style), so existing
  // projects keep their capitals and only an explicit false lowercases one.
  // background / align / line_spacing / shadow_on: QA-078 (rule 7 of lib/textLayout).
  style?: { font?: string | null; size?: number; color?: string; stroke?: string
            stroke_w?: number; upper?: boolean | null
            background?: string | null; align?: 'left' | 'center' | 'right'
            line_spacing?: number; shadow_on?: boolean | null; letter_spacing?: number }
  anim_in?: string | null
  anim_out?: string | null
  anim_dur?: number | null
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
  /** While any track is soloed only soloed tracks are heard (QA-086). */
  solo?: boolean
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
  // QA-046: ⌘Z steps the server can still take (snapshot horizon, never the
  // project's own init op). Optional for an older backend.
  undo_depth?: number
}

/** One entry of the project's media library (GET /sessions/:id/media,
 *  media_library.py). Listed whether or not any timeline clip uses it. */
export interface MediaItem {
  id: string
  /** Absolute path — what a timeline clip's `src` holds; drag payload. */
  src: string
  /** The user's name for it (the original filename), not the on-disk one. */
  name: string
  kind: 'video' | 'audio'
  origin: 'upload' | 'audio' | 'voiceover' | 'imported' | 'timeline'
  duration: number | null
  width: number | null
  height: number | null
  added: number
  /** Timeline clips referencing it, and their ids. */
  uses: number
  clip_ids: string[]
  /** QA-095: a timeline clip plays it but the file is not on disk (offline). */
  missing?: boolean
  /** QA-090: a photo — placed at a default length, extendable like any clip. */
  still?: boolean
  /** Wave D (INSTANT_PREVIEW_SPEC §5.2): stream facts from the preview-proxy
   *  probe. Absent until a proxy was asked for (never probed on /media). */
  fps?: { num: number; den: number }
  frames?: number
  pix_fmt?: string
  has_audio?: boolean
  proxy?: MediaProxyState
}

/** A media item's instant-preview proxy (GET /sessions/:id/media `proxy`). */
export interface MediaProxyState {
  key: string | null
  state: 'none' | 'pending' | 'partial' | 'ready' | 'failed' | 'offline'
  w: number | null
  h: number | null
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
