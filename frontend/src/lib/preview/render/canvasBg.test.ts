import { describe, expect, it } from 'vitest'
import cases from './__fixtures__/canvas_bg_cases.json'
import { apply } from './geometry'
import { blurDims, blurSigmaSmall, canvasBgDraw } from './canvasBg'

// render/canvas_bg.py's numbers (tests/gen_canvas_bg_fixture.py; the pytest
// suite asserts the same file is current).
describe('canvas background (engine port of render/canvas_bg.py)', () => {
  it('builds the blur on the export\'s 1/4 frame with its sigma', () => {
    for (const c of cases.blur) {
      const [w, h] = c.canvas
      expect(blurDims(w, h), `${w}x${h}`).toEqual(c.small)
      c.sigma.forEach((s, i) => expect(blurSigmaSmall(i + 1, w, h)).toBeCloseTo(s, 6))
    }
  })

  const canvas = { w: 360, h: 640 }
  const source = { w: 1280, h: 720 }

  it('draws nothing for no background or a cover fit (no letterbox)', () => {
    expect(canvasBgDraw({ src: 'a', canvas_bg: null } as never, canvas, source)).toBeNull()
    expect(canvasBgDraw({ src: 'a', fit: 'cover', canvas_bg: { type: 'color', color: '#FF0000' } } as never,
      canvas, source)).toBeNull()
  })

  it('colours the letterbox, mapping F1 to the background uv', () => {
    const d = canvasBgDraw({ src: 'a', canvas_bg: { type: 'color', color: '#E53935' } } as never, canvas, source)!
    expect(d.mode).toBe('color')
    if (d.mode !== 'color') return
    expect(d.rgb).toEqual([229, 57, 53])
    expect(apply(d.toBg, 180, 320)).toEqual([0.5, 0.5])
  })

  it('mirrors a BLUR background with the Transform flip (a copy of the mirrored picture), not a picture', () => {
    const d = canvasBgDraw({ src: 'a', transform: { flip_h: true }, canvas_bg: { type: 'blur', blur: 2 } } as never,
      canvas, source)!
    const [u, v] = apply(d.toBg, 36, 64)
    expect(u).toBeCloseTo(0.9, 9)
    expect(v).toBeCloseTo(0.1, 9)
    const im = canvasBgDraw({ src: 'a', id: 'c', transform: { flip_h: true },
      canvas_bg: { type: 'image', image: '/p.png' } } as never, canvas, source, '/api/x')!
    expect(apply(im.toBg, 36, 64)).toEqual([0.1, 0.1])
  })

  it('maps a CANVAS point, whatever the clip\'s pose (review RE: a still layer)', () => {
    const d = canvasBgDraw({ src: 'a', transform: { scale: 0.5, x: 40, rotation: 12 },
      canvas_bg: { type: 'color', color: '#000000' } } as never, canvas, source)!
    expect(apply(d.toBg, 0, 0)).toEqual([0, 0])
    expect(apply(d.toBg, 360, 640)).toEqual([1, 1])
  })

  it('covers the small frame with ffmpeg\'s size and crop rules', () => {
    const d = canvasBgDraw({ src: 'a', canvas_bg: { type: 'blur', blur: 4 } } as never, canvas, source)!
    expect(d.mode).toBe('blur')
    if (d.mode !== 'blur') return
    expect(d.small).toEqual({ w: 90, h: 160 })
    expect(d.sigma).toBeCloseTo(5.4, 9)
    // 1280x720 covering 90x160: 284x160 (av_rescale), crop x = lrint(97) → 96 (down to even)
    const [u0] = apply(d.coverToUv, 0, 0)
    const [u1] = apply(d.coverToUv, 90, 0)
    expect(u0 * 284).toBeCloseTo(96, 9)
    expect(u1 * 284).toBeCloseTo(186, 9)
  })

  it('fetches an image background from the session route, keyed by its picture', () => {
    const clip = { id: 'c 1', src: 'a', canvas_bg: { type: 'image', image: '/u/pic.png' } }
    expect(canvasBgDraw(clip as never, canvas, source)).toBeNull()
    const d = canvasBgDraw(clip as never, canvas, source, '/api/sessions/s/canvas-bg')!
    expect(d.mode).toBe('image')
    if (d.mode !== 'image') return
    expect(d.url).toBe('/api/sessions/s/canvas-bg/c%201.png?w=360&h=640&v=%2Fu%2Fpic.png')
  })
})
