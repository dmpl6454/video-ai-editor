import { describe, expect, it } from 'vitest'
import { textDrawOrder } from './textDrawOrder'

describe('text layer order = the export order (review RE)', () => {
  it('draws a caption (captions track, z 13) over a super text (z 11) that overlaps it', () => {
    const items = [
      { z: 13, c: { start: 2.5 }, id: 'caption' },
      { z: 11, c: { start: 3.0 }, id: 'super' },
      { z: 10, c: { start: 0.0 }, id: 'hook' },
    ]
    expect([...items].sort(textDrawOrder).map((x) => x.id)).toEqual(['hook', 'super', 'caption'])
  })
  it('breaks a z tie by start, like the export', () => {
    const items = [{ z: 11, c: { start: 4 }, id: 'b' }, { z: 11, c: { start: 1 }, id: 'a' }]
    expect(items.sort(textDrawOrder).map((x) => x.id)).toEqual(['a', 'b'])
  })
})
