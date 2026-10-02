// Copy / Paste attributes (design §3; brief §6): a media clip's transform,
// grade, LUT, speed, animation, audio level and fades, carried to another
// clip through the same tools the inspector uses — never a raw EDL splice.
// Held in memory for the session (a clipboard of attributes, not of clips).
import type { AnyClip } from '../types'

export interface ClipAttributes {
  transform?: { x?: unknown; y?: unknown; scale?: unknown; rotation?: unknown; opacity?: unknown }
  color?: Record<string, number>
  lut?: { src: string; intensity: number }
  speed?: number
  anim?: { in?: string | null; out?: string | null; combo?: string | null; dur?: number | null }
  audio?: { gain_db?: number; fade_in?: number; fade_out?: number }
}

let held: ClipAttributes | null = null

export function readAttributes(): ClipAttributes | null { return held }

export function attributesOf(clip: AnyClip): ClipAttributes {
  const c = clip as unknown as Record<string, unknown>
  const tx = c.transform as ClipAttributes['transform'] | undefined
  const effects = (c.effects as { type: string; params?: Record<string, unknown> }[] | undefined) ?? []
  const color = effects.find((e) => e.type === 'color' || e.type === 'color_grade')?.params as Record<string, number> | undefined
  const lut = effects.find((e) => e.type === 'lut')?.params as { src?: string; intensity?: number } | undefined
  const audio = c.audio as { gain_db?: number; fade_in?: number; fade_out?: number } | undefined
  const scalar = (v: unknown) => (typeof v === 'number' ? v : undefined)
  return {
    transform: tx ? { x: scalar(tx.x), y: scalar(tx.y), scale: scalar(tx.scale), rotation: scalar(tx.rotation), opacity: scalar(tx.opacity) } : undefined,
    color,
    lut: lut?.src ? { src: lut.src, intensity: lut.intensity ?? 1 } : undefined,
    speed: typeof c.speed === 'number' ? c.speed : undefined,
    anim: { in: c.anim_in as string | null, out: c.anim_out as string | null, combo: c.anim_combo as string | null, dur: c.anim_dur as number | null },
    audio: audio ? { gain_db: audio.gain_db, fade_in: audio.fade_in, fade_out: audio.fade_out } : undefined,
  }
}

export function writeAttributes(clip: AnyClip): void { held = attributesOf(clip) }

type Dispatch = (tool: string, args: Record<string, unknown>) => Promise<unknown>

/** The dispatches that apply `held` to `clipId`, in order. */
export function pasteCommands(clipId: string, a: ClipAttributes): { tool: string; args: Record<string, unknown> }[] {
  const out: { tool: string; args: Record<string, unknown> }[] = []
  const tx = a.transform
  if (tx && Object.values(tx).some((v) => v !== undefined)) {
    const args: Record<string, unknown> = { clip_id: clipId }
    for (const k of ['x', 'y', 'scale', 'rotation', 'opacity'] as const) if (tx[k] !== undefined) args[k] = tx[k]
    out.push({ tool: 'set_clip_transform', args })
  }
  if (a.color && Object.keys(a.color).length) out.push({ tool: 'color_grade', args: { clip_id: clipId, ...a.color } })
  if (a.lut) out.push({ tool: 'apply_lut', args: { clip_id: clipId, src: a.lut.src, intensity: a.lut.intensity } })
  if (a.speed && a.speed !== 1) out.push({ tool: 'set_speed', args: { clip_id: clipId, factor: a.speed } })
  if (a.anim && (a.anim.in || a.anim.out || a.anim.combo)) {
    out.push({ tool: 'set_animation', args: { clip_id: clipId, in: a.anim.in ?? 'none', out: a.anim.out ?? 'none', combo: a.anim.combo ?? 'none', ...(a.anim.dur ? { duration: a.anim.dur } : {}) } })
  }
  if (a.audio) {
    if (typeof a.audio.gain_db === 'number') out.push({ tool: 'set_volume', args: { target: clipId, db: a.audio.gain_db } })
    if (typeof a.audio.fade_in === 'number' || typeof a.audio.fade_out === 'number') {
      out.push({ tool: 'add_fade', args: { clip_id: clipId, in_s: a.audio.fade_in ?? 0, out_s: a.audio.fade_out ?? 0 } })
    }
  }
  return out
}

export async function pasteAttributes(clipId: string, dispatch: Dispatch): Promise<void> {
  if (!held) return
  for (const c of pasteCommands(clipId, held)) await dispatch(c.tool, c.args)
}
