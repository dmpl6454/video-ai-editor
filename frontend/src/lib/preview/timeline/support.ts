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
import { hasAnimation, planOf, type AnimFields } from '../../anim/clipAnim'
import { voicePlan } from '../../voice/voiceFx'
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

/** Transform.flip_h / flip_v (CapCut Mirror / Flip, wave E lane F4a): EXACT.
 *  A geometry pass like the flip effects — the fitted frame mirrored BEFORE
 *  the rotation (geometry.ts `flipStage`; the cover-pan window taken at +x) —
 *  so it adds no cause. Measured: the nine `tflip_*` cases of
 *  tests/goldens/geometry_cases.json (real compositor renders: rotate, cover
 *  pan, scale + pan, keyframes, anamorphic, with the hflip effect) within
 *  1 px in geometry.test.ts, and transformFlip.test.ts shows each would miss
 *  by > 20 px unmirrored (the cover-pan case by 60 px with its pan
 *  unflipped). Overlays and stickers are drawn by pipDraw /
 *  StickerLayer in both modes (`flipScale` inside the element's turn,
 *  tests/test_transform_flip_render.py measures the export). */
export const TRANSFORM_FLIP_MODE: Mode = MODE_EXACT

/** Voice effects (wave E, F3; edl/voice_effects.py, lib/voice/voiceFx.ts),
 *  measured: the export's sound against the client's offline mix of the same
 *  timeline (tests/wk/test_wk_voice_fx.py — a synthesized voice on v1, an
 *  overlay, the voice-over and the music lane; Playwright Chromium and
 *  WebKit gave identical numbers), per 50 ms block: `centroidPct` bounds the
 *  mean |client/export − 1| of the spectral centroid (%), `rmsDb` the mean
 *  |Δ| of the block level; `unit*` bound the pitch shifter alone against
 *  ffmpeg's (voiceFx.test.ts). Both tests fail past these numbers.
 *
 *  EXACT — robot, echo, telephone, megaphone, radio, underwater, vibrato: the
 *  client runs the export's own biquads, echo taps, saturation, ring
 *  modulation and vibrato line on the samples of each block with its
 *  look-back (so a seek or an edit mid-echo is exact too). Measured max
 *  |Δ| 1.6e-5 (ffmpeg's float32 biquads against double), block level
 *  ≤ 0.0002 dB, lag 0.
 *  APPROX — the Hall: the export's own impulse response in a ConvolverNode,
 *  max |Δ| 1.9e-7 from a cold start, but the node's tail restarts at a seek
 *  or a structural edit (up to its 2 s). The pitch presets (chipmunk, deep,
 *  monster): a period-aligned granular shifter where the export re-clocks and
 *  WSOLA-stretches — measured centroid 6.1-8.0 % mean (21 % worst block),
 *  level 1.3-1.7 dB mean (3.7 dB worst), 0.7-1.3 ms later. */
export const VOICE_FX_PARITY = {
  chipmunk: { mode: MODE_APPROX, centroidPct: 8, rmsDb: 2.0, unitCentroidMean: 0.03, unitRmsMeanDb: 1.0 },
  deep: { mode: MODE_APPROX, centroidPct: 8, rmsDb: 2.0, unitCentroidMean: 0.03, unitRmsMeanDb: 1.75 },
  monster: { mode: MODE_APPROX, centroidPct: 9.5, rmsDb: 1.75, unitCentroidMean: 0.05, unitRmsMeanDb: 1.5 },
  robot: { mode: MODE_EXACT, centroidPct: 0.01, rmsDb: 0.01 },
  echo: { mode: MODE_EXACT, centroidPct: 0.01, rmsDb: 0.01 },
  reverb: { mode: MODE_APPROX, centroidPct: 0.01, rmsDb: 0.01 },
  telephone: { mode: MODE_EXACT, centroidPct: 0.01, rmsDb: 0.01 },
  megaphone: { mode: MODE_EXACT, centroidPct: 0.01, rmsDb: 0.01 },
  radio: { mode: MODE_EXACT, centroidPct: 0.01, rmsDb: 0.01 },
  underwater: { mode: MODE_EXACT, centroidPct: 0.01, rmsDb: 0.01 },
  vibrato: { mode: MODE_EXACT, centroidPct: 0.01, rmsDb: 0.01 },
} as const

