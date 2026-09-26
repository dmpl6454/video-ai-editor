// Fidelity classes per output range (instant preview spec §7): which frames
// the client draws EXACTly, which APPROXimately, which must be BAKED (server
// frames spliced over the client's), and which are PENDING (proxy spans not
// ready: last good frame held). One capability table per delivery phase
// (§12) decides it; the engine asks `classify` after every committed edit.
//
// Severity is the ladder of §7 — EXACT < APPROX < BAKED < PENDING — and a
// frame takes the most severe mode any of its causes asks for. Every range
// carries the reasons, for the "≈" badge, the spinner and telemetry.

import {
  clipStart, effectiveDuration, freezeOf, v1Transitions, videoClips, isMediaClip, type EdlClip, type EdlLike,
} from './framePlan'
import { isCurve } from './speedCurve'
import { renderTime as layoutRenderTime, seamTable } from '../../timelineLayout'
import { frameOf, type FpsLike } from './timebase'
import { KIND_BLEND, KIND_GAP, type ProgramMap } from './programMap'

export const MODE_EXACT = 0
export const MODE_APPROX = 1
export const MODE_BAKED = 2
export const MODE_PENDING = 3
export type Mode = typeof MODE_EXACT | typeof MODE_APPROX | typeof MODE_BAKED | typeof MODE_PENDING

export const MODE_NAMES = ['EXACT', 'APPROX', 'BAKED', 'PENDING'] as const

export type Phase = 1 | 2 | 3 | 4 | 5

/** What the client can draw/hear, per phase (§12). */
export interface Capabilities {
  /** eq / colorbalance / LUT shaders. */
  colour: Mode
  /** blur, sharpen, vignette, glow, rgb_split shaders. */
  shaderEffects: Mode
  /** grain, vintage, VHS (noise: never pixel-exact). */
  noiseEffects: Mode
  chromaKey: Mode
  masks: Mode
  /** bgremove via an alpha-packed proxy. */
  matte: Mode
  /** Native xfade ports (per-type tolerance table in P3). */
  nativeTransitions: Mode
  /** Pitch-preserving speed audio (atempo) — varispeed until tempo sidecars. */
  tempoAudio: Mode
  /** Speed-CURVE sound (render/speed_audio.py: varispeed or WSOLA on a
   *  warped map) — the client plays it with playbackRate automation, never
   *  sample-exact. (The curve's PICTURE is always EXACT.) */
  curveAudio: Mode
}

/** The capability table. Unmeasured ports are APPROX until the P3/P4 PSNR
 *  tables promote them (see `TRANSITION_EXACT`). */
export const PHASE_CAPS: Record<Phase, Capabilities> = {
  1: { colour: MODE_BAKED, shaderEffects: MODE_BAKED, noiseEffects: MODE_BAKED, chromaKey: MODE_BAKED,
       masks: MODE_BAKED, matte: MODE_BAKED, nativeTransitions: MODE_BAKED, tempoAudio: MODE_APPROX, curveAudio: MODE_APPROX },
  2: { colour: MODE_EXACT, shaderEffects: MODE_BAKED, noiseEffects: MODE_BAKED, chromaKey: MODE_BAKED,
       masks: MODE_BAKED, matte: MODE_BAKED, nativeTransitions: MODE_BAKED, tempoAudio: MODE_APPROX, curveAudio: MODE_APPROX },
  3: { colour: MODE_EXACT, shaderEffects: MODE_BAKED, noiseEffects: MODE_BAKED, chromaKey: MODE_BAKED,
       masks: MODE_BAKED, matte: MODE_BAKED, nativeTransitions: MODE_APPROX, tempoAudio: MODE_APPROX, curveAudio: MODE_APPROX },
  4: { colour: MODE_EXACT, shaderEffects: MODE_APPROX, noiseEffects: MODE_APPROX, chromaKey: MODE_APPROX,
       masks: MODE_EXACT, matte: MODE_APPROX, nativeTransitions: MODE_APPROX, tempoAudio: MODE_EXACT, curveAudio: MODE_APPROX },
  5: { colour: MODE_EXACT, shaderEffects: MODE_APPROX, noiseEffects: MODE_APPROX, chromaKey: MODE_APPROX,
       masks: MODE_EXACT, matte: MODE_APPROX, nativeTransitions: MODE_APPROX, tempoAudio: MODE_EXACT, curveAudio: MODE_APPROX },
}

/** Native transitions measured EXACT (≥ 35 dB) in the P3 table. Empty until
 *  that table exists: every native port is APPROX meanwhile. */
export const TRANSITION_EXACT: ReadonlySet<string> = new Set<string>()

/** render/transitions.py CUSTOM_EXPRS and POST_FILTERS (+ their aliases): no
 *  client port — always BAKED. Pinned by tests/goldens/transition_kinds.json. */
