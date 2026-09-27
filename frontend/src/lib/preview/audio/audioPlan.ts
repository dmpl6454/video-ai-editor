// The AUDIO PLAN: where every clip's sound lands in the program, at 48 kHz,
// and every gain the export applies to it — pure data, no Web Audio. A port
// of the render's sound graph (`compositor._audio_only_graph`, the preview's
// audio: the v1 assembly, the PiP fold, `audio_mix.build_audio_mix`) that
// mixGraph.ts realises on an AudioContext or OfflineAudioContext
// (instant preview spec §3.6, §6 R9/R10).
//
//   v1     `programMap.audioPlacements` (R9: samples_for_frames per segment,
//          acrossfade overlaps), gain/env/fades on CLIP-LOCAL samples
//          (`_audio_props_filters`), the track mute on the whole lane;
//   PiP    `pip_audio_chain`: the v1 seek rule, samples_for_frames(n) long,
//          delayed samples_for_frames(f0); folded into the main sound;
//   lanes  music / voice-over / audio tracks (`_audio_clip_filter`): the
//          input seek `[S(in), S(out))`, placed at round(rs·1000) ms on the
//          RENDER clock, fades on the render clock, the music duck;
//   master a mixed programme passes `alimiter=limit=0.97` with its auto-level
//          (+0.26 dB, measured); a loudness target adds the last-known
//          preview gain and a −1 dBFS limiter.

import {
  effectiveDuration, freezeOf, isMediaClip, planView, reversedFrameCount, speedFactor, type EdlClip, type EdlLike, type EdlTrack,
} from '../timeline/framePlan'
import {
  audioPlacements, clipSample0, type AudioPlacement, type AudioRun, type ProgramMap, type SourceLookup,
} from '../timeline/programMap'
import { curvePoints, type CurvePoints } from '../timeline/speedCurve'
import { ffmpegMicros, frameOf, samplesForFrames, type FpsLike } from '../timeline/timebase'
import {
  acrossfadeSamples, afadeGainAt, clipFadeWindows, clipGainLinear, compileEnv, envDbAt, laneFadeWindows, microsToSamples, pyRound,
  pyFixedValue, SAMPLE_RATE, sampleTime, type CompiledEnv, type FadeWindow, type GainEnv,
} from './curves'

export type ChannelMode = 'stereo' | 'left' | 'right' | 'mono'

export interface EdlAudioProps {
  gain_db?: number
  mute?: boolean
  fade_in?: number
  fade_out?: number
  gain_env?: GainEnv | null
  keep_pitch?: boolean
  channels?: ChannelMode | string
}

/** How a clip's output samples map onto its source's 48 kHz samples. */
export type SourceMap =
  /** Sample-exact: output `out0 + off + i` is source `first + dir·i`. */
  | { kind: 'runs'; runs: AudioRun[] }
  /** A resample (varispeed / atempo, APPROX): output `out0 + i` is source
   *  position `src0 + i·rate` (reverse: `src0 − i·rate`). Source samples at
   *  or past `end` are silence. */
  | { kind: 'rate'; src0: number; rate: number; reverse: boolean; end: number }
  /** A speed CURVE (render/speed_audio.py, APPROX): output `out0 + i` is
   *  source position `src0 + 48000 · source_seconds(i / 48000)` over the
   *  curve laid across `seconds` of source; at or past `end`, silence. */
  | { kind: 'curve'; src0: number; points: CurvePoints; seconds: number; end: number }

export type BusKind = 'v1' | 'pip' | 'music' | 'vo' | 'audio'

export interface BusPlan {
  id: string
  kind: BusKind
  /** 0 when the track is muted, or soloed out (`apply_solo`). */
  gain: number
}

