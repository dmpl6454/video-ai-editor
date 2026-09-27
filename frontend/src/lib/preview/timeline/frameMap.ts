// Clip-local output frame → SOURCE frame (instant preview spec §6 R4) — a
// port of `render/frame_map.py`, which models the compositor's chain step by
// step in exact integers:
//
//   [-ss seek] -to/-t … setpts=PTS-STARTPTS [,setpts=PTS/speed] , fps=R ,
//   tpad=stop_mode=clone , trim=end_frame=n
//
// * the seek is half a PROJECT frame before `in` (`seek_preroll`), printed
//   %.6f; the demuxer shifts timestamps by it rounded into the stream time
//   base and the accurate-seek trim keeps pts >= 0;
// * `-to` (or `-t`) becomes a trim DURATION measured from the first kept frame;
// * `setpts=PTS/speed` divides in double and truncates;
// * `fps` rounds each pts half-away-from-zero into output slots and shows, in
//   slot s, the latest frame whose slot is <= s; at EOF it flushes up to the
//   rounded end timestamp, and tpad clones the last frame (a "freeze").
// * a reversed clip plays an intermediate built segment by segment;
// * a speed CURVE runs on the file clock anchored at `in` (wave D3,
//   speedCurve.ts "the v1 chain's clock": a 1/5 s-grid seek 0.5 s early, a
//   refined time base, the closed-form setpts of T = PTS·TB − in, then
//   fps=R:start_time=0), so a split piece shows its parent's frames; a
//   FREEZE holds the first frame of a 1x chain at `in`.
//
// Normative definition: tests/goldens/frame_map/*.json (real ffmpeg renders).

import {
  ffmpegMicros, frameDuration, frameOf, rateOf, rescale, seekPreroll, ticksPerFrame, timeOf,
  type FpsLike, type Rational,
} from './timebase'
import { clipFrames, clipIn, clipOut, freezeOf, reversedFrameCount, reversedView, type EdlClip } from './framePlan'
import {
  anchoredTicks, cRound, curveMap, curvePoints, curveRetimer, curveSeek, curveTimeBase, type CurveMap,
} from './speedCurve'

/** What the program map needs to know about one source (see frame_map.py). */
export interface SourceInfo {
  rate: Rational
  /** Stream time base (seconds per tick). */
  tb: Rational
  /** Decoded frame count. */
  frames: number
  /** First frame's pts minus the file start_time, in ticks. */
  startTicks: number
  w: number
  h: number
}

/** The JSON shape shared with `SourceInfo.to_json()` (frame_map.py). */
export interface SourceInfoJson {
  rate: [number, number]
  tb: [number, number]
  frames: number
  start_ticks?: number
  w?: number
  h?: number
}

export function sourceFromJson(d: SourceInfoJson): SourceInfo {
  return {
    rate: { num: d.rate[0], den: d.rate[1] }, tb: { num: d.tb[0], den: d.tb[1] },
    frames: d.frames, startTicks: d.start_ticks ?? 0, w: d.w ?? 1920, h: d.h ?? 1080,
  }
}

const US: Rational = { num: 1, den: 1_000_000 }

/** Ticks per frame, as {num, den} (an integer num/1 for every real file). */
function frameTicks(src: SourceInfo): Rational {
  // 1 / (rate · tb) = tb.den · rate.den / (tb.num · rate.num)
  return { num: src.tb.den * src.rate.den, den: src.tb.num * src.rate.num }
}

/** pts (ticks) of decoded frame `i`, CFR (extrapolated past the end). */
export function ptsOf(src: SourceInfo, i: number): number {
  const f = frameTicks(src)
  if (f.num % f.den === 0) return src.startTicks + i * (f.num / f.den)
  return src.startTicks + rescale(i, { num: 1, den: 1 }, { num: f.den, den: f.num })
}

/** The ffmpeg muxer's default stream time base for a CFR encode at `rate`. */
export function defaultTimeBase(rate: FpsLike): Rational {
  const r = rateOf(rate)
  let scale = r.num
  while (scale < 10000) scale *= 2
  return { num: 1, den: scale }
}

/** MSE ticks (timescale 240000) per project frame, or null (R1 refusal):
 *  timebase's one implementation, re-exported for the map's users. */
export { ticksPerFrame }

function speedDivisor(speed: EdlClip['speed']): number | null {
  if (typeof speed !== 'number' || !speed || speed <= 0 || speed === 1) return null
  return speed
}

