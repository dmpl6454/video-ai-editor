// The PROGRAM MAP (instant preview spec §4.1, §8, appendix A): for every
// output frame k of the render, which source frame (and, across a seam,
// which incoming frame and at what xfade progress) the export shows. A port
// of `render/frame_map.py::build_program_map`, struct-of-arrays, with a
// per-clip memo so an edit recomputes only the clips whose timing changed,
// a diff → dirty ranges, and the RLE form `GET /frame_map` returns (§8).

import { freezeOf, planView, type EdlClip, type EdlLike, type PlanSeam } from './framePlan'
import { curvePoints, meanSpeed } from './speedCurve'
import {
  clipFrameList, reverseSegmentFrames, reversedFrameCount, sourceFromJson, ticksPerFrame,
  type SourceInfo, type SourceInfoJson,
} from './frameMap'
import {
  ffmpegMicros, rateOf, rescale, samplesForFrames, seekPreroll, timeOf, type FpsLike, type Rational,
} from './timebase'

export const KIND_CLIP = 0
export const KIND_GAP = 1
export const KIND_BLEND = 2

export type SourceLookup = (src: string) => SourceInfo

export interface ProgramMap {
  R: Rational
  /** MSE ticks per frame at timescale 240000 (null: non-standard rate). */
  T: number | null
  total: number
  kind: Uint8Array
  /** Index into `clips` (−1 for a gap). For a blend: the OUTGOING leaf. */
  clip: Int32Array
  /** Index into `sources` (−1 for a gap). */
  srcKey: Int32Array
  srcFrame: Int32Array
  /** Blend only: the incoming clip / source / frame (else −1). */
  bClip: Int32Array
  bSrcKey: Int32Array
  bSrcFrame: Int32Array
  /** Blend progress P = pNum / pDen (xfade's 1 → 0); pDen 0 = none. */
  pNum: Int32Array
  pDen: Int32Array
  progress: Float32Array
  /** A blend whose outgoing side is itself a blend (3 clips at once). */
  nested: Uint8Array
  /** v1 clips in render order (original fields). */
  clips: EdlClip[]
  /** Distinct `src` strings, in first-use order (`srcKey` indexes this). */
  sources: string[]
  /** Per clip: first output frame of its segment and its frame count. */
  clipStart: Int32Array
  clipLen: Int32Array
  seams: PlanSeam[]
  /** The v1 assembly: kind, clip index (−1 gap), frames, and the seconds the
   *  seam BEFORE the segment overlaps it (0 for a cut). */
  segments: Array<{ kind: 'clip' | 'gap'; clip: number; frames: number; costBefore: number }>
}

// ---------------------------------------------------------------- per-clip memo

const MEMO_MAX = 4096
const memo = new Map<string, Int32Array>()
let memoHits = 0
let memoMisses = 0

function srcJsonKey(s: SourceInfo): string {
  return `${s.rate.num}/${s.rate.den}|${s.tb.num}/${s.tb.den}|${s.frames}|${s.startTicks}|${s.w}x${s.h}`
}

/** The timing hash of §4.1: every field frame selection reads. */
export function clipTimingKey(c: EdlClip, src: SourceInfo, fps: FpsLike): string {
  const r = rateOf(fps)
  const curve = curvePoints(c.speed)
  const sp = typeof c.speed === 'number' ? c.speed : curve ? JSON.stringify(curve) : null
  const fz = freezeOf(c)
  return `${c.src}|${c.in ?? 0}|${c.out ?? 0}|${sp}|${c.reverse ? 1 : 0}|${fz ?? ''}|${r.num}/${r.den}|${srcJsonKey(src)}`
}

function memoFrames(c: EdlClip, src: SourceInfo, fps: FpsLike): Int32Array {
  const key = clipTimingKey(c, src, fps)
  const hit = memo.get(key)
  if (hit) {
    memoHits++
    memo.delete(key)
    memo.set(key, hit)
    return hit
  }
  memoMisses++
  const frames = clipFrameList(c, src, fps)
  memo.set(key, frames)
  if (memo.size > MEMO_MAX) memo.delete(memo.keys().next().value as string)
  return frames
}