export interface ClipAudio {
  /** `<bus>/<clip id>/<n>`: unique in the plan. */
  key: string
  id: string
  bus: string
  src: string
  /** Output samples [out0, out0 + n) at 48 kHz. */
  out0: number
  n: number
  map: SourceMap
  channels: ChannelMode
  /** `volume=<gain_db %.2f>dB`, linear. */
  gain: number
  mute: boolean
  env: CompiledEnv | null
  /** Output sample where the envelope's (and the fades') clip-local t = 0 is. */
  t0: number
  /** afade windows on the OUTPUT sample clock. */
  fades: FadeWindow[]
  /** acrossfade overlap samples at the head (incoming) and tail (outgoing). */
  xIn: number
  xOut: number
  exact: boolean
  /** Everything that decides WHICH samples play where (a change reschedules). */
  timing: string
  /** Everything that decides only their gain (a change rewrites automation). */
  params: string
}

export interface DuckPlan {
  bus: string
  /** 10^(to_db/20). */
  floor: number
  /** Output-sample intervals where the key (v1 + PiP + voice-over + audio
   *  lanes) carries sound, merged: the trapezoid's plateaus. */
  key: Array<[number, number]>
}

export interface MasterPlan {
  /** Static gain before the limiter: the mix limiter's auto-level (1/0.97
   *  when lanes are mixed) × the preview loudness gain. */
  gain: number
  /** Limiter ceiling in dBFS, or null when the render has none. */
  ceilingDb: number | null
}

export interface AudioPlan {
  /** Program length in samples (the v1 assembly; every lane is cut to it). */
  total: number
  clips: ClipAudio[]
  buses: BusPlan[]
  duck: DuckPlan | null
  master: MasterPlan
  /** Why parts of the sound are only approximate (§7 APPROX). */
  approx: string[]
}

export interface AudioPlanOptions {
  /** The last known preview loudness gain in dB (`/preview_loudness`), used
   *  when the canvas has a loudness target. null: none known yet. */
  loudnessGainDb?: number | null
  /** Sources known to have no sound (index.json `audio.silent`). */
  silent?: (src: string) => boolean
}

// ---------------------------------------------------------------- constants

/** Mix limiter of a preview with lanes (`alimiter=limit=0.97:latency=1`,
 *  auto-level on): it lifts everything by 1/0.97 and holds 0 dBFS. */
export const MIX_LIMIT = 0.97
/** PREVIEW_LIMITER's ceiling (0.891251 = −1 dBFS). */
export const LOUDNESS_CEILING_DB = -1
/** `audio_mix.DUCK_LOOKAHEAD_S` and the gain chain's time constants. */
export const DUCK_LOOKAHEAD_S = 0.06
export const DUCK_ATTACK_TAU_S = 1 / (2 * Math.PI * 6)
export const DUCK_RELEASE_TAU_S = 1 / (2 * Math.PI * 1)
/** Speech → hold: the 0.5 Hz one-pole falls below 0.01 after ln(100)/π s. */
export const DUCK_HOLD_S = Math.log(100) / (2 * Math.PI * 0.5)
/** `clock.SEAM_EPS`. */
const SEAM_EPS = 1e-6

const SR = SAMPLE_RATE

// ---------------------------------------------------------------- helpers

const audioOf = (c: EdlClip): EdlAudioProps => (c.audio ?? {}) as EdlAudioProps

function channelMode(a: EdlAudioProps): ChannelMode {
  const m = a.channels
  return m === 'left' || m === 'right' || m === 'mono' ? m : 'stereo'
}

/** Source sample an input seek `-ss t` starts at (`rescale(ffmpeg_us(t))`;
 *  no seek at 0). */
export const inputSeekSample = (t: number): number => (t > 0 ? microsToSamples(ffmpegMicros(t)) : 0)

/** `clock.render_time` over a seam table. */
export function renderTime(seams: Array<[number, number]>, t: number): number {
  let o = 0
  for (const [seam, cost] of seams) if (seam <= t + SEAM_EPS) o += cost
  return t - o
}

/** `clock.render_window`, or null when the seams consumed the item. */
export function renderWindow(seams: Array<[number, number]>, start: number, end: number): [number, number] | null {
  const rs = renderTime(seams, start)
  const re = renderTime(seams, end)
  return re - rs <= SEAM_EPS ? null : [rs, re]
}

const speedOf = (c: EdlClip): number | null => {
  const sp = c.speed
  return typeof sp === 'number' && sp > 0 && sp !== 1 ? sp : null
}

