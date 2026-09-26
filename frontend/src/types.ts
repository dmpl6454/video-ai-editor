// EDL types — match backend pydantic schema (camelCase fields preserved as snake_case to mirror Python)

import {
  clipSpeedFactor as planSpeedFactor, effectiveDuration as planDuration, type EdlClip,
} from './lib/preview/timeline/framePlan'

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

/** A media clip's FREEZE hold in timeline seconds (`Clip.freeze`), or null. */
export function clipFreeze(c: AnyClip): number | null {
  const f = (c as unknown as { freeze?: number | null }).freeze
  return typeof f === 'number' && f > 0 ? f : null
}

/** MEAN playback speed of a media clip — source seconds per timeline
 * second, backend Clip.speed_factor: the scalar for a constant speed, 1
 * unset, a speed CURVE's mean (wave D; its footprint is `(out-in)/mean`), a
 * FREEZE's `(out-in)/freeze` (1 when it consumes no source). Trim math
 * converts timeline deltas with it, so the server's `trim_clip` reads a
 * freeze's edge drag back as a hold. ONE implementation (review RD2): the
 * golden-pinned framePlan port; this only guards a non-media clip and a
 * degenerate value. */
export function clipSpeedFactor(c: AnyClip): number {
  if (!isMediaClip(c)) return 1
  const f = planSpeedFactor(c as unknown as EdlClip)
  return Number.isFinite(f) && f > 0 ? f : 1
}

/** TIMELINE seconds a clip occupies — backend Clip.effective_duration via
 * framePlan (a constant speed's `(out-in)/speed`, a curve's integral, a
 * freeze's hold). Using raw out-in drew a 2x clip at its source length,
 * overlapping the neighbours the backend had rippled left. */
export function clipDuration(c: AnyClip): number {
  if (isMediaClip(c)) {
    const d = planDuration(c as unknown as EdlClip)
    return Number.isFinite(d) ? d : c.out - c.in
  }
  return c.end - c.start
}

/** The Normal speed slider's range (Properties). */
export const NORMAL_SPEED_RANGE: readonly [number, number] = [0.25, 4]

/** Where the Inspector's Normal speed slider starts: the constant speed, or
 * a curve clip's MEAN speed — so the first nudge replaces the curve with a
 * constant that keeps the clip's length near the curve's (review RD2: it
 * started at 1.00x whatever the curve). */
export function normalSpeedOf(c: AnyClip): number {
  const sp = (c as unknown as { speed?: unknown }).speed
  const v = typeof sp === 'number' ? (sp > 0 ? sp : 1) : clipFreeze(c) !== null ? 1 : clipSpeedFactor(c)
  return Math.min(NORMAL_SPEED_RANGE[1], Math.max(NORMAL_SPEED_RANGE[0], v))
}

export function clipEnd(c: AnyClip): number {
  if (isMediaClip(c)) return c.start + clipDuration(c)
  return c.end
}
