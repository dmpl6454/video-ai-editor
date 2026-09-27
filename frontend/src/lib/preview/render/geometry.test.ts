// geometry.ts against the SERVER's filter maths (spec §3.4, §13): every case
// in tests/goldens/geometry_cases.json was rendered through the real
// compositor from marker sources and measured — where each marker's centre
// landed, where the picture's edges are, and the RGB gain. The program map
// (the engine's own path) picks the source frame for k, frameGeometry turns
// it into the inverse chain the shader walks, and the forward map must put
// every marker where ffmpeg did.
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import type { EdlLike } from '../timeline/framePlan'
import type { SourceInfoJson } from '../timeline/frameMap'
import { buildProgramMap, lookupFromJson } from '../timeline/programMap'
import {
  canvasPointOf, compose, computeGeometry, cropOffset, evenDown, fadeFactor, fitDims, frameGeometry, invert,
  apply, hasKeyframes, kfPanFrame, lrint, padOffset, sourceUvAt, type ClipGeometry,
} from './geometry'

interface Frame { k: number; markers?: Record<string, [number, number] | null>; bbox?: number[] | null; gain?: number }
interface Case {
  name: string; canvas: [number, number]; gain_only: boolean; edl: EdlLike
  sources: Record<string, SourceInfoJson>; frames: Frame[]
}
interface Doc { version: number; grey: number; markers: Record<string, [number, number]>; cases: Case[] }

const DOC = JSON.parse(readFileSync(fileURLToPath(new URL('../../../../../tests/goldens/geometry_cases.json', import.meta.url)), 'utf8')) as Doc

/** Canvas bbox of the pixels whose centre maps into the picture. */
function bboxOf(g: ClipGeometry, W: number, H: number): number[] | null {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity
  for (let y = 0; y < H; y++) {
    for (let x = 0; x < W; x++) {
      if (!sourceUvAt(g, x + 0.5, y + 0.5)) continue
      if (x < x0) x0 = x
      if (y < y0) y0 = y
      if (x + 1 > x1) x1 = x + 1
      if (y + 1 > y1) y1 = y + 1
    }
  }
  return x0 === Infinity ? null : [x0, y0, x1, y1]
}

describe('ffmpeg integer rules', () => {
  it('lrint rounds half to even, like C in the default mode', () => {
    expect([0.5, 1.5, 2.5, -1.5, 202.5, 609.5, 2.4, 2.6].map(lrint)).toEqual([0, 2, 2, -2, 202, 610, 2, 3])
  })
  it('scale force_original_aspect_ratio', () => {
    expect(fitDims(720, 1280, 640, 360, 'decrease')).toEqual([203, 360]) // av_rescale: 202.5 → 203
    expect(fitDims(702, 1280, 640, 360, 'decrease')).toEqual([197, 360])
    expect(fitDims(720, 1280, 640, 360, 'increase')).toEqual([640, 1138])
    expect(fitDims(1280, 706, 640, 360, 'increase')).toEqual([653, 360])
  })
  it('pad and crop offsets snap to the chroma grid', () => {
    expect(padOffset(219)).toBe(218)
    expect(padOffset(221.5)).toBe(220)
    expect(cropOffset(33, 706, 640)).toBe(32)
    expect(cropOffset(609.5, 1479, 360)).toBe(610)
    expect(cropOffset(-30, 640, 640)).toBe(0)
    expect(cropOffset(889, 1138, 360)).toBe(778)
    expect(evenDown(-3)).toBe(-4)
  })
  it('fade factors are vf_fade 16-bit steps', () => {
    expect(fadeFactor(0, 0, 0.5, false)).toBe(0)
    expect(fadeFactor(0.25, 0, 0.5, false)).toBeCloseTo(0.5, 4)
    expect(fadeFactor(0.5, 0, 0.5, false)).toBe(1)
    expect(fadeFactor(0.1, 0.5, 0.5, true)).toBe(1)
    expect(fadeFactor(1.0, 0.5, 0.5, true)).toBe(0)
  })
  it('affine helpers invert and compose', () => {
    const m = { a: 2, b: 0.5, c: -1, d: 3, e: 7, f: -2 }
    const id = compose(m, invert(m))
    const [x, y] = apply(id, 13, -4)
    expect(x).toBeCloseTo(13, 9)
    expect(y).toBeCloseTo(-4, 9)
  })
})