function paramsKey(a: EdlAudioProps, extra: string): string {
  return JSON.stringify([a.gain_db ?? 0, !!a.mute, a.fade_in ?? 0, a.fade_out ?? 0, a.gain_env ?? null,
    channelMode(a), extra])
}

function baseClip(c: EdlClip, bus: string, n: number): Pick<ClipAudio, 'id' | 'bus' | 'src' | 'channels' | 'gain' | 'mute' | 'env'> & { key: string } {
  const a = audioOf(c)
  return {
    key: `${bus}/${c.id}/${n}`, id: c.id, bus, src: c.src, channels: channelMode(a),
    gain: clipGainLinear(a.gain_db), mute: !!a.mute, env: compileEnv(a.gain_env ?? null),
  }
}

const shift = (ws: FadeWindow[], by: number): FadeWindow[] => ws.map((w) => ({ ...w, start: w.start + by }))

// ---------------------------------------------------------------- buses

function trackGain(t: EdlTrack, anySolo: boolean): number {
  return t.muted || (anySolo && !t.solo) ? 0 : 1
}

// ---------------------------------------------------------------- build

/** The plan from a program map (`programMap.audioPlacements` for v1). */
export function planFromProgram(edl: EdlLike, pm: ProgramMap, lookup?: SourceLookup,
                                opts: AudioPlanOptions = {}): AudioPlan {
  return buildAudioPlan(edl, audioPlacements(pm, lookup), pm.R, opts)
}

/** The v1 sound's length (`audio_total_samples`): every segment is
 *  samples_for_frames of its frames, less each seam's acrossfade. */
export function totalSamples(edl: EdlLike, fps: FpsLike): number {
  const view = planView(edl, fps)
  let total = 0
  view.plan.forEach((seg, si) => {
    total += samplesForFrames(seg.frames, fps) - (si > 0 ? acrossfadeSamples(view.segCost.get(si - 1) ?? 0) : 0)
  })
  return total
}

/** The whole programme's sound plan: `placements` are the v1 clips' (R9,
 *  `programMap.audioPlacements` over the same EDL at rate `fps`). */