/** `audio:voice:<id>` for a clip whose sound carries an APPROX voice effect
 *  (VOICE_FX_PARITY), else null (none, or an EXACT one). */
export function voiceFxReason(c: EdlClip): string | null {
  const a = (c as { audio?: { voice_effect?: unknown; voice_intensity?: unknown } }).audio
  const plan = voicePlan(a?.voice_effect ?? null, a?.voice_intensity ?? 1)
  if (!plan) return null
  const row = VOICE_FX_PARITY[plan.effect as keyof typeof VOICE_FX_PARITY]
  return row && row.mode === MODE_EXACT ? null : `audio:voice:${plan.effect}`
}

/** Clip animations (wave E, F1; edl/clip_animations.py, lib/anim/clipAnim.ts)
 *  on a v1 clip. The engine runs the export's own chain for them (geometry.ts:
 *  the keyed-transform branch with the animation's scale × / x, y + /
 *  rotation +, and the `fade` ramps), measured as Y-PSNR of the engine's
 *  canvas against the decoded export of the same EDL — every preset at 3
 *  frames of its window, real footage, Playwright Chromium, Playwright
 *  WebKit and WKWebView
 *  (tests/wk/test_clip_anim_parity.py, which pins these classes):
 *
 *  * EXACT (≥ 35 dB, lowest 35.7): Fade, Zoom In (In), Zoom In / Zoom Out
 *    (Out), the four Slides, Rotate, Bounce (both sides) and every Combo, on
 *    top of keys, static poses and cover fit.
 *  * APPROX (`ANIM_APPROX`, the lowest frame in dB, Chromium / WebKit):
 *    Zoom Out In 34.8-34.9 in both — its ×1.5 start is a bicubic up-scale in
 *    the export, bilinear on the GPU; Spin In 34.0-34.2 and Spin Out
 *    30.31 / 30.32 — the export turns the picture THEN re-scales it (two
 *    resamplings), the engine samples the source once. Two runs each.
 *  * BAKED: Blur In / Blur Out mix a gaussian-blurred copy the engine has no
 *    pass for (26.3 dB drawn without it) — over the blur's own window only.
 *
 *  Only the frames of the animation's own window change class. */
/** Blur In / Blur Out on a v1 clip: no engine pass exists — BAKED at every
 *  phase until one is built and measured (review RE). */
export const ANIM_BLUR_MODE: Mode = MODE_BAKED

export const ANIM_APPROX: Readonly<Record<'in' | 'out', ReadonlySet<string>>> = {
  in: new Set(['zoom_out', 'spin']),
  out: new Set(['spin']),
}

/** The windows (clip-local seconds) where a v1 clip's animation is not
 *  EXACT: [t0, t1, mode, reason]. */
export function animWindows(c: EdlClip, blurMode: Mode = MODE_BAKED): Array<[number, number, Mode, string]> {
  const a = c as AnimFields
  if (!hasAnimation(a)) return []
  const pl = planOf(a, effectiveDuration(c))
  if (!pl) return []
  const out: Array<[number, number, Mode, string]> = []
  for (const w of [pl.blurIn, pl.blurOut]) if (w) out.push([w[0], w[0] + w[1], blurMode, 'anim:blur'])
  if (a.anim_in && ANIM_APPROX.in.has(a.anim_in) && pl.dIn > 0) out.push([0, pl.dIn, MODE_APPROX, `anim:in:${a.anim_in}`])
  if (a.anim_out && ANIM_APPROX.out.has(a.anim_out) && pl.dOut > 0) {
    out.push([pl.window - pl.dOut, pl.window, MODE_APPROX, `anim:out:${a.anim_out}`])
  }
  return out
}

/** Back-compat helper for tests: the blur windows only. */
export function animBlurWindows(c: EdlClip): Array<[number, number]> {
  return animWindows(c).filter((w) => w[3] === 'anim:blur').map((w) => [w[0], w[1]])
}

