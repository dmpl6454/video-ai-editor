// A clip's channel mode, and what the waveform draws for it (QA-122).
//
// A camera recording on one input (a lav into the left jack) exported
// one-sided — L −15 dB, R −240 dB — and nothing in the editor showed it: the
// waveform was the max across channels, so a left-only file looked exactly
// like a stereo one. The backend now returns each side's peaks
// (`peaks_l`/`peaks_r`, render/waveform.py) and every clip has a channel mode
// (`audio.channels`, rendered by render/audio_mix.channel_filter):
//
//   stereo — as recorded;  left / right — that side on both;  mono — L+R folded.
//
// The timeline draws the waveform's TOP half from what the render will put on
// the left and the BOTTOM half from the right, so a one-sided source reads as
// a half-waveform until its mode fills it.

import { columnPeak } from './waveformDraw'

export type ChannelMode = 'stereo' | 'left' | 'right' | 'mono'

export const CHANNEL_MODES: readonly { value: ChannelMode; label: string; title: string }[] = [
  { value: 'stereo', label: 'Stereo', title: 'Play the left and right channels as they were recorded' },
  { value: 'left', label: 'Left to both', title: 'Put the left channel on both sides — for sound recorded on the left input only' },
  { value: 'right', label: 'Right to both', title: 'Put the right channel on both sides — for sound recorded on the right input only' },
  { value: 'mono', label: 'Mono mix', title: 'Fold left and right together and play the result on both sides' },
]

export function channelMode(audio: { channels?: string | null } | undefined): ChannelMode {
  const m = audio?.channels
  return m === 'left' || m === 'right' || m === 'mono' ? m : 'stereo'
}

/** The per-side peaks the waveform endpoint returns for a 2+ channel source. */
export interface SidedWave {
  peaks: number[]
  peaks_per_sec: number
  peaks_l?: number[]
  peaks_r?: number[]
}

/** Max peak of one peak array over source seconds [t0, t1) — lib/waveformDraw's
 *  column rule, on one side. */
function spanMax(arr: number[], pps: number, t0: number, t1: number): number {
  return columnPeak({ peaks: arr, peaks_per_sec: pps, duration: 0 }, t0, t1)
}

/** Peaks (0..1) the render puts on the LEFT (`top`) and RIGHT (`bottom`)
 *  over source seconds [t0, t1) under `mode`. A source without per-side data
 *  (mono, or a cached pre-wave-C response) draws its combined peak on both. */
export function channelColumns(w: SidedWave, t0: number, t1: number, mode: ChannelMode): { top: number; bottom: number } {
  if (!w.peaks_l || !w.peaks_r) {
    const p = spanMax(w.peaks, w.peaks_per_sec, t0, t1)
    return { top: p, bottom: p }
  }
  const l = spanMax(w.peaks_l, w.peaks_per_sec, t0, t1)
  const r = spanMax(w.peaks_r, w.peaks_per_sec, t0, t1)
  switch (mode) {
    case 'left': return { top: l, bottom: l }
    case 'right': return { top: r, bottom: r }
    // An upper bound of |(L+R)/2| from the two sides' peaks.
    case 'mono': return { top: (l + r) / 2, bottom: (l + r) / 2 }
    default: return { top: l, bottom: r }
  }
}

/** A side at least this far below the other (dB) counts as empty. */
const ONE_SIDED_DB = 30
/** …and the loud side must carry real sound (≈ −46 dBFS peak). */
const AUDIBLE_PEAK = 0.005

/** 'left' / 'right' when the source's sound is on that side only — what the
 *  inspector offers to fill both sides from — else null. */
export function oneSidedSource(w: SidedWave | null | undefined): 'left' | 'right' | null {
  if (!w?.peaks_l || !w.peaks_r) return null
  // A loop, not Math.max(...arr): a 30-min source is 90k peaks per side.
  const peakOf = (arr: number[]) => { let m = 0; for (const p of arr) if (p > m) m = p; return m }
  const l = peakOf(w.peaks_l)
  const r = peakOf(w.peaks_r)
  const ratio = Math.pow(10, -ONE_SIDED_DB / 20)
  if (l >= AUDIBLE_PEAK && r <= l * ratio) return 'left'
  if (r >= AUDIBLE_PEAK && l <= r * ratio) return 'right'
  return null
}

/** The Keep pitch control's tooltip (QA-039). The render binary has no
 *  transient-exact time-stretcher (no rubberband), so keep-pitch timing is
 *  ffmpeg's atempo. The numbers are measured on a click track (0.5x-2x, v1
 *  and the audio lanes): every transient within ±20 ms (worst seen −19.8 ms,
 *  a music lane at 0.5x; the clip's first sound 20 ms late), and at 2x a very
 *  short click can be dropped. The backend states the same bound as
 *  `render/audio_mix.KEEP_PITCH_MAX_OFFSET_MS` and its tests measure it. */
export const KEEP_PITCH_TITLE =
  'On: the sound keeps its pitch (time-stretched). Claps and consonants can land up to ±20 ms off '
  + 'their frame — this build has no transient-exact stretcher. '
  + 'Off: tape-style speed change — every sound stays exactly on its frame, and the pitch rises or '
  + 'falls with the speed.'