describe('geometry parity with real compositor renders', () => {
  it('covers every feature of the P1 geometry passes', () => {
    const names = DOC.cases.map((c) => c.name).join(' ')
    for (const f of ['contain', 'cover', 'cover_pan', 'rotate', 'scale', 'pan', 'kf_', 'hflip', 'vflip', 'opacity', 'fade']) {
      expect(names).toContain(f)
    }
  })

  for (const c of DOC.cases) {
    it(c.name, () => {
      const [W, H] = c.canvas
      const lookup = lookupFromJson(c.sources)
      const pm = buildProgramMap(c.edl, lookup)
      const infoOf = (src: string) => lookup(src)
      for (const fr of c.frames) {
        const fg = frameGeometry(pm, fr.k, { w: W, h: H }, infoOf)
        expect(fg, `${c.name} k=${fr.k} is a clip frame`).not.toBeNull()
        const g = fg!.geometry
        if (fr.gain !== undefined) {
          // 2 levels of 128 at the darkest (8-bit yuv round trips), else 1 %
          expect(Math.abs(g.gain - fr.gain), `${c.name} k=${fr.k} gain ${g.gain} vs ${fr.gain}`).toBeLessThanOrEqual(Math.max(0.01, 2 / 128))
        }
        if (fr.markers) {
          const info = lookup(fg!.clip.src)
          for (const [name, at] of Object.entries(fr.markers)) {
            if (!at) continue
            const [u0, v0] = DOC.markers[name]
            // the square is centred on the rounded pixel (make_source)
            const u = lrint(u0 * info.w) / info.w
            const v = lrint(v0 * info.h) / info.h
            const p = canvasPointOf(g, u, v)
            expect(p, `${c.name} k=${fr.k} ${name} should be visible`).not.toBeNull()
            const err = Math.hypot(p![0] - at[0], p![1] - at[1])
            expect(err, `${c.name} k=${fr.k} ${name} at ${p} vs rendered ${at}`).toBeLessThanOrEqual(1.0)
          }
        }
        if (fr.bbox !== undefined) {
          const bb = bboxOf(g, W, H)
          expect(bb === null, `${c.name} k=${fr.k} picture presence`).toBe(fr.bbox === null)
          if (bb && fr.bbox) {
            bb.forEach((v, i) => expect(Math.abs(v - fr.bbox![i]), `${c.name} k=${fr.k} bbox ${bb} vs ${fr.bbox}`).toBeLessThanOrEqual(1))
          }
        }
      }
    })
  }
})

describe('keyframes: the clock and the meaning the UI gives them (Wave D3)', () => {
  const W = 640
  const H = 360
  const at = (clip: Record<string, unknown>, tClip: number, tSrc = 0) =>
    computeGeometry({ canvas: { w: W, h: H }, source: { w: 1280, h: 720 }, clip: clip as never, tSrc, tClip })

  it('keys at the OUTPUT frame\'s clip-local time, not the source frame\'s', () => {
    // a 0.5x clip repeats each source frame: both output frames must differ
    const clip = { id: 'a', src: 's', in: 0, out: 1, start: 2, speed: 0.5, transform: { x: { keyframes: [[0, 0], [2, 200]] }, scale: 2 } }
    const k0 = canvasPointOf(at(clip, 0 / 30, 0), 0.5, 0.5)![0]
    const k1 = canvasPointOf(at(clip, 1 / 30, 0), 0.5, 0.5)![0]
    const k2 = canvasPointOf(at(clip, 2 / 30, 1 / 30), 0.5, 0.5)![0]
    expect(k0).toBeCloseTo(320, 5)
    expect(k1).toBeGreaterThan(k0)        // x = 3.33 → +2 on the chroma grid
    expect(k2).toBeGreaterThan(k1)        // x = 6.67 → +6
  })

  it('+x moves the picture RIGHT, like a static pan and the UI', () => {
    const clip = { id: 'a', src: 's', in: 0, out: 2, transform: { x: { keyframes: [[0, 0], [2, 300]] }, scale: 2 } }
    expect(canvasPointOf(at(clip, 0.5), 0.5, 0.5)![0]).toBeCloseTo(394, 0)
  })

  it('a keyed zoom grows about the centre and a keyed scale < 1 shrinks', () => {
    const grow = { id: 'a', src: 's', in: 0, out: 1, transform: { scale: { keyframes: [[0, 1], [1, 2]] } } }
    for (const t of [0, 0.5, 1]) expect(canvasPointOf(at(grow, t), 0.5, 0.5)).toEqual([320, 180])
    const shrink = { id: 'a', src: 's', in: 0, out: 1, transform: { scale: { keyframes: [[0, 0.5], [1, 1]] } } }
    const g = at(shrink, 0)
    expect(sourceUvAt(g, 100, 180)).toBeNull()          // black around the half-size picture
    expect(canvasPointOf(g, 0, 0)![0]).toBeCloseTo(160, 5)
  })

  it('hasKeyframes and the pan frame follow compositor.kf_pan_frame', () => {
    expect(hasKeyframes({ id: 'a', src: 's', transform: { opacity: { keyframes: [[0, 1], [1, 0]] } } } as never)).toBe(true)
    expect(hasKeyframes({ id: 'a', src: 's', transform: { opacity: { keyframes: [[0, 1]] } } } as never)).toBe(false)
    expect(kfPanFrame({ x: { keyframes: [[0, 0], [2, 100]] } }, 640, 360)).toEqual([842, 362])
    expect(kfPanFrame({ scale: { keyframes: [[0, 0.5], [1, 1.8]] }, y: 20.5 }, 640, 360)).toEqual([1154, 692])
  })
})