export function memoStats(): { hits: number; misses: number; size: number } {
  return { hits: memoHits, misses: memoMisses, size: memo.size }
}

export function clearMemo(): void {
  memo.clear()
  memoHits = 0
  memoMisses = 0
}

// ---------------------------------------------------------------- build

/** `build_program_map(edl, sources)`: the map at `fps` (default canvas rate). */
export function buildProgramMap(edl: EdlLike, lookup: SourceLookup, fpsArg?: FpsLike): ProgramMap {
  const view = planView(edl, fpsArg)
  const fps = view.fps
  const R = rateOf(fps)
  let total = 0
  for (const seg of view.plan) total += seg.frames
  // Upper bound; seams only shorten the output.
  const kind = new Uint8Array(total)
  const clip = new Int32Array(total)
  const srcFrame = new Int32Array(total)
  const bClip = new Int32Array(total).fill(-1)
  const bSrcFrame = new Int32Array(total).fill(-1)
  const pNum = new Int32Array(total)
  const pDen = new Int32Array(total)
  const nested = new Uint8Array(total)
  const clipStart = new Int32Array(view.originals.length).fill(-1)
  const clipLen = new Int32Array(view.originals.length)
  const perClip = new Map<number, Int32Array>()

  let len = 0
  view.plan.forEach((seg, si) => {
    const d = si > 0 ? (view.segTrans.get(si - 1) ?? 0) : 0
    let frames: Int32Array | null = null
    if (seg.kind === 'clip') {
      const c = view.originals[seg.clip]
      frames = perClip.get(seg.clip) ?? memoFrames(c, lookup(c.src), fps)
      perClip.set(seg.clip, frames)
    }
    if (d > 0 && seg.kind === 'clip') {
      // xfade: the last d frames of the program so far blend with the first
      // d frames of this clip; the rest of it follows.
      const offset = Math.max(0, len - d)
      for (let j = 0; j < d; j++) {
        const k = offset + j
        nested[k] = kind[k] === KIND_BLEND ? 1 : 0
        kind[k] = KIND_BLEND
        bClip[k] = seg.clip
        bSrcFrame[k] = frames![j]
        // P = 1 − j/d, unreduced (den = the seam's frames: one RLE run).
        pNum[k] = d - j
        pDen[k] = d
      }
      clipStart[seg.clip] = offset
      clipLen[seg.clip] = seg.frames
      for (let j = d; j < seg.frames; j++) push(KIND_CLIP, seg.clip, frames![j])
      return
    }
    if (seg.kind === 'clip') {
      clipStart[seg.clip] = len
      clipLen[seg.clip] = seg.frames
      for (let j = 0; j < seg.frames; j++) push(KIND_CLIP, seg.clip, frames![j])
    } else {
      for (let j = 0; j < seg.frames; j++) push(KIND_GAP, -1, -1)
    }
  })

  function push(kd: number, ci: number, f: number): void {
    kind[len] = kd
    clip[len] = ci
    srcFrame[len] = f
    bClip[len] = -1
    bSrcFrame[len] = -1
    pNum[len] = 0
    pDen[len] = 0
    nested[len] = 0
    len++
  }

  const sources: string[] = []
  const srcIndex = new Map<string, number>()
  const clipSrc = view.originals.map((c) => {
    let i = srcIndex.get(c.src)
    if (i === undefined) { i = sources.length; sources.push(c.src); srcIndex.set(c.src, i) }
    return i
  })
  const srcKey = new Int32Array(len)
  const bSrcKey = new Int32Array(len)
  const progress = new Float32Array(len)
  for (let k = 0; k < len; k++) {
    srcKey[k] = clip[k] >= 0 ? clipSrc[clip[k]] : -1
    bSrcKey[k] = bClip[k] >= 0 ? clipSrc[bClip[k]] : -1
    progress[k] = pDen[k] ? pNum[k] / pDen[k] : 0
  }
  return {
    R, T: ticksPerFrame(R), total: len,
    kind: kind.slice(0, len), clip: clip.slice(0, len), srcKey, srcFrame: srcFrame.slice(0, len),
    bClip: bClip.slice(0, len), bSrcKey, bSrcFrame: bSrcFrame.slice(0, len),
    pNum: pNum.slice(0, len), pDen: pDen.slice(0, len), progress, nested: nested.slice(0, len),
    clips: view.originals, sources, clipStart, clipLen, seams: view.seamRows,
    segments: view.plan.map((seg, si) => ({
      kind: seg.kind, clip: seg.clip ?? -1, frames: seg.frames,
      costBefore: si > 0 ? (view.segCost.get(si - 1) ?? 0) : 0,
    })),
  }
}

