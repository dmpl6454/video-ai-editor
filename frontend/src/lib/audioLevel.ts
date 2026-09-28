// Clip audio level + volume automation for the inspector (QA-086).
//
// Mirrors the backend model (edl/schema.py AudioProps): `gain_db` is the
// clip-gain trim and `gain_env` holds volume keyframes as dB OFFSETS from it,
// keyed in clip-local time — the level at t is gain_db + env(t). The UI speaks
// the absolute level; add_keyframe(prop "audio.gain_db") stores the offset.

import { keyEps, sampleKF, type KFSpec } from './overlay'

export const GAIN_KF_PROP = 'audio.gain_db'

/** The Volume slider's range. The render path takes −96…+24 (set_volume's
 *  schema); the slider used to stop at +6, below what quiet dialogue needs. */
export const VOLUME_RANGE = { min: -60, max: 20, step: 0.5 } as const

export interface ClipAudioProps {
  gain_db?: number
  gain_env?: KFSpec | null
  mute?: boolean
  fade_in?: number
  fade_out?: number
  keep_pitch?: boolean
  /** QA-122: 'stereo' | 'left' | 'right' | 'mono' (lib/audioChannels). */
  channels?: string
  /** Wave E (F3): a voice-effect preset id (edl/voice_effects.py) and 0-1. */
  voice_effect?: string | null
  voice_intensity?: number
}

export function hasVolumeKeys(a: ClipAudioProps | undefined): boolean {
  return !!a?.gain_env && Array.isArray(a.gain_env.keyframes) && a.gain_env.keyframes.length > 0
}

/** Absolute level (dB) at clip-local time `t`. */
export function levelAt(a: ClipAudioProps | undefined, t: number): number {
  const base = a?.gain_db ?? 0
  return hasVolumeKeys(a) ? base + sampleKF(a!.gain_env!, t, 0) : base
}

/** Times (clip-local, ascending) of the volume keys. */
export function volumeKeyTimes(a: ClipAudioProps | undefined): number[] {
  if (!hasVolumeKeys(a)) return []
  return a!.gain_env!.keyframes.map((k) => k[0]).sort((x, y) => x - y)
}

/** Is there a volume key within half a frame of `t`? */
export function volumeKeyAt(a: ClipAudioProps | undefined, t: number, fps?: number): boolean {
  const eps = keyEps(fps)
  return volumeKeyTimes(a).some((k) => Math.abs(k - t) < eps)
}

/** The dispatch a Volume slider commit means: a plain clip gain while the clip
 *  has no automation, a key AT the playhead once it has (so dragging never
 *  flattens the curve). */
export function volumeCommit(clipId: string, a: ClipAudioProps | undefined, t: number, level: number)
  : { tool: string; args: Record<string, unknown> } {
  const v = Math.min(VOLUME_RANGE.max, Math.max(VOLUME_RANGE.min, level))
  if (hasVolumeKeys(a)) {
    return { tool: 'add_keyframe', args: { clip_id: clipId, prop: GAIN_KF_PROP, time: t, value: v } }
  }
  return { tool: 'set_volume', args: { target: clipId, db: v } }
}

/** The ◆ Volume key toggle: remove the key at the playhead, or pin the
 *  level the clip has there right now (so adding a key changes nothing). */
export function volumeKeyToggle(clipId: string, a: ClipAudioProps | undefined, t: number, fps?: number)
  : { tool: string; args: Record<string, unknown> } {
  return volumeKeyAt(a, t, fps)
    ? { tool: 'remove_keyframe', args: { clip_id: clipId, prop: GAIN_KF_PROP, time: t } }
    : { tool: 'add_keyframe', args: { clip_id: clipId, prop: GAIN_KF_PROP, time: t, value: levelAt(a, t) } }
}
