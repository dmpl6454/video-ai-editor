// Which frames the WebCodecs scrubber must decode, and which single frame it
// must paint, for a given seek target.
//
// Pure and separate from FrameScrubber.tsx because the rule is subtle in one
// specific way — mp4 sample tables are in DECODE order while `cts` is
// COMPOSITION time, and with B-frames those are different sequences — and
// getting it wrong is invisible in code review but immediately visible on
// screen: the canvas paints one frame and the <video> underneath shows
// another, so the picture jumps the moment the canvas hands back.

export interface WalkSample {
  /** composition (presentation) timestamp, microseconds */
  cts: number
  /** microseconds */
  duration: number
  is_sync: boolean
}

export interface FrameChain {
  /** Keyframe sample index to start decoding from. */
  startIdx: number
  /** Last sample, in DECODE order, that must be fed to the decoder. */
  lastIdx: number
  /** Composition timestamp of the ONE frame that should be painted. */
  paintUs: number
  /** Greatest cts+duration among the frames at or before the target — used to
   *  clamp a target that lies past the end of the media. */
  maxEndUs: number
}

/** Latest keyframe whose cts ≤ target (index into `samples`). */
export function keyframeAtOrBefore(
  samples: WalkSample[], keyIdx: number[], targetUs: number,
): number {
  let lo = 0, hi = keyIdx.length - 1, best = 0
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    if (samples[keyIdx[mid]].cts <= targetUs) { best = mid; lo = mid + 1 }
    else hi = mid - 1
  }
  return keyIdx.length ? keyIdx[best] : 0
}

/** How far past the target to keep scanning for a smaller cts. Only B-frame
 *  reordering can put one there, and it is bounded by a few frames — 16 is
 *  well past any real encoder's reorder depth while keeping the scan O(1). */
const REORDER_FRAMES = 16
const DEFAULT_FRAME_US = 33333

export function frameChainFor(
  samples: WalkSample[], keyIdx: number[], targetUs: number,
): FrameChain {
  if (!samples.length) return { startIdx: 0, lastIdx: -1, paintUs: -1, maxEndUs: 0 }
  const startIdx = keyframeAtOrBefore(samples, keyIdx, targetUs)
  const reorderUs = Math.max(1, samples[startIdx].duration || DEFAULT_FRAME_US) * REORDER_FRAMES

  // The frame to PAINT is the one with the greatest cts ≤ target: that is the
  // frame whose interval covers the target, and it is what `<video>` shows for
  // the same `currentTime`. The old rule took the first sample whose cts
  // PASSED the target, which in an I P B B P B B stream is the P frame that
  // displays two or three frames later — and it stopped before feeding the B
  // frames that actually cover the target.
  let lastIdx = startIdx
  let maxEndUs = 0
  for (let i = startIdx; i < samples.length; i++) {
    const s = samples[i]
    if (s.cts <= targetUs) {
      if (s.cts >= samples[lastIdx].cts) lastIdx = i
      maxEndUs = Math.max(maxEndUs, s.cts + s.duration)
    } else if (s.cts > targetUs + reorderUs) {
      // Far enough past that no later sample can still compose at/below the
      // target. Without this the scan would run to the end of the file.
      break
    }
  }
  // Everything from the keyframe up to that sample gets fed, in decode order:
  // the ones in that span whose cts is ABOVE the target are references the
  // covering frame depends on — they must be decoded, they just must not be
  // painted. (Decode order guarantees every dependency sits at a lower index,
  // so this span is always sufficient.)
  return { startIdx, lastIdx, paintUs: samples[lastIdx].cts, maxEndUs }
}