/** A lookup over `{src: SourceInfoJson}` (the frame_map / golden shape). */
export function lookupFromJson(table: Record<string, SourceInfoJson>): SourceLookup {
  const cache = new Map<string, SourceInfo>()
  return (src) => {
    let s = cache.get(src)
    if (!s) {
      const j = table[src]
      if (!j) throw new Error(`no SourceInfo for ${src}`)
      s = sourceFromJson(j)
      cache.set(src, s)
    }
    return s
  }
}

// ---------------------------------------------------------------- audio (R9)

/** `[offset, count, first, direction]`: output `out0 + offset + i` plays
 *  source sample `first + direction · i` (48 kHz, the FLAC sidecar's index). */
export type AudioRun = [number, number, number, number]

export interface AudioPlacement {
  clip: number
  /** Output samples [out0, out0 + n) at 48 kHz. */
  out0: number
  n: number
  /** First source sample of the clip at 1x (48 kHz source timeline). */
  src0: number
  /** Constant speed; a curve's MEAN speed; 0 for a freeze. */
  rate: number
  /** `curve`: source position follows the speed curve (speedCurve.ts
   *  `sourceSeconds`), no runs; `silence`: a freeze. */
  mode: 'exact' | 'varispeed' | 'tempo' | 'reverse' | 'curve' | 'silence'
  runs: AudioRun[]
  /** Samples at the head/tail mixed with the neighbour by acrossfade. */
  fadeIn: number
  fadeOut: number
}

const SR: Rational = { num: 1, den: 48000 }
const US_TB: Rational = { num: 1, den: 1_000_000 }

/** `_clip_sample0`: the seek's shift rounded into 1/48000, plus the
 *  pre-roll `atrim` rounded — two roundings, not round(t · 48 kHz). */
export function clipSample0(t: number, fps: FpsLike): number {
  const pre = seekPreroll(t, fps)
  const seek = Math.max(0, t - pre)
  const j0 = seek > 0 ? rescale(ffmpegMicros(seek), US_TB, SR) : 0
  return j0 + (pre > 1e-9 ? rescale(ffmpegMicros(pre), US_TB, SR) : 0)
}

function reversedRuns(c: EdlClip, src: SourceInfo | null, fps: FpsLike, n: number): AudioRun[] {
  const mFrames = reversedFrameCount(c, fps)
  const seg = reverseSegmentFrames(src ? src.w : 1920, src ? src.h : 1080, fps)
  const forward: Array<[number, number]> = []
  for (let j0 = 0; j0 < mFrames; j0 += seg) {
    const nf = Math.min(seg, mFrames - j0)
    const s0 = samplesForFrames(j0, fps)
    const cnt = samplesForFrames(j0 + nf, fps) - s0
    forward.push([clipSample0((c.in ?? 0) + timeOf(j0, fps), fps), cnt])
  }
  const runs: AudioRun[] = []
  let off = 0
  for (const [first, cnt] of forward.reverse()) {
    const take = Math.min(cnt, n - off)
    if (take <= 0) break
    runs.push([off, take, first + cnt - 1, -1])
    off += take
  }
  return runs
}

const acrossfadeSamples = (cost: number) => (cost > 0 ? rescale(ffmpegMicros(cost), US_TB, SR) : 0)

