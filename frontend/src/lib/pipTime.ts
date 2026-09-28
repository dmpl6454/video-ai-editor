// Which SOURCE instant a PIP (v2+ overlay) shows at a clip-local timeline
// instant — speed, speed curves, freeze and reverse (wave D3, lane E2).
//
// render/pip.py retimes a PIP with v1's own rule (`pip_retime`: `setpts=PTS/s`
// or the curve's closed-form integral between the rebase and the project-grid
// `fps=`), so output slot j shows the LAST source frame whose retimed time is
// at or before the slot's MIDDLE, (j + ½)/fps. Asking the hidden <video> for
// the source instant of that middle therefore decodes the export's frame for
// every speed shape; the continuous map (no snapping) drives playback.
//
//   forward  source = in + S(t)               S = speed·t, or the curve's
//                                             integral (speedCurve.sourceSeconds)
//   reverse  the reversed intermediate (render/reverse.py) holds M grid
//            frames backwards from source time `first` (`intermediateSpan`:
//            the range's own, or a curve's source-grid [floor(in),
//            ceil(out))), so intermediate frame i is grid frame M−1−i:
//            source = first + (M − 1 − i + ½)/fps with
//            i = ⌊fps · (vIn + S((j + ½)/fps))⌋ (`reversedViewRange`)
//   freeze   the first frame a 1x chain shows at `in` (frame_map.freeze_frame):
//            in + ½/fps, for the whole hold
//
// "At or before" is STRICTLY before on a tie: `fps=` rounds a pts to the
// nearest output tick with ties AWAY from zero, so a source frame whose
// retimed time is exactly a slot's middle belongs to the NEXT slot (2x from
// a 30 fps source on a 30 fps project: slot 0 is `in`'s frame, not the one
// after). Every instant is therefore taken TIE_S before the exact value.
//
// Pure: no DOM, so vitest pins it against the Python model's numbers.

import {
  clipIn, clipOut, freezeOf, intermediateSpan, reversedViewOut, reversedViewRange, type EdlClip,
} from './preview/timeline/framePlan'
import { curveMap, curvePoints, sourceSeconds, speedAt, type CurveMap } from './preview/timeline/speedCurve'
import { reverseSegmentFrames } from './preview/timeline/frameMap'
import { seekPreroll, timeOf, type FpsLike } from './preview/timeline/timebase'

/** How far below an exact tie a frame instant is taken (1 µs: far below
 *  any frame at <= 240 fps, far above double rounding). */
const TIE_S = 1e-6

export type PipTimingKind = 'forward' | 'reverse' | 'freeze'

export interface PipTiming {
  kind: PipTimingKind
  /** Source seconds of the frame the EXPORT shows in the output slot under
   *  the playhead (mid-frame, so a seek decodes exactly that frame). */
  frameTime: number
  /** Continuous source seconds at the playhead (playback). */
  time: number
  /** Source seconds per timeline second at the playhead: the speed there
   *  (a curve's instantaneous speed), 0 for a freeze, NEGATIVE reversed. */
  rate: number
}

/** What the caller may know about the PIP's SOURCE (the media library's
 *  proxy probe). With its frame rate a reversed PIP whose source rate is not
 *  the project's finds its frames exactly: each intermediate segment's
 *  `fps=` grid starts at the first SOURCE frame its seek keeps, not at the
 *  segment's project-grid time (render/reverse.py `_render_segment`). */
export interface PipSourceHint {
  rate?: { num: number; den: number } | null
  w?: number | null
  h?: number | null
}

/** Source seconds (a mid-slot instant, less the tie margin) of the
 *  intermediate's FORWARD frame `k`, the intermediate starting at source
 *  time `first` (`intermediateSpan`). */
function intermediateFrameTime(first: number, k: number, fps: FpsLike, hint?: PipSourceHint): number {
  const fd = timeOf(1, fps)
  const rate = hint?.rate
  if (!rate || !(rate.num > 0) || !(rate.den > 0)) return first + k * fd + fd / 2 - TIE_S
  const seg = reverseSegmentFrames(hint?.w || 1920, hint?.h || 1080, fps)
  const j0 = Math.floor(k / seg) * seg
  const t0 = first + timeOf(j0, fps)
  const rs = rate.num / rate.den
  // The first source frame at or after the segment's seek (half a project
  // frame before t0): the segment's `setpts=PTS-STARTPTS` + `fps=` grid
  // origin.
  const pFirst = Math.ceil((t0 - seekPreroll(t0, fps)) * rs - 1e-6) / rs
  return pFirst + timeOf(k - j0, fps) + fd / 2 - TIE_S
}

interface Retime { offset: (t: number) => number; speedAt: (t: number) => number }

function retimeOf(c: EdlClip, sourceSpan: number): Retime {
  const pts = curvePoints(c.speed)
  const cm: CurveMap | null = pts ? curveMap(pts, sourceSpan) : null
  if (cm) return { offset: (t) => sourceSeconds(cm, t), speedAt: (t) => speedAt(cm, t) }
  const s = typeof c.speed === 'number' && c.speed > 0 ? c.speed : 1
  return { offset: (t) => Math.max(0, t) * s, speedAt: () => s }
}

/** Whether a PIP's picture is anything but its source at 1x forwards. */
export function pipIsRetimed(c: EdlClip): boolean {
  if (freezeOf(c) !== null || c.reverse) return true
  if (curvePoints(c.speed)) return true
  return typeof c.speed === 'number' && c.speed > 0 && c.speed !== 1
}

/** The source instant PIP `c` shows at clip-local timeline seconds `local`
 *  (render clock: playhead − render_time(start)), on a project at `fps`. */
export function pipTiming(c: EdlClip, local: number, fps: FpsLike, hint?: PipSourceHint): PipTiming {
  const inP = clipIn(c)
  const fd = timeOf(1, fps)
  const t = Math.max(0, local)
  if (freezeOf(c) !== null) {
    const at = inP + fd / 2
    return { kind: 'freeze', frameTime: at, time: at, rate: 0 }
  }
  // The output slot SHOWING at `local` (slot j spans [j, j+1) frames); the
  // playhead normally sits on a boundary, where this is its own frame.
  const j = Math.floor(t / fd + 1e-6)
  const mid = timeOf(j, fps) + fd / 2
  if (c.reverse) {
    // The retime runs over the INTERMEDIATE, opened at the view's `in` (0,
    // or where `out` sits on a curve's source-grid intermediate).
    const [first, m] = intermediateSpan(c, fps)
    const [vIn] = reversedViewRange(c, fps)
    const rt = retimeOf(c, reversedViewOut(c))
    const i = Math.min(m - 1, Math.floor((vIn + rt.offset(mid) - TIE_S) / fd))
    const top = first + timeOf(m, fps) - vIn
    return {
      kind: 'reverse',
      // forward frame m−1−i of the intermediate: its own middle, less the tie margin
      frameTime: intermediateFrameTime(first, m - 1 - i, fps, hint),
      time: Math.max(inP, top - rt.offset(t)),
      rate: -rt.speedAt(t),
    }
  }
  const rt = retimeOf(c, Math.max(0, clipOut(c) - inP))
  return { kind: 'forward', frameTime: inP + rt.offset(mid) - TIE_S, time: inP + rt.offset(t), rate: rt.speedAt(t) }
}
