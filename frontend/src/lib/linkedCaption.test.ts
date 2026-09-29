// Final sweep 3 r2: a caption made from a voiceover / PIP's words names that
// clip (`linked_to`) and plays on its clock — the desktop's mirror of
// `render/clock.linked_text_windows`. Drawn through `render_time(start)`
// instead, every voiceover caption after a 1.0 s fade before it sat a word
// (1.0 s) left of the word the export plays.
import { describe, expect, it } from 'vitest'
import { drawnSpan, linkedTextSpans, renderSpanOf, timingClockOf, v1LayoutOf } from './timelineLayout'
import type { AnyClip, EDL } from '../types'

const media = (id: string, start: number, dur: number, src = `${id}.mp4`) =>
  ({ id, src, in: 0, out: dur, start, speed: 1 })

function edl(cues: AnyClip[], pip = false): EDL {
  return {
    canvas: { w: 320, h: 180, fps: 30 },
    duration: 11,
    tracks: [
      { id: 'v1', type: 'video', z: 0, clips: [media('A', 0, 4), media('B', 4, 4), media('C', 8, 4)],
        transitions: [{ at: 4, type: 'fade', duration: 1 }] },
      { id: 'vo', type: 'vo', z: 0, clips: [media('vo1', 2, 6, 'vo.wav')] },
      ...(pip ? [{ id: 'v2', type: 'video', z: 1, clips: [media('g', 6, 4, 'guest.mp4')] }] : []),
      { id: 'captions', type: 'captions', z: 13, clips: cues },
    ],
  } as unknown as EDL
}

const cue = (id: string, start: number, end: number, linked_to?: string): AnyClip =>
  ({ id, text: id, start, end, role: 'caption', ...(linked_to ? { linked_to } : {}) }) as AnyClip

describe('a caption linked to a voiceover', () => {
  it('plays where its word is heard, not at render_time(start)', () => {
    // vo1 at 2.0 is pulled by nothing (the seam is at 4.0): its word at
    // 4.5 on its own clock is heard at 4.5.
    const e = edl([cue('w2', 4.5, 5.4, 'vo1'), cue('plain', 4.5, 5.4)])
    const [linked, plain] = e.tracks[e.tracks.length - 1].clips
    expect(renderSpanOf(e, 'captions', linked).start).toBeCloseTo(4.5, 6)
    expect(renderSpanOf(e, 'captions', plain).start).toBeCloseTo(3.5, 6)    // the layout clock
    const span = drawnSpan('captions', linked, v1LayoutOf(e), 'captions')
    expect(span.start).toBeCloseTo(4.5, 6)
    expect(span.duration).toBeCloseTo(0.9, 6)
  })

  it('shows and decodes its timing on the voiceover clock', () => {
    const e = edl([cue('w2', 4.5, 5.4, 'vo1')])
    const c = e.tracks[e.tracks.length - 1].clips[0]
    const clock = timingClockOf(e, 'captions', c)
    expect(clock.show(4.5)).toBeCloseTo(4.5, 6)
    expect(clock.start(6.0)).toBeCloseTo(6.0, 6)
    expect(clock.end(6.0)).toBeCloseTo(6.0, 6)
  })

  it('stops with its clip and follows a PIP window', () => {
    // g at 6.0 is pulled by the 1.0 s fade: its window starts at 5.0.
    const e = edl([cue('gw', 6.5, 7.0, 'g'), cue('tail', 7.5, 9.0, 'vo1')], true)
    const spans = linkedTextSpans(e)
    expect(spans.get('gw')?.start).toBeCloseTo(5.5, 6)
    expect(spans.get('tail')?.end).toBeCloseTo(8.0, 6)      // vo1 ends at 8.0
  })

  it('falls back to the layout clock when its clip is gone', () => {
    const e = edl([cue('orphan', 4.5, 5.4, 'nope')])
    const c = e.tracks[e.tracks.length - 1].clips[0]
    expect(linkedTextSpans(e).has('orphan')).toBe(false)
    expect(renderSpanOf(e, 'captions', c).start).toBeCloseTo(3.5, 6)
  })
})