export const CUSTOM_TRANSITIONS: ReadonlySet<string> = new Set([
  'bars', 'blinds', 'boxopen', 'burn', 'checker', 'diamond', 'glitch', 'ripple', 'spiral', 'wave',
  'spin', 'swirl', 'venetian', 'stripes', 'luma', 'filmburn', 'boxin',
])
export const POST_TRANSITIONS: ReadonlySet<string> = new Set(['whip', 'whipright', 'whipup', 'whipdown', 'whippan'])

const COLOUR_EFFECTS = new Set(['color', 'color_grade', 'lut'])
const SHADER_EFFECTS = new Set(['blur', 'sharpen', 'vignette', 'glow', 'rgb_split'])
const NOISE_EFFECTS = new Set(['grain', 'vintage', 'vhs'])
/** Geometry passes of the P1 compositor (flip is a scale by −1). */
const GEOMETRY_EFFECTS = new Set(['hflip', 'vflip'])

export type ProxyState = 'ready' | 'pending' | 'failed'

export interface SupportOptions {
  phase: Phase
  /** Proxy state of a source (default: ready). `pending` → PENDING; `failed`
   *  → the degraded <video> tier, BAKED while playing (§7). */
  proxyState?: (src: string) => ProxyState
  /** Per-span readiness inside a ready proxy (on-demand spans, §5.1). */
  spanReady?: (src: string, frame: number) => boolean
  /** Output ranges the structural check (R14) found in disagreement. */
  demote?: ReadonlyArray<readonly [number, number]>
  /** The duck gain curve for the current render hash has landed (P2). */
  duckCurveReady?: boolean
  /** The preview-loudness gain matches the current render hash. */
  loudnessCurrent?: boolean
  /** Transition type → mechanism (the catalog's `kind`); default: the lists above. */
  transitionKind?: (type: string) => 'native' | 'custom' | 'post'
}

export interface ModeRange { k0: number; k1: number; mode: Mode; reasons: string[] }

export interface Support {
  mode: Uint8Array
  ranges: ModeRange[]
  /** Engine-level: may the client engine run at all (R1, §7)? */
  engine: { ok: true } | { ok: false; reason: string }
}

function defaultKind(type: string): 'native' | 'custom' | 'post' {
  const t = (type || '').trim().toLowerCase()
  if (POST_TRANSITIONS.has(t)) return 'post'
  if (CUSTOM_TRANSITIONS.has(t)) return 'custom'
  return 'native'
}

const isKeyframed = (v: unknown) => Array.isArray(v) || (typeof v === 'object' && v !== null)

/** The video+sound features of one v1 clip → (mode, reason) pairs. */
export function clipFeatures(c: EdlClip, caps: Capabilities): Array<[Mode, string]> {
  const out: Array<[Mode, string]> = []
  const effects = (c.effects as Array<{ type?: string }> | undefined) ?? []
  for (const e of effects) {
    const t = e?.type ?? ''
    if (GEOMETRY_EFFECTS.has(t)) continue
    if (COLOUR_EFFECTS.has(t)) out.push([caps.colour, `effect:${t}`])
    else if (SHADER_EFFECTS.has(t)) out.push([caps.shaderEffects, `effect:${t}`])
    else if (NOISE_EFFECTS.has(t)) out.push([caps.noiseEffects, `effect:${t}`])
    else out.push([MODE_BAKED, `effect:${t || 'unknown'}`])
    const params = (e as { params?: Record<string, unknown> }).params ?? {}
    if (Object.values(params).some(isKeyframed)) out.push([MODE_BAKED, `effect:${t}:keyframed`])
  }
  if (c.chromakey) out.push([caps.chromaKey, 'chromakey'])
  if (c.mask) out.push([caps.masks, 'mask'])
  if (c.matte_src) out.push([caps.matte, 'matte'])
  if (c.track_to) out.push([MODE_BAKED, 'motion-track'])
  const sp = c.speed
  const retimed = typeof sp === 'number' && sp > 0 && sp !== 1
  // A freeze's picture is one proxy frame, its sound silence: EXACT.
  if (freezeOf(c) !== null) return out
  if (isCurve(sp)) {
    // The curve's picture is EXACT (frameMap models its setpts to the bit);
    // its sound is not sample-addressed on the client.
    out.push([caps.curveAudio, 'audio:curve'])
    return out
  }
  if (retimed && c.reverse) {
    // The export resamples the reversed intermediate; frameMap has no
    // sample-exact runs for it (programMap.audioPlacements).
    out.push([MODE_APPROX, 'audio:reverse-speed'])
  } else if (retimed && (c.audio?.keep_pitch ?? true)) {
    out.push([caps.tempoAudio, 'audio:tempo'])
  }
  return out
}