function firstAtOrAfter(src: SourceInfo, ticks: number): number {
  const f = frameTicks(src)
  let i = Math.max(0, Math.floor(((ticks - src.startTicks) * f.den) / f.num) - 1)
  while (ptsOf(src, i) < ticks) i++
  while (i > 0 && ptsOf(src, i - 1) >= ticks) i--
  return i
}

/** `select_frames`: the source frame shown in each of `n` output slots.
 *  `curve` (a speed curve laid over the clip) replaces the constant retime. */
export function selectFrames(
  src: SourceInfo,
  opts: {
    seekUs: number | null; durUs: number; n: number; fps: FpsLike; speed?: EdlClip['speed']
    curve?: CurveMap | null
    /** `(seek, in)`: the curve runs on the v1 chain's in-anchored clock. */
    anchor?: readonly [number, number] | null
  },
): Int32Array {
  const { seekUs, durUs, n, fps } = opts
  const out = new Int32Array(Math.max(0, n))
  if (n <= 0) return out
  const last = src.frames - 1
  if (last < 0) return out
  const r = rateOf(fps)
  const outTb: Rational = { num: r.den, den: r.num }
  let f0 = 0
  let off = 0
  if (seekUs !== null) {
    off = rescale(-seekUs, US, src.tb)
    f0 = firstAtOrAfter(src, -off)
  }
  if (f0 > last) { out.fill(last); return out }
  const base = ptsOf(src, f0)
  const durTb = rescale(durUs, US, src.tb)
  let qlast = 0
  while (f0 + qlast + 1 <= last && ptsOf(src, f0 + qlast + 1) - base < durTb) qlast++
  const div = speedDivisor(opts.speed)
  let slotOf: (q: number) => number
  if (opts.curve && opts.anchor) {
    // edl/speed_curve.py "the v1 chain's clock": back on the file clock,
    // the refined time base (pts × k), T = PTS·TB − in.
    const cm = opts.curve
    const TB = src.tb.num / src.tb.den
    const [aSeek, aIn] = opts.anchor
    const back = (seekUs !== null ? off : 0) + (aSeek > 0 ? cRound(aSeek / TB) : 0)
    const tbc = curveTimeBase(src.tb, r.num)
    const k = (src.tb.num * tbc.den) / (src.tb.den * tbc.num)
    const TBc = tbc.num / tbc.den
    slotOf = (q: number) => rescale(anchoredTicks((ptsOf(src, f0 + q) + back) * k, TBc, aIn, cm), tbc, outTb)
  } else {
    const retime = opts.curve
      ? curveRetimer(opts.curve, src.tb)
      : (x: number) => (div !== null ? Math.trunc(x / div) : x)
    slotOf = (q: number) => rescale(retime(ptsOf(src, f0 + q) - base), src.tb, outTb)
  }
  const eof = slotOf(qlast + 1)
  let q = 0
  let nextSlot = qlast > 0 ? slotOf(1) : 0
  for (let s = 0; s < n; s++) {
    const lim = Math.min(s, eof - 1)
    while (q < qlast && nextSlot <= lim) {
      q++
      nextSlot = q < qlast ? slotOf(q + 1) : 0
    }
    out[s] = f0 + q
  }
  return out
}

/** `speed_curve_map`: the curve a clip chain retimes by, laid over
 *  `out − in` source seconds (null for a constant speed). */
export function speedCurveMap(speed: EdlClip['speed'], inS: number, outS: number): CurveMap | null {
  const pts = curvePoints(speed)
  return pts ? curveMap(pts, Math.max(0, outS - inS)) : null
}

/** `forward_clip_frames`: the clip chain opened with `clip_input_args` (a
 *  speed CURVE: the in-anchored chain, seeking at `curveSeek`). */
export function forwardClipFrames(
  src: SourceInfo, c: { in: number; out: number; speed?: EdlClip['speed'] }, n: number, fps: FpsLike,
): Int32Array {
  const curve = speedCurveMap(c.speed, c.in, c.out)
  const seek = curve ? curveSeek(c.in) : Math.max(0, c.in - seekPreroll(c.in, fps))
  const end = c.out + 2 * frameDuration(fps)
  const seekUs = seek > 0 ? ffmpegMicros(seek) : null
  const durUs = ffmpegMicros(end) - (seekUs ?? 0)
  return selectFrames(src, {
    seekUs, durUs, n, fps, speed: c.speed, curve, anchor: curve ? [seek, c.in] : null,
  })
}

