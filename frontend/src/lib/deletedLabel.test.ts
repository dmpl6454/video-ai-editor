// Naming what a delete actually removed.
//
// Reported as: "When I remove or delete emoji or text, it says 'clip deleted'
// even though they are not clips... this should be specific for all layers, and
// this should work for all the other layers." Calling a sticker a clip makes the
// confirmation read like it removed something else — worst on an emoji, where
// the word matches nothing the user can see on screen.

import { describe, expect, it } from 'vitest'
import { deletedLabel } from './deletedLabel'
import type { EDL } from '../types'

const edl = (tracks: Array<{ type: string; ids: string[] }>): EDL => ({
  version: 2,
  canvas: { w: 1080, h: 1920, fps: 30 },
  duration: 10,
  tracks: tracks.map((t, i) => ({
    id: `t${i}`, type: t.type, z: i,
    clips: t.ids.map((id) => ({ id })),
  })),
} as unknown as EDL)

describe('deletedLabel', () => {
  it('names each layer in the singular', () => {
    const cases: Array<[string, string]> = [
      ['sticker', 'Sticker deleted'],
      ['captions', 'Caption deleted'],
      ['text', 'Text deleted'],
      ['music', 'Music clip deleted'],
      ['vo', 'Voiceover deleted'],
      ['audio', 'Audio clip deleted'],
      ['video', 'Clip deleted'],
    ]
    for (const [type, want] of cases) {
      expect(deletedLabel(edl([{ type, ids: ['a'] }]), ['a'])).toBe(want)
    }
  })

  it('pluralises a multi-delete of one kind', () => {
    expect(deletedLabel(edl([{ type: 'sticker', ids: ['a', 'b', 'c'] }]), ['a', 'b', 'c']))
      .toBe('3 stickers deleted')
    expect(deletedLabel(edl([{ type: 'video', ids: ['a', 'b'] }]), ['a', 'b']))
      .toBe('2 clips deleted')
  })

  it('stays generic for a MIXED selection', () => {
    // No single honest noun covers a sticker plus a video clip, and naming
    // whichever was found first would be wrong half the time.
    const e = edl([{ type: 'sticker', ids: ['a'] }, { type: 'video', ids: ['b'] }])
    expect(deletedLabel(e, ['a', 'b'])).toBe('2 items deleted')
  })

  it('falls back rather than throwing on an unknown or missing track type', () => {
    // A lane added later must degrade to the old wording, not crash a delete.
    expect(deletedLabel(edl([{ type: 'something_new', ids: ['a'] }]), ['a']))
      .toBe('Clip deleted')
  })

  it('survives a null EDL and an empty id list', () => {
    // The label is resolved before the request; a delete dispatched before the
    // first EDL load must still produce a toast.
    expect(deletedLabel(null, ['a'])).toBe('Clip deleted')
    expect(deletedLabel(null, ['a', 'b'])).toBe('2 clips deleted')
    expect(deletedLabel(edl([{ type: 'sticker', ids: ['a'] }]), [])).toBe('Clip deleted')
  })

  it('ignores ids that are not on the timeline', () => {
    // A stale id (already deleted, or from another session) must not make a
    // real sticker delete report as mixed.
    const e = edl([{ type: 'sticker', ids: ['a'] }])
    expect(deletedLabel(e, ['a'])).toBe('Sticker deleted')
  })
})