/** CapCut Canvas background of a letterboxed (contain) v1 clip (wave E, F2;
 *  edl/canvas_blend.py, render/canvas_bg.py — the engine: render/canvasBg.ts,
 *  canvasBlur.ts and the geometry shader's letterbox). MEASURED as Y-PSNR of
 *  the engine canvas against the decoded export of the same EDL — the whole
 *  frame and the letterbox alone — on a 16:9 canvas (a portrait clip) and a
 *  9:16 canvas (a landscape clip) of real footage, Playwright Chromium and
 *  WebKit (identical to ±0.1 dB; tests/wk/test_canvas_bg_parity.py, which
 *  asserts this table):
 *
 *  * colour: letterbox 51.1-72.8 dB, frame 37.9-47.0 (the frame's floor is
 *    the picture's own proxy parity) — EXACT;
 *  * image: the export's own cover-fitted file, sampled 1:1 — letterbox
 *    48.9-49.8, frame 36.2-37.5 — EXACT;
 *  * blur (all four strengths, and under a static pan/zoom): the export's
 *    1/4-size cover + Gaussian + bilinear, re-derived on the GPU — letterbox
 *    42.7-50.2, frame 35.6-42.6 — EXACT;
 *  * any kind on a ROTATED clip (`CANVAS_BG_ROTATED_MODE`): APPROX — the
 *    rotated frame's corners are black against the background, and the two
 *    renderers' rotate resamplings differ at that edge (white at 6°: 33.0 dB,
 *    blur at −8°: 37.1). */
export const CANVAS_BG_MODE: Readonly<Record<'color' | 'image' | 'blur', Mode>> = {
  color: MODE_EXACT,
  image: MODE_EXACT,
  blur: MODE_EXACT,
}
export const CANVAS_BG_ROTATED_MODE: Mode = MODE_APPROX

/** Review RE: the background is a STILL layer under the moving picture
 *  (render/canvas_bg.composite_block; the shader lays the picture over it by
 *  the same matte). Re-measured on the new composite (Playwright Chromium and
 *  WebKit, identical to ±0.1 dB; tests/wk/test_canvas_bg_parity.py): the
 *  unmoved kinds 35.7-45.1 dB frame / 43.7-55.3 letterbox, a pan 38.7-42.0 /
 *  40.7-47.8 — EXACT. A picture SCALED below 1 over the background (Blur 3 at
 *  0.7: 34.1-36.0 frame, 43.4-48.3 letterbox — the picture's own two
 *  resamplings against the GPU's one, the matte's soft edge against a hard
 *  one) and a KEYED or ANIMATED move (unmeasured) are APPROX. */
export const CANVAS_BG_MOVED_MODE: Mode = MODE_APPROX

/** The (mode, reason) a clip's canvas background adds, or null (none, a
 *  cover fit — no letterbox — or EXACT). */
export function canvasBgReason(c: EdlClip): [Mode, string] | null {
  const bg = (c as { canvas_bg?: { type?: unknown } | null }).canvas_bg
  const t = bg?.type
  if ((t !== 'color' && t !== 'image' && t !== 'blur') || (c as { fit?: string }).fit === 'cover') return null
  const tx = (c as { transform?: { rotation?: unknown; scale?: unknown; x?: unknown; y?: unknown } }).transform
  const rot = tx?.rotation
  if (isKeyframed(rot) || (typeof rot === 'number' && Math.abs(rot) > 0.001)) {
    return CANVAS_BG_ROTATED_MODE === MODE_EXACT ? null : [CANVAS_BG_ROTATED_MODE, `canvas:${t}:rotated`]
  }
  const sc = tx?.scale
  const moved = isKeyframed(sc) || isKeyframed(tx?.x) || isKeyframed(tx?.y)
    || (typeof sc === 'number' && sc < 0.999) || animatesGeometry(c)
  if (moved && CANVAS_BG_MOVED_MODE !== MODE_EXACT) return [CANVAS_BG_MOVED_MODE, `canvas:${t}:moved`]
  const m = CANVAS_BG_MODE[t]
  return m === MODE_EXACT ? null : [m, `canvas:${t}`]
}