export function buildAudioPlan(edl: EdlLike, placements: readonly AudioPlacement[], fps: FpsLike,
                               opts: AudioPlanOptions = {}): AudioPlan {
  const view = planView(edl, fps)
  const tracks = edl.tracks ?? []
  const anySolo = tracks.some((t) => !!t.solo)
  const silent = opts.silent ?? (() => false)
  const clips: ClipAudio[] = []
  const buses: BusPlan[] = []
  const approx = new Set<string>()

  // ---- v1 (the assembly: programMap's placements)
  const v1 = tracks.find((t) => t.id === 'v1')
  let total = 0
  view.plan.forEach((seg, si) => {
    total += samplesForFrames(seg.frames, fps) - (si > 0 ? acrossfadeSamples(view.segCost.get(si - 1) ?? 0) : 0)
  })
  buses.push({ id: 'v1', kind: 'v1', gain: v1 ? trackGain(v1, anySolo) : 1 })
  placements.forEach((p, i) => {
    const c = view.originals[p.clip]
    if (!c) return
    const planned = view.planned[p.clip] ?? c
    let map: SourceMap
    const mode = p.mode as string
    const pts = curvePoints(c.speed)
    if (p.runs.length) {
      map = { kind: 'runs', runs: p.runs.map((r) => [...r] as AudioRun) }
    } else if (mode === 'silence') {
      map = { kind: 'runs', runs: [] }                    // a freeze is silent (exact)
    } else if (mode === 'curve' && pts) {
      map = { kind: 'curve', src0: p.src0, points: pts, seconds: Math.max(0, (c.out ?? 0) - (c.in ?? 0)), end: Number.MAX_SAFE_INTEGER }
      approx.add('speed-curve')
    } else {
      // A resample (varispeed, atempo, a retimed reverse): APPROX. A retimed
      // reverse reads the reversed intermediate — the source backwards from
      // the last sample of its `reversed_frames`.
      const reverse = p.mode === 'reverse'
      const src0 = reverse ? p.src0 + samplesForFrames(reversedFrameCount(c, fps), fps) - 1 : p.src0
      map = { kind: 'rate', src0, rate: p.rate, reverse, end: Number.MAX_SAFE_INTEGER }
      approx.add(p.mode === 'tempo' ? 'tempo' : 'varispeed')
    }
    const exact = p.runs.length > 0 || mode === 'silence'
    const base = baseClip(c, 'v1', i)
    clips.push({
      ...base, out0: p.out0, n: p.n, map, t0: p.out0,
      fades: shift(clipFadeWindows(audioOf(c), effectiveDuration(planned)), p.out0),
      xIn: p.fadeIn, xOut: p.fadeOut, exact,
      timing: JSON.stringify(['v1', c.src, p.out0, p.n, map]),
      params: paramsKey(audioOf(c), `${effectiveDuration(planned)}|${p.fadeIn}|${p.fadeOut}`),
    })
  })

  // ---- PiP: every non-v1 video track, folded into the main sound
  const seams = view.seams
  const pips: Array<{ t: EdlTrack; c: EdlClip }> = []
  for (const t of tracks) {
    if (t.type !== 'video' || t.id === 'v1' || t.muted) continue
    for (const c of t.clips) if (isMediaClip(c)) pips.push({ t, c })
  }
  pips.sort((a, b) => (a.c.start ?? 0) - (b.c.start ?? 0))
  for (const { t, c } of pips) {
    // The window is the PIP's TIMELINE footprint and its sound follows its
    // speed (wave D3, E2: render/pip.py `pip_retime` + `pip_audio_chain`,
    // v1's rules): a freeze is silent, a curve reads its curve map, a
    // constant speed resamples, a reverse reads its range backwards.
    const eff = effectiveDuration(c)
    const win = renderWindow(seams, c.start ?? 0, (c.start ?? 0) + eff)
    if (!win) continue
    const bus = `pip:${t.id}`
    if (!buses.some((b) => b.id === bus)) buses.push({ id: bus, kind: 'pip', gain: trackGain(t, anySolo) })
    const f0 = frameOf(win[0], fps)
    const nf = Math.max(1, frameOf(win[1], fps) - f0)
    const n = samplesForFrames(nf, fps)
    const out0 = samplesForFrames(f0, fps)
    const s0 = clipSample0(c.in ?? 0, fps)
    const pts = curvePoints(c.speed)
    const sp = typeof c.speed === 'number' && c.speed > 0 && c.speed !== 1 ? c.speed : null
    let map: SourceMap
    if (freezeOf(c) !== null) {
      map = { kind: 'runs', runs: [] }                                   // silence (exact)
    } else if (c.reverse) {
      // The reversed intermediate's sound: the range backwards from the
      // last sample of its `reversed_frames` (as v1's retimed reverse).
      map = { kind: 'rate', src0: s0 + samplesForFrames(reversedFrameCount(c, fps), fps) - 1,
              rate: sp ?? (pts ? speedFactor(c.speed) : 1), reverse: true, end: Number.MAX_SAFE_INTEGER }
      approx.add('varispeed')
    } else if (pts) {
      map = { kind: 'curve', src0: s0, points: pts, seconds: Math.max(0, (c.out ?? 0) - (c.in ?? 0)), end: Number.MAX_SAFE_INTEGER }
      approx.add('speed-curve')
    } else if (sp !== null) {
      map = { kind: 'rate', src0: s0, rate: sp, reverse: false, end: Number.MAX_SAFE_INTEGER }
      approx.add((audioOf(c).keep_pitch ?? true) ? 'tempo' : 'varispeed')
    } else {
      map = { kind: 'runs', runs: [[0, n, s0, 1]] }
    }
    const exact = map.kind === 'runs'
    clips.push({
      ...baseClip(c, bus, clips.length), out0, n, map, t0: out0,
      fades: shift(clipFadeWindows(audioOf(c), eff), out0), xIn: 0, xOut: 0, exact,
      timing: JSON.stringify([bus, c.src, out0, n, map]),
      params: paramsKey(audioOf(c), String(effectiveDuration(c))),
    })
  }

  // ---- lanes: music (id "music"), voice-over (id "vo") + every audio track
  const laneTracks: EdlTrack[] = []
  const music = tracks.find((t) => t.id === 'music')
  const vo = tracks.find((t) => t.id === 'vo')
  if (music && !music.muted) laneTracks.push(music)
  if (vo && !vo.muted) laneTracks.push(vo)
  for (const t of tracks) if (t.type === 'audio' && !t.muted) laneTracks.push(t)
  let placedMusic = 0
  let placedLanes = 0
  for (const t of laneTracks) {
    const kind: BusKind = t.id === 'music' ? 'music' : t.id === 'vo' ? 'vo' : 'audio'
    const bus = t.id
    for (const c of t.clips) {
      if (!isMediaClip(c)) continue
      const eff = effectiveDuration(c)
      const win = renderWindow(seams, c.start ?? 0, (c.start ?? 0) + eff)
      if (!win) continue
      if (!buses.some((b) => b.id === bus)) buses.push({ id: bus, kind, gain: trackGain(t, anySolo) })
      const lane = laneClip(c, bus, clips.length, win, eff)
      if (lane.map.kind === 'curve') approx.add('speed-curve')
      else if (!lane.exact) approx.add((audioOf(c).keep_pitch ?? true) ? 'tempo' : 'varispeed')
      clips.push(lane)
      placedLanes++
      if (kind === 'music') placedMusic++
    }
  }

  // ---- music duck (APPROX trapezoid until the server curve lands)
  let duck: DuckPlan | null = null
  const toDb = (music?.duck as { to_db?: number } | null | undefined)?.to_db
  if (music && placedMusic && music.duck) {
    const heard = (b: string) => (buses.find((x) => x.id === b)?.gain ?? 0) > 0
    const keyed = clips.filter((c) => c.bus !== 'music' && heard(c.bus) && !c.mute && c.gain > 0 && !silent(c.src))
    duck = { bus: 'music', floor: Math.pow(10, Math.min(0, toDb ?? -18) / 20), key: mergeIntervals(keyed.map((c) => [c.out0, c.out0 + c.n]), total) }
    approx.add('duck')
  }

  // ---- master
  const mixed = placedLanes > 0
  let gain = mixed ? 1 / MIX_LIMIT : 1
  let ceilingDb: number | null = mixed ? 0 : null
  const lufs = (edl.canvas as { loudness_lufs?: number | null } | undefined)?.loudness_lufs
  if (lufs !== null && lufs !== undefined && opts.loudnessGainDb !== null && opts.loudnessGainDb !== undefined) {
    gain *= Math.pow(10, pyFixedValue(opts.loudnessGainDb, 2) / 20)
    ceilingDb = LOUDNESS_CEILING_DB
    approx.add('loudness')
  }
  return { total, clips, buses, duck, master: { gain, ceilingDb }, approx: [...approx] }
}