/** `audio_placements`: walk the v1 sound assembly (`_assemble_v1_audio`). */
export function audioPlacements(pm: ProgramMap, lookup?: SourceLookup): AudioPlacement[] {
  const out: AudioPlacement[] = []
  let cursor = 0
  let last = -1
  for (const seg of pm.segments) {
    const m = samplesForFrames(seg.frames, pm.R)
    const ov = acrossfadeSamples(seg.costBefore)
    const start = cursor - ov
    if (ov && last >= 0) out[last].fadeOut = ov
    if (seg.kind === 'clip') {
      const c = pm.clips[seg.clip]
      const sp = typeof c.speed === 'number' && c.speed > 0 && c.speed !== 1 ? c.speed : null
      const curve = curvePoints(c.speed)
      let mode: AudioPlacement['mode']
      let runs: AudioRun[]
      const src0 = clipSample0(c.in ?? 0, pm.R)
      if (freezeOf(c) !== null) {
        out.push({ clip: seg.clip, out0: start, n: m, src0, rate: 0, mode: 'silence', runs: [], fadeIn: ov, fadeOut: 0 })
        last = out.length - 1
        cursor = start + m
        continue
      }
      if (c.reverse) {
        mode = 'reverse'
        let src: SourceInfo | null
        try { src = lookup ? lookup(c.src) : null } catch { src = null }
        runs = sp === null && curve === null ? reversedRuns(c, src, pm.R, m) : []
      } else if (curve) {
        mode = 'curve'
        runs = []
      } else {
        mode = sp === null ? 'exact' : (c.audio?.keep_pitch ?? true) ? 'tempo' : 'varispeed'
        // Retimed sound is a resample: source position src0 + i·rate, no runs.
        runs = sp === null ? [[0, m, src0, 1]] : []
      }
      const rate = curve ? meanSpeed(curve) : sp ?? 1
      out.push({ clip: seg.clip, out0: start, n: m, src0, rate, mode, runs, fadeIn: ov, fadeOut: 0 })
      last = out.length - 1
    } else {
      last = -1
    }
    cursor = start + m
  }
  return out
}

/** The `frame_map_json` "audio" rows (snake_case, as the server sends). */
export function audioJson(pm: ProgramMap, lookup?: SourceLookup): Array<Record<string, unknown>> {
  return audioPlacements(pm, lookup).map((a) => ({
    clip_id: pm.clips[a.clip].id, src: pm.clips[a.clip].src, out0: a.out0, n: a.n, src0: a.src0,
    rate: a.rate, mode: a.mode, runs: a.runs, fade_in: a.fadeIn, fade_out: a.fadeOut,
  }))
}

/** `audio_total_samples`: the v1 sound's length. */
export function audioTotalSamples(pm: ProgramMap): number {
  let cursor = 0
  for (const seg of pm.segments) cursor += samplesForFrames(seg.frames, pm.R) - acrossfadeSamples(seg.costBefore)
  return cursor
}

// ---------------------------------------------------------------- RLE (§8)

export interface Seq { f0: number; step?: number; d?: string }

export interface Run {
  k0: number
  n: number
  kind: number
  clip_id?: string
  src?: string
  a?: Seq
  b_clip_id?: string
  b_src?: string
  b?: Seq
  p_den?: number
  p_j0?: number
  nested?: boolean
}

const B64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'

function b64encode(bytes: number[]): string {
  let out = ''
  for (let i = 0; i < bytes.length; i += 3) {
    const a = bytes[i], b = bytes[i + 1], c = bytes[i + 2]
    const n = (a << 16) | ((b ?? 0) << 8) | (c ?? 0)
    out += B64[(n >> 18) & 63] + B64[(n >> 12) & 63]
      + (b === undefined ? '=' : B64[(n >> 6) & 63]) + (c === undefined ? '=' : B64[n & 63])
  }
  return out
}

function b64decode(text: string): number[] {
  const out: number[] = []
  const clean = text.replace(/=+$/, '')
  let buf = 0, bits = 0
  for (const ch of clean) {
    buf = (buf << 6) | B64.indexOf(ch)
    bits += 6
    if (bits >= 8) { bits -= 8; out.push((buf >> bits) & 0xff) }
  }
  return out
}

