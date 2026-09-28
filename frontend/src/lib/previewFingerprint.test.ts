// The background render's trigger (lib/previewFingerprint.ts). Final QA r3:
// a solo or a music-duck change is a change of SOUND the server preview must
// re-mix (its cheap audio remux) — and the Instant preview's loudness gain is
// keyed on the same sound (render/preview_loudness.audio_key), so without a
// render it stayed "not current" (≈ Loudness) until the next picture edit.
import { describe, expect, it } from 'vitest'
import type { EDL } from '../types'
import { videoFingerprintOf } from './previewFingerprint'

const base = (): EDL => ({
  canvas: { w: 1920, h: 1080, fps: 30, loudness_lufs: -16 },
  duration: 4,
  tracks: [
    { id: 'v1', type: 'video', clips: [{ id: 'a', src: '/x.mp4', start: 0, in: 0, out: 4 }] },
    { id: 'music', type: 'music', clips: [{ id: 'm', src: '/m.mp3', start: 0, in: 0, out: 4 }] },
    { id: 't1', type: 'text', clips: [{ id: 't', text: 'Hi', start: 0, end: 1 }] },
  ],
} as unknown as EDL)

describe('videoFingerprintOf', () => {
  it('changes for a solo or a duck (sound-only track changes)', () => {
    const e0 = base()
    const solo = base()
    ;(solo.tracks[1] as unknown as { solo: boolean }).solo = true
    const duck = base()
    ;(duck.tracks[1] as unknown as { duck: unknown }).duck = { to_db: -18 }
    expect(videoFingerprintOf(solo)).not.toBe(videoFingerprintOf(e0))
    expect(videoFingerprintOf(duck)).not.toBe(videoFingerprintOf(e0))
  })
  it('does not change for a title (drawn by the client)', () => {
    const t = base()
    ;(t.tracks[2].clips[0] as unknown as { text: string }).text = 'Hello'
    expect(videoFingerprintOf(t)).toBe(videoFingerprintOf(base()))
  })
})