/** One music / voice-over / audio-lane clip (`_audio_clip_filter`). */
function laneClip(c: EdlClip, bus: string, n: number, win: [number, number], eff: number): ClipAudio {
  const a = audioOf(c)
  const [rs, re] = win
  const sIn = inputSeekSample(c.in ?? 0)
  const sOut = microsToSamples(ffmpegMicros(c.out ?? 0))
  const sp = speedOf(c)
  const pts = curvePoints(c.speed)
  const frozen = typeof c.freeze === 'number' && c.freeze > 0
  const srcLen = Math.max(0, sOut - sIn)
  // atrim=end_sample for a retimed clip (speed, curve, freeze) or one a
  // seam shortened.
  const cut = sp !== null || pts !== null || frozen || re - rs < eff - 0.0005
  const len = cut ? Math.max(0, pyRound(Math.min(re - rs, eff) * SR)) : srcLen
  const delayMs = Math.max(0, pyRound(rs * 1000))
  const out0 = delayMs * (SR / 1000)
  let map: SourceMap
  if (frozen) map = { kind: 'runs', runs: [] }                          // volume=0
  else if (pts) map = { kind: 'curve', src0: sIn, points: pts, seconds: Math.max(0, (c.out ?? 0) - (c.in ?? 0)), end: sOut }
  else if (sp !== null) map = { kind: 'rate', src0: sIn, rate: speedFactor(sp), reverse: false, end: sOut }
  else map = { kind: 'runs', runs: [[0, Math.min(len, srcLen), sIn, 1]] }
  const base = baseClip(c, bus, n)
  return {
    ...base, out0, n: len, map, t0: out0, fades: laneFadeWindows(a, rs, re), xIn: 0, xOut: 0,
    exact: map.kind === 'runs',
    timing: JSON.stringify([bus, c.src, out0, len, map]),
    params: paramsKey(a, `${rs}|${re}`),
  }
}