/** A clip animation that moves the picture (a slide, zoom, spin, bounce…). */
function animatesGeometry(c: EdlClip): boolean {
  const a = c as AnimFields
  if (!hasAnimation(a)) return false
  const pl = planOf(a, effectiveDuration(c))
  return !!pl && (['scale', 'x', 'y', 'rotation'] as const).some((ch) => !!pl.keyed[ch] || !!pl.waves[ch])
}

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
  /** Output frame ranges where the master limiter may work: the browser's
   *  limiter is not the export's alimiter there (gate RX; audio/limiting.ts). */
  limiting?: ReadonlyArray<readonly [number, number]>
  /** Transition type → mechanism (the catalog's `kind`); default: the lists above. */
  transitionKind?: (type: string) => 'native' | 'custom' | 'post'
  /** An IMAGE canvas background's picture: not yet decoded / failed (review
   *  RE) makes its clip's frames PENDING — black bars are not the export. */
  canvasImagePending?: (clip: EdlClip) => boolean
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
  const canvasBg = canvasBgReason(c)
  if (canvasBg) out.push(canvasBg)
  const sp = c.speed
  const retimed = typeof sp === 'number' && sp > 0 && sp !== 1
  // A freeze's picture is one proxy frame, its sound silence: EXACT.
  if (freezeOf(c) !== null) return out
  // A voice effect (wave E, F3): APPROX where VOICE_FX_PARITY says so.
  const voice = voiceFxReason(c)
  if (voice) out.push([MODE_APPROX, voice])
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
  } else if (retimed) {
    out.push([VARISPEED_AUDIO_MODE, 'audio:varispeed'])
  }
  return out
}

/** Sound retimed with Keep pitch off (and an overlay's reversed sound): the
 *  client plays a playbackRate resample of the source where the export runs
 *  ffmpeg's resampler — never sample-exact (audioPlan.ts `approx`
 *  'varispeed', measured at the APPROX 1 dB in tests/wk/test_wk_audio.py).
 *  Final QA r3: it was left EXACT, so no ≈ showed over a 2x or 0.5x clip. */
export const VARISPEED_AUDIO_MODE: Mode = MODE_APPROX

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
  const clipMode = pm.clips.map((c) => {
    const f = clipFeatures(c, caps)
    if (opts.canvasImagePending?.(c)) f.push([MODE_PENDING, 'canvas:image:pending'])
    return f
  })
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

  // A v1 clip animation (wave E, F1): its blur BAKED, the APPROX presets
  // APPROX — over the animation's own window only.
  for (const [ci, c] of pm.clips.entries()) {
    const k0 = pm.clipStart[ci]
    if (!(k0 >= 0)) continue
    // review RE: BAKED, not `caps.shaderEffects` (APPROX from phase 4): the
    // engine has no animation-blur pass (26.3 dB drawn without it); only a
    // measured gaussian-mix pass may promote it
    for (const [a, b, m, why] of animWindows(c, ANIM_BLUR_MODE)) {
      bump(k0 + frameOf(a, fps), k0 + frameOf(b, fps) + 1, m, why)
    }
  }
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
      const voice = voiceFxReason(c)
      if (voice) bump(a, b, MODE_APPROX, voice)
      const retimed = typeof c.speed === 'number' && c.speed > 0 && c.speed !== 1
      if (t.type === 'video' && c.reverse) {
        // an overlay's reversed sound reads its range backwards at its rate
        // (audioPlan's PiP branch: a resample, curve or not, even at 1x)
        bump(a, b, VARISPEED_AUDIO_MODE, retimed || isCurve(c.speed) ? 'audio:reverse-speed' : 'audio:reverse')
      } else if (isCurve(c.speed)) {
        bump(a, b, caps.curveAudio, 'audio:curve')
      } else if (retimed && (c.audio?.keep_pitch ?? true)) {
        bump(a, b, caps.tempoAudio, 'audio:tempo')
      } else if (retimed) {
        bump(a, b, VARISPEED_AUDIO_MODE, 'audio:varispeed')
      }
    }
  }
  if (opts.loudnessCurrent === false) bump(0, n, MODE_APPROX, 'audio:loudness')
  for (const [a, b] of opts.limiting ?? []) bump(Math.max(0, a), Math.min(n, b), MODE_APPROX, 'audio:limiting')
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
