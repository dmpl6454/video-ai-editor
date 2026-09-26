// What the timeline draws inside an audio-bearing clip (QA-081).
//
// The waveform used to be the raw source peaks sampled ONE peak per pixel
// column: zoomed out, a column spanning twenty peaks showed whichever one its
// left edge hit (so a burst between samples vanished), and nothing about the
// clip's own audio reached the drawing — a +6 dB clip, a fade and a muted clip
// all looked identical to the untouched source. Now each column is the MAX of
// every peak it covers, scaled by the gain the renderer applies at that instant
// (gain_db plus its gain_env automation, times the linear fade ramps `afade` uses), and a
// muted clip or lane is drawn greyed rather than at full strength.

import { levelAt, type ClipAudioProps } from './audioLevel'

/** `peaks_l`/`peaks_r`: each side's peaks for a 2+ channel source (QA-122 —
 *  lib/audioChannels draws the top half from L and the bottom from R). */
export interface WaveData { peaks: number[]; peaks_per_sec: number; duration: number; peaks_l?: number[]; peaks_r?: number[] }

/** The clip's audio props as the EDL stores them — ONE level model with the
 *  inspector (lib/audioLevel): `gain_db` is a scalar trim and volume
 *  automation lives in `gain_env` (dB offsets added to it). The waveform used
 *  to sample `gain_db` as keyframes, a shape the backend cannot produce, so
 *  volume keys never reached the drawing. */
export type ClipAudio = ClipAudioProps

/** Max peak over SOURCE seconds [t0, t1): every peak the column covers, and at
 *  least the one under t0. 0 when the span is outside the data. */
export function columnPeak(w: WaveData, t0: number, t1: number): number {
  const n = w.peaks.length
  if (!n || !(w.peaks_per_sec > 0)) return 0
  const i0 = Math.floor(t0 * w.peaks_per_sec)
  const i1 = Math.max(i0 + 1, Math.ceil(t1 * w.peaks_per_sec))
  let m = 0
  for (let i = Math.max(0, i0); i < Math.min(n, i1); i++) {
    const p = w.peaks[i]
    if (p > m) m = p
  }
  return m
}

/** Linear gain the render applies at clip-local TIMELINE time `lt` of a clip
 *  that lasts `effDur` timeline seconds — gain_db (keyframes sampled in the
 *  same clip-local time) times `afade`'s linear in/out ramps. Mute is NOT
 *  folded in: the caller greys a muted clip instead of flattening it. */
export function clipGainAt(audio: ClipAudio | undefined, lt: number, effDur: number): number {
  if (!audio) return 1
  const db = levelAt(audio, lt)
  let g = Math.pow(10, db / 20)
  const fi = audio.fade_in ?? 0
  const fo = audio.fade_out ?? 0
  if (fi > 0.001 && lt < fi) g *= Math.max(0, lt / fi)
  if (fo > 0.001 && lt > effDur - fo) g *= Math.max(0, (effDur - lt) / fo)
  return g
}

export interface WaveColumn {
  /** Half-height in px (0..halfH). */
  h: number
  /** The scaled peak exceeds full scale — the renderer's limiter will act. */
  clipped: boolean
}

/** One column of the drawn waveform: the covered peak scaled by the gain. */
export function waveColumn(peak: number, gain: number, halfH: number): WaveColumn {
  const v = peak * gain
  return { h: Math.max(0.5, Math.min(1, v) * halfH), clipped: v > 1.0 }
}
