// The bug these cover: an emoji typed into a TEXT clip left a permanent empty
// gap in the preview. The artwork downloads fine — it just arrives AFTER the
// frame that asked for it, and TextLayer only repaints when the playhead moves,
// so on a paused preview nothing ever painted it. The arrival has to be
// observable, which is what emojiGeneration() is for.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { codepointSeq, emojiGeneration, emojiImage } from './emojiArt'

// Minimal stand-in for the browser's Image: records the src it was pointed at
// and lets the test decide when (and whether) the load lands.
class FakeImage {
  static made: FakeImage[] = []
  onload: (() => void) | null = null
  onerror: (() => void) | null = null
  private _src = ''
  get src() { return this._src }
  set src(v: string) { this._src = v; FakeImage.made.push(this) }
}

beforeEach(() => {
  FakeImage.made = []
  vi.stubGlobal('Image', FakeImage)
})

// Each test uses its OWN cluster string: the cache is module-level and
// deliberately has no reset hatch (production never wants one).
describe('codepointSeq', () => {
  it('lowercases hex and joins multi-codepoint clusters', () => {
    expect(codepointSeq('\u{1F60A}')).toBe('1f60a')
    expect(codepointSeq('\u{1F1EE}\u{1F1F3}')).toBe('1f1ee-1f1f3')
    expect(codepointSeq('\u{2764}\u{FE0F}')).toBe('2764-fe0f')
  })
})

describe('emojiImage', () => {
  it('returns null while loading and requests the artwork route once', () => {
    expect(emojiImage('\u{1F525}')).toBeNull()
    expect(emojiImage('\u{1F525}')).toBeNull()
    expect(FakeImage.made.map((i) => i.src)).toEqual(['/api/emoji/1f525.png'])
  })

  it('bumps the generation on load, so a paused layer knows to repaint', () => {
    const before = emojiGeneration()
    expect(emojiImage('\u{1F602}')).toBeNull()
    expect(emojiGeneration()).toBe(before)      // nothing has arrived yet
    FakeImage.made[0].onload!()
    expect(emojiGeneration()).toBe(before + 1)  // <- the missing signal
    expect(emojiImage('\u{1F602}')).toBe(FakeImage.made[0])
  })

  it('bumps the generation on failure too, and stops re-requesting', () => {
    // A 404 must also wake the layer: it repaints once, draws nothing for that
    // emoji, and settles — rather than the loop spinning on a pending fetch.
    const before = emojiGeneration()
    expect(emojiImage('\u{1F92F}')).toBeNull()
    FakeImage.made[0].onerror!()
    expect(emojiGeneration()).toBe(before + 1)
    expect(emojiImage('\u{1F92F}')).toBeNull()
    expect(FakeImage.made).toHaveLength(1)
  })
})