/** Sorted, merged, clipped to [0, total). */
export function mergeIntervals(iv: Array<[number, number]>, total: number): Array<[number, number]> {
  const s = iv.map(([a, b]) => [Math.max(0, a), Math.min(total, b)] as [number, number])
    .filter(([a, b]) => b > a).sort((x, y) => x[0] - y[0])
  const out: Array<[number, number]> = []
  for (const [a, b] of s) {
    const last = out[out.length - 1]
    if (last && a <= last[1]) last[1] = Math.max(last[1], b)
    else out.push([a, b])
  }
  return out
}

// ---------------------------------------------------------------- per-sample gain

/** The clip's gain at output sample `p` (everything except channel mode,
 *  bus, duck and master): clip gain × envelope × afades × acrossfade × mute. */
export function clipGainAt(c: ClipAudio, p: number): number {
  if (c.mute) return 0
  let g = c.gain
  if (c.env) {
    // ffmpeg's `t` of the clip-local sample (i · 1/48000).
    g *= Math.pow(10, envDbAtLocal(c.env, p - c.t0) / 20)
  }
  for (const w of c.fades) g *= afadeGainAt(w, p)
  const i = p - c.out0
  if (c.xIn > 0 && i < c.xIn) g *= i / c.xIn
  if (c.xOut > 0) {
    const j = i - (c.n - c.xOut)
    if (j >= 0) g *= (c.xOut - 1 - j) / c.xOut
  }
  return g
}

function envDbAtLocal(env: CompiledEnv, i: number): number {
  return envDbAt(env, sampleTime(i))
}

/** Fill `out[j] = clipGainAt(c, p0 + j)`. */
export function fillClipGain(c: ClipAudio, p0: number, out: Float32Array): Float32Array {
  for (let j = 0; j < out.length; j++) out[j] = clipGainAt(c, p0 + j)
  return out
}

/** Which buses' SOURCES must be rescheduled between two plans (a timing
 *  change on any of their clips), and which clips only changed gains. */
export interface PlanDiff {
  dirtyBuses: Set<string>
  paramClips: Set<string>
  busGains: boolean
  master: boolean
  duck: boolean
}

export function diffPlans(prev: AudioPlan | null, next: AudioPlan): PlanDiff {
  const dirtyBuses = new Set<string>()
  const paramClips = new Set<string>()
  if (!prev) {
    for (const b of next.buses) dirtyBuses.add(b.id)
    return { dirtyBuses, paramClips, busGains: true, master: true, duck: true }
  }
  const timings = (p: AudioPlan, bus: string) => p.clips.filter((c) => c.bus === bus).map((c) => `${c.id}|${c.timing}`).join('\n')
  const busIds = new Set([...prev.buses.map((b) => b.id), ...next.buses.map((b) => b.id)])
  for (const b of busIds) if (timings(prev, b) !== timings(next, b)) dirtyBuses.add(b)
  const before = new Map(prev.clips.map((c) => [`${c.bus}|${c.id}|${c.timing}`, c]))
  for (const c of next.clips) {
    if (dirtyBuses.has(c.bus)) continue
    const old = before.get(`${c.bus}|${c.id}|${c.timing}`)
    if (old && old.params !== c.params) paramClips.add(c.key)
  }
  const gains = (p: AudioPlan) => JSON.stringify(p.buses.map((b) => [b.id, b.gain]))
  return {
    dirtyBuses, paramClips,
    busGains: gains(prev) !== gains(next),
    master: prev.master.gain !== next.master.gain || prev.master.ceilingDb !== next.master.ceilingDb,
    duck: JSON.stringify(prev.duck) !== JSON.stringify(next.duck),
  }
}