function varints(vals: number[]): string {
  const bytes: number[] = []
  for (const v of vals) {
    let z = v >= 0 ? v * 2 : -v * 2 - 1
    for (;;) {
      const b = z % 128
      z = Math.floor(z / 128)
      if (z) bytes.push(b | 0x80)
      else { bytes.push(b); break }
    }
  }
  return b64encode(bytes)
}

function decodeVarints(text: string): number[] {
  const out: number[] = []
  let z = 0, mul = 1
  for (const b of b64decode(text)) {
    z += (b & 0x7f) * mul
    if (b & 0x80) { mul *= 128; continue }
    out.push(z % 2 === 0 ? z / 2 : -(z + 1) / 2)
    z = 0; mul = 1
  }
  return out
}

function encodeSeq(vals: ArrayLike<number>, k: number, e: number): Seq {
  const f0 = vals[k]
  if (e - k === 1) return { f0, step: 1 }
  const step = vals[k + 1] - vals[k]
  let linear = true
  for (let i = k + 1; i < e; i++) if (vals[i] - vals[i - 1] !== step) { linear = false; break }
  if (linear) return { f0, step }
  const deltas: number[] = []
  for (let i = k + 1; i < e; i++) deltas.push(vals[i] - vals[i - 1])
  return { f0, d: varints(deltas) }
}

export function decodeSeq(s: Seq, n: number): number[] {
  if (s.step !== undefined) return Array.from({ length: n }, (_, i) => s.f0 + s.step! * i)
  const out = [s.f0]
  for (const x of decodeVarints(s.d ?? '').slice(0, n - 1)) out.push(out[out.length - 1] + x)
  return out
}

/** `to_rle`: runs of maximal stretches with the same kind and clip(s). */
export function toRle(pm: ProgramMap, srcKey: (c: EdlClip) => string = (c) => c.src): Run[] {
  const runs: Run[] = []
  let k = 0
  while (k < pm.total) {
    const kd = pm.kind[k], ci = pm.clip[k], bi = pm.bClip[k], ns = pm.nested[k]
    let e = k + 1
    while (e < pm.total && pm.kind[e] === kd && pm.clip[e] === ci && pm.bClip[e] === bi
      && pm.nested[e] === ns
      && !(kd === KIND_BLEND && (pm.pDen[e] !== pm.pDen[k] || pm.pNum[e] !== pm.pNum[e - 1] - 1))) e++
    const run: Run = { k0: k, n: e - k, kind: kd }
    if (kd !== KIND_GAP) {
      const c = pm.clips[ci]
      run.clip_id = c.id
      run.src = srcKey(c)
      run.a = encodeSeq(pm.srcFrame, k, e)
    }
    if (kd === KIND_BLEND) {
      const b = pm.clips[bi]
      const den = pm.pDen[k]
      run.b_clip_id = b.id
      run.b_src = srcKey(b)
      run.b = encodeSeq(pm.bSrcFrame, k, e)
      run.p_den = den
      run.p_j0 = den ? den - pm.pNum[k] : 0
      run.nested = ns === 1
    }
    runs.push(run)
    k = e
  }
  return runs
}

export interface RleFrame {
  kind: number
  clipId: string | null
  src: string | null
  frame: number
  bClipId?: string
  bSrc?: string
  bFrame?: number
  p?: [number, number]
  nested?: boolean
}

/** `rle_frames`: expand runs to one entry per output frame. */
export function rleFrames(runs: Run[]): RleFrame[] {
  const out: RleFrame[] = []
  for (const run of runs) {
    const a = run.a ? decodeSeq(run.a, run.n) : new Array<number>(run.n).fill(-1)
    const b = run.b ? decodeSeq(run.b, run.n) : null
    for (let i = 0; i < run.n; i++) {
      const f: RleFrame = { kind: run.kind, clipId: run.clip_id ?? null, src: run.src ?? null, frame: a[i] }
      if (b) {
        const j = (run.p_j0 ?? 0) + i
        const den = run.p_den ?? 1
        Object.assign(f, {
          bClipId: run.b_clip_id, bSrc: run.b_src, bFrame: b[i],
          p: [den - j, den] as [number, number], nested: run.nested,
        })
      }
      out.push(f)
    }
  }
  return out
}

