// Inspector > Framing's one-line status. It said "Letterboxed — bars
// top/bottom." for every contain-fit clip — a 1080x1920 clip in a 16:9
// project (bars left and right) and a 1920x1080 clip in a 1920x1080 project
// (no bars at all) alike (final QA, editor-ux).
import { describe, expect, it } from 'vitest'
import { framingNote } from './framingNote'

const HD = { w: 1920, h: 1080 }

describe('framingNote', () => {
  it('names side bars for a vertical clip in a landscape frame', () => {
    expect(framingNote(false, { w: 1080, h: 1920 }, HD)).toBe('Pillarboxed — bars left/right.')
  })
  it('says there are no bars when the clip has the frame\'s shape', () => {
    expect(framingNote(false, { w: 1920, h: 1080 }, HD)).toBe('Fills the frame — no bars.')
    // Within a pixel: 3840x2161 still fills 1920x1080.
    expect(framingNote(false, { w: 3840, h: 2161 }, HD)).toBe('Fills the frame — no bars.')
  })
  it('names top/bottom bars for a wider clip', () => {
    expect(framingNote(false, { w: 2560, h: 1080 }, HD)).toBe('Letterboxed — bars top/bottom.')
    expect(framingNote(false, { w: 1920, h: 1080 }, { w: 1080, h: 1920 })).toBe('Letterboxed — bars top/bottom.')
  })
  it('counts the clip\'s scale', () => {
    expect(framingNote(false, { w: 1920, h: 1080 }, HD, 0.5)).toBe('Smaller than the frame — bars all round.')
    // A vertical clip zoomed past the frame's width fills it.
    expect(framingNote(false, { w: 1080, h: 1920 }, HD, 3.2)).toBe('Fills the frame — no bars.')
  })
  it('keeps the cover text, and stays neutral while the size is unknown', () => {
    expect(framingNote(true, { w: 1080, h: 1920 }, HD)).toBe('Filling the frame (cropped).')
    expect(framingNote(false, null, HD)).toBe('Fitted inside the frame.')
  })
})