/** Classify every output frame of `pm` (built from `edl`). */
export function classify(pm: ProgramMap, edl: EdlLike, opts: SupportOptions): Support {
  const caps = PHASE_CAPS[opts.phase]
  const n = pm.total
  const mode = new Uint8Array(n)
  const reasons: Array<Set<string> | null> = new Array(n).fill(null)
  const bump = (k0: number, k1: number, m: Mode, why: string) => {
    for (let k = Math.max(0, k0); k < Math.min(n, k1); k++) {
      if (m > mode[k]) mode[k] = m
      if (m > MODE_EXACT) (reasons[k] ??= new Set()).add(why)
    }
  }

  // Engine-level refusal (R1): a rate whose frame is not a whole number of
  // 240 kHz ticks cannot be written to MSE.
  const engine: Support['engine'] = pm.T === null ? { ok: false, reason: 'rate' } : { ok: true }

  // Per-clip features over each clip's frames (its own and, in a blend, as
  // either side).
  const clipMode = pm.clips.map((c) => clipFeatures(c, caps))
  const kindOf = opts.transitionKind ?? defaultKind
  const seamMode = new Map<number, Array<[Mode, string]>>()
  for (const s of pm.seams) {
    const kd = kindOf(s.type)
    const m: Mode = kd !== 'native' ? MODE_BAKED
      : caps.nativeTransitions === MODE_BAKED ? MODE_BAKED
        : TRANSITION_EXACT.has(s.type) ? MODE_EXACT : caps.nativeTransitions
    // Keyed by the incoming clip: a seam's right side is unique, while the
    // outgoing leaf of a nested blend is not the seam's left clip.
    seamMode.set(s.right, [[m, `transition:${s.type}`]])
  }
  const proxy = opts.proxyState ?? (() => 'ready' as ProxyState)
  for (let k = 0; k < n; k++) {
    if (pm.kind[k] === KIND_GAP) continue
    const sides: number[] = [pm.clip[k]]
    if (pm.kind[k] === KIND_BLEND) {
      sides.push(pm.bClip[k])
      if (pm.nested[k]) bump(k, k + 1, MODE_BAKED, 'transition:nested')
      for (const [m, why] of seamMode.get(pm.bClip[k]) ?? [[MODE_BAKED, 'transition']] as Array<[Mode, string]>) {
        bump(k, k + 1, m, why)
      }
    }
    for (const [i, ci] of sides.entries()) {
      for (const [m, why] of clipMode[ci]) bump(k, k + 1, m, why)
      const src = pm.clips[ci].src
      const st = proxy(src)
      const frame = i === 0 ? pm.srcFrame[k] : pm.bSrcFrame[k]
      if (st === 'pending' || (st === 'ready' && opts.spanReady && !opts.spanReady(src, frame))) {
        bump(k, k + 1, MODE_PENDING, 'proxy:pending')
      } else if (st === 'failed') {
        bump(k, k + 1, MODE_BAKED, 'proxy:degraded')
      }
    }
  }

  // Other lanes' sound: ducked beds are APPROX until the server curve lands
  // (§3.6, P2); retimed keep-pitch clips on audio lanes likewise.
  const fps = pm.R as FpsLike
  // The timeline UI's own seam table and render-time fold (one rule, R3).
  const seams = seamTable(videoClips(edl).map((c, i) => ({
    id: String(c.id ?? i), start: clipStart(c), duration: effectiveDuration(c),
  })), v1Transitions(edl), fps)
  const renderTime = (t: number) => layoutRenderTime(seams, t)
  for (const t of edl.tracks ?? []) {
    if (t.id === 'v1') continue
    const ducked = !!(t as { duck?: unknown }).duck && !opts.duckCurveReady
    for (const c of t.clips) {
      if (!isMediaClip(c)) continue
      const a = frameOf(renderTime(c.start ?? 0), fps)
      const eff = effectiveDuration(c)
      const b = frameOf(renderTime((c.start ?? 0) + eff), fps)
      if (ducked) bump(a, b, MODE_APPROX, 'audio:duck')
      if (freezeOf(c) !== null) continue
      if (isCurve(c.speed)) {
        bump(a, b, caps.curveAudio, 'audio:curve')
      } else if (typeof c.speed === 'number' && c.speed > 0 && c.speed !== 1 && (c.audio?.keep_pitch ?? true)) {
        bump(a, b, caps.tempoAudio, 'audio:tempo')
      }
    }
  }
  if (opts.loudnessCurrent === false) bump(0, n, MODE_APPROX, 'audio:loudness')
  for (const [a, b] of opts.demote ?? []) bump(a, b, MODE_BAKED, 'structure:mismatch')

  // Merge into ranges with identical mode and reasons.
  const ranges: ModeRange[] = []
  let k = 0
  while (k < n) {
    const key = (i: number) => `${mode[i]}|${[...(reasons[i] ?? [])].sort().join(',')}`
    const kk = key(k)
    let e = k + 1
    while (e < n && key(e) === kk) e++
    ranges.push({ k0: k, k1: e, mode: mode[k] as Mode, reasons: [...(reasons[k] ?? [])].sort() })
    k = e
  }
  return { mode, ranges, engine }
}