/** Frames where the map disagrees with a server RLE (R14, §8): the first
 *  `limit` mismatching k, O(total). An empty result means structural parity. */
export function compareWithRle(pm: ProgramMap, runs: Run[], limit = 16,
                               srcKey: (c: EdlClip) => string = (c) => c.src): number[] {
  const bad: number[] = []
  const frames = rleFrames(runs)
  const n = Math.max(frames.length, pm.total)
  for (let k = 0; k < n && bad.length < limit; k++) {
    const f = frames[k]
    if (!f || k >= pm.total) { bad.push(k); continue }
    const kd = pm.kind[k]
    let same = f.kind === kd && f.frame === (kd === KIND_GAP ? -1 : pm.srcFrame[k])
    if (same && kd !== KIND_GAP) same = f.src === srcKey(pm.clips[pm.clip[k]])
    if (same && kd === KIND_BLEND) {
      same = f.bFrame === pm.bSrcFrame[k] && f.bSrc === srcKey(pm.clips[pm.bClip[k]])
        && !!f.p && f.p[0] === pm.pNum[k] && f.p[1] === pm.pDen[k] && !!f.nested === (pm.nested[k] === 1)
    }
    if (!same) bad.push(k)
  }
  return bad
}

// ---------------------------------------------------------------- diff (§4.1)

export interface ProgramDiff {
  /** Half-open [k0, k1) output ranges whose frames changed. */
  dirtyFrames: Array<[number, number]>
  /** Clip ids that now own frames they did not own before (param rebinds). */
  dirtyParams: Set<string>
}

/** What changed between two maps. Frames are compared by SOURCE identity
 *  (src + frame, both sides of a blend, progress), so a split — the same
 *  frames under two clip ids — is `dirtyFrames = ∅` and only the right
 *  half's id lands in `dirtyParams`. */
export function diffPrograms(prev: ProgramMap | null, next: ProgramMap): ProgramDiff {
  const dirtyParams = new Set<string>()
  if (!prev) {
    next.clips.forEach((c) => dirtyParams.add(c.id))
    return { dirtyFrames: next.total ? [[0, next.total]] : [], dirtyParams }
  }
  const dirtyFrames: Array<[number, number]> = []
  const n = Math.max(prev.total, next.total)
  let open = -1
  const srcOf = (pm: ProgramMap, key: number) => (key >= 0 ? pm.sources[key] : null)
  for (let k = 0; k < n; k++) {
    let same = k < prev.total && k < next.total
    if (same) {
      same = prev.kind[k] === next.kind[k] && prev.srcFrame[k] === next.srcFrame[k]
        && srcOf(prev, prev.srcKey[k]) === srcOf(next, next.srcKey[k])
        && prev.bSrcFrame[k] === next.bSrcFrame[k]
        && srcOf(prev, prev.bSrcKey[k]) === srcOf(next, next.bSrcKey[k])
        && prev.pNum[k] === next.pNum[k] && prev.pDen[k] === next.pDen[k]
    }
    if (same) {
      const pc = prev.clip[k] >= 0 ? prev.clips[prev.clip[k]].id : null
      const nc = next.clip[k] >= 0 ? next.clips[next.clip[k]].id : null
      if (nc !== null && pc !== nc) dirtyParams.add(nc)
      const pb = prev.bClip[k] >= 0 ? prev.clips[prev.bClip[k]].id : null
      const nb = next.bClip[k] >= 0 ? next.clips[next.bClip[k]].id : null
      if (nb !== null && pb !== nb) dirtyParams.add(nb)
    }
    if (!same && open < 0) open = k
    if (same && open >= 0) { dirtyFrames.push([open, k]); open = -1 }
  }
  if (open >= 0) dirtyFrames.push([open, n])
  for (const [a, b] of dirtyFrames) {
    for (let k = a; k < Math.min(b, next.total); k++) {
      if (next.clip[k] >= 0) dirtyParams.add(next.clips[next.clip[k]].id)
      if (next.bClip[k] >= 0) dirtyParams.add(next.clips[next.bClip[k]].id)
    }
  }
  return { dirtyFrames, dirtyParams }
}
