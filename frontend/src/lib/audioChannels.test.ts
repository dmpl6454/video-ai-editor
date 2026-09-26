// QA-122: what the timeline draws for a clip's channel mode, and when the
// inspector calls a source one-sided. The peaks mirror render/waveform.py's
// `peaks` / `peaks_l` / `peaks_r` for a left-only camera file.
import { describe, expect, it } from 'vitest'
import { CHANNEL_MODES, KEEP_PITCH_TITLE, channelColumns, channelMode, oneSidedSource } from './audioChannels'

const LEFT_ONLY = { peaks: [0.3, 0.3, 0.3, 0.3], peaks_per_sec: 2, peaks_l: [0.3, 0.3, 0.3, 0.3], peaks_r: [0, 0, 0, 0] }
const STEREO = { peaks: [0.5, 0.4], peaks_per_sec: 1, peaks_l: [0.5, 0.2], peaks_r: [0.3, 0.4] }

describe('channelColumns (the waveform halves)', () => {
  it('shows a left-only recording as HALF a waveform in stereo', () => {
    expect(channelColumns(LEFT_ONLY, 0, 1, 'stereo')).toEqual({ top: 0.3, bottom: 0 })
  })
  it('draws the channel the render puts on each side', () => {
    expect(channelColumns(LEFT_ONLY, 0, 1, 'left')).toEqual({ top: 0.3, bottom: 0.3 })
    expect(channelColumns(LEFT_ONLY, 0, 1, 'right')).toEqual({ top: 0, bottom: 0 })
    expect(channelColumns(LEFT_ONLY, 0, 1, 'mono')).toEqual({ top: 0.15, bottom: 0.15 })   // (L+R)/2: −6 dB
    expect(channelColumns(STEREO, 0, 2, 'stereo')).toEqual({ top: 0.5, bottom: 0.4 })
    expect(channelColumns(STEREO, 1, 2, 'stereo')).toEqual({ top: 0.2, bottom: 0.4 })
  })
  it('a source without per-side peaks (mono, or an old cache) draws its peak on both halves', () => {
    expect(channelColumns({ peaks: [0.7], peaks_per_sec: 1 }, 0, 1, 'stereo')).toEqual({ top: 0.7, bottom: 0.7 })
  })
})

describe('oneSidedSource (the inspector notice)', () => {
  it('names the side a one-sided recording is on', () => {
    expect(oneSidedSource(LEFT_ONLY)).toBe('left')
    expect(oneSidedSource({ ...LEFT_ONLY, peaks_l: LEFT_ONLY.peaks_r, peaks_r: LEFT_ONLY.peaks_l })).toBe('right')
  })
  it('stays quiet for real stereo, silence and mono sources', () => {
    expect(oneSidedSource(STEREO)).toBeNull()
    expect(oneSidedSource({ peaks: [0], peaks_per_sec: 1, peaks_l: [0], peaks_r: [0] })).toBeNull()
    expect(oneSidedSource({ peaks: [0.5], peaks_per_sec: 1 })).toBeNull()
    // a hard-panned but present right side (−20 dB) is a mix, not a dead input
    expect(oneSidedSource({ peaks: [0.5], peaks_per_sec: 1, peaks_l: [0.5], peaks_r: [0.05] })).toBeNull()
  })
  it('handles a 30-minute source (90k peaks per side) without a spread overflow', () => {
    const n = 200_000
    const l = new Array(n).fill(0.2)
    expect(oneSidedSource({ peaks: l, peaks_per_sec: 50, peaks_l: l, peaks_r: new Array(n).fill(0) })).toBe('left')
  })
})

describe('channel modes and the keep-pitch note', () => {
  it('offers the four modes the renderer knows (edl/schema.py AudioProps.channels)', () => {
    expect(CHANNEL_MODES.map((m) => m.value)).toEqual(['stereo', 'left', 'right', 'mono'])
    expect(channelMode(undefined)).toBe('stereo')
    expect(channelMode({ channels: 'left' })).toBe('left')
    expect(channelMode({ channels: 'bogus' })).toBe('stereo')
  })
  it('the Keep pitch tooltip states the measured limit (QA-039)', () => {
    // Measured up to −19.8 ms (tests/test_c5_render_audio.py): ±15 was an understatement.
    expect(KEEP_PITCH_TITLE).toMatch(/up to ±20 ms/)
    expect(KEEP_PITCH_TITLE).not.toMatch(/±1[0-9] ms/)
  })
})