/** `compositor.freeze_input_span`: a freeze's (seek, end). */
export function freezeInputSpan(inS: number, fps: FpsLike): [number, number] {
  const pre = seekPreroll(inS, fps)
  return [Math.max(0, inS - pre), inS + (1 + 2) * frameDuration(fps)]
}

/** `freeze_frame`: the source frame a freeze clip holds — the first output
 *  frame of a 1x chain opened at `in`. */
export function freezeFrame(src: SourceInfo, inS: number, fps: FpsLike): number {
  const [seek, end] = freezeInputSpan(inS, fps)
  const seekUs = seek > 0 ? ffmpegMicros(seek) : null
  const durUs = ffmpegMicros(end) - (seekUs ?? 0)
  return selectFrames(src, { seekUs, durUs, n: 1, fps })[0]
}

/** `freeze_in_for`: an `in` whose freeze holds source frame `frame` (checked
 *  with `freezeFrame`, never assumed) — what a freeze-frame op writes. */
export function freezeInFor(src: SourceInfo, frame: number, fps: FpsLike): number {
  const f = Math.max(0, Math.min(Math.trunc(frame), Math.max(0, src.frames - 1)))
  const base = ((ptsOf(src, f) - src.startTicks) * src.tb.num) / src.tb.den
  const fd = src.rate.den / src.rate.num
  const round6 = (t: number) => Math.round(t * 1e6) / 1e6
  for (const frac of [0, 0.25, 0.5, 0.75, -0.25, 0.1, 0.9]) {
    const t = round6(Math.max(0, base + frac * fd))
    if (freezeFrame(src, t, fps) === f) return t
  }
  return round6(Math.max(0, base))
}

/** `render/reverse.py` `_segment_frames`. */
export function reverseSegmentFrames(w: number, h: number, fps: FpsLike): number {
  const perFrame = Math.max(1, Math.trunc(w * h * 1.5))
  const byMem = Math.floor((384 * 1024 * 1024) / perFrame)
  const r = rateOf(fps)
  const byTime = Math.trunc(4 * (r.num / r.den))
  return Math.max(8, Math.min(byMem, byTime))
}

/** `reversed_intermediate`: source frame of each intermediate frame. */
export function reversedIntermediate(src: SourceInfo, inS: number, outS: number, fps: FpsLike): Int32Array {
  const m = Math.max(1, frameOf(outS - inS, fps))
  const seg = reverseSegmentFrames(src.w, src.h, fps)
  const forward = new Int32Array(m)
  for (let j0 = 0; j0 < m; j0 += seg) {
    const n = Math.min(seg, m - j0)
    const t0 = inS + timeOf(j0, fps)
    const pre = seekPreroll(t0, fps)
    const seek = Math.max(0, t0 - pre)
    const span = pre + timeOf(n + 2, fps)
    const seekUs = seek > 0 ? ffmpegMicros(seek) : null
    forward.set(selectFrames(src, { seekUs, durUs: ffmpegMicros(span), n, fps }), j0)
  }
  return forward.reverse()
}

/** The reversed intermediate as a source. */
export function intermediateSource(m: number, fps: FpsLike): SourceInfo {
  return { rate: rateOf(fps), tb: defaultTimeBase(fps), frames: m, startTicks: 0, w: 0, h: 0 }
}

/** `clip_frame_list`: source frame for each clip-local output frame of an
 *  ORIGINAL v1 clip (reversed or not). */
export function clipFrameList(c: EdlClip, src: SourceInfo, fps: FpsLike): Int32Array {
  if (freezeOf(c) !== null) return new Int32Array(clipFrames(c, fps)).fill(freezeFrame(src, clipIn(c), fps))
  if (c.reverse) {
    const inter = reversedIntermediate(src, clipIn(c), clipOut(c), fps)
    const view = reversedView(c, fps)
    const idx = forwardClipFrames(intermediateSource(inter.length, fps),
      { in: clipIn(view), out: clipOut(view), speed: view.speed }, clipFrames(view, fps), fps)
    return idx.map((i) => inter[i])
  }
  return forwardClipFrames(src, { in: clipIn(c), out: clipOut(c), speed: c.speed }, clipFrames(c, fps), fps)
}

export { reversedFrameCount }
