// Transform.flip_h / flip_v (wave E, lane F4a): the engine mirrors the fitted
// frame BEFORE the rotation, scale and pan, exactly as the export's `hflip` /
// `vflip` on the canvas-sized frame (compositor `_build_clip_video_chain`).
// geometry.test.ts holds every `tflip_*` golden (real renders) to 1 px; this
// file proves those cases DISCRIMINATE — the same clip without the mirror (or
// with the mirror after the rotation, like the hflip EFFECT) lands its
// markers elsewhere — and pins the stage's algebra.
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import type { EdlClip, EdlLike } from '../timeline/framePlan'
import type { SourceInfoJson } from '../timeline/frameMap'
import { buildProgramMap, lookupFromJson } from '../timeline/programMap'
import { apply, canvasPointOf, computeGeometry, flipStage, frameGeometry, lrint } from './geometry'
import { clipFeatures, MODE_EXACT, PHASE_CAPS, TRANSFORM_FLIP_MODE } from '../timeline/support'

interface Frame { k: number; markers?: Record<string, [number, number] | null> }
interface Case { name: string; canvas: [number, number]; edl: EdlLike; sources: Record<string, SourceInfoJson>; frames: Frame[] }
const DOC = JSON.parse(readFileSync(fileURLToPath(new URL('../../../../../tests/goldens/geometry_cases.json', import.meta.url)), 'utf8')) as
  { markers: Record<string, [number, number]>; cases: Case[] }

const FLIPS = DOC.cases.filter((c) => c.name.startsWith('tflip_'))

/** Worst marker error (px) of `edl` against the case's rendered markers. */
function worstError(c: Case, edl: EdlLike): number {
  const [W, H] = c.canvas
  const lookup = lookupFromJson(c.sources)
  const pm = buildProgramMap(edl, lookup)
  let worst = 0
  for (const fr of c.frames) {
    const fg = frameGeometry(pm, fr.k, { w: W, h: H }, (s) => lookup(s))!
    const info = lookup(fg.clip.src)
    for (const [name, at] of Object.entries(fr.markers ?? {})) {
      if (!at) continue
      const [u0, v0] = DOC.markers[name]
      const p = canvasPointOf(fg.geometry, lrint(u0 * info.w) / info.w, lrint(v0 * info.h) / info.h)
      worst = Math.max(worst, p ? Math.hypot(p[0] - at[0], p[1] - at[1]) : 1e9)
    }
  }
  return worst
}

function withTransform(edl: EdlLike, patch: Record<string, unknown>): EdlLike {
  return {
    ...edl,
    tracks: (edl.tracks ?? []).map((t) => ({
      ...t,
      clips: t.clips.map((c) => {
        const cc = c as EdlClip
        return { ...cc, transform: { ...(cc.transform as object), ...patch } }
      }),
    })),
  }
}

describe('Transform flip: the rendered goldens discriminate', () => {
  it('has the flip cases', () => {
    expect(FLIPS.length).toBeGreaterThanOrEqual(8)
  })
  // A portrait source covering a landscape frame shows only its centre
  // marker, which a mirror cannot move: that case pins the PAN under the
  // mirror instead — the window is taken at +x, so the mirrored picture still
  // moves right (a mirror of the −x window would put the marker 60 px left).
  const PAN_ONLY = new Set(['tflip_h_cover_pan'])
  for (const c of FLIPS) {
    it(`${c.name}: exact with the mirror, far off without it`, () => {
      expect(worstError(c, c.edl)).toBeLessThanOrEqual(1.0)
      if (PAN_ONLY.has(c.name)) {
        expect(worstError(c, withTransform(c.edl, { x: -30 }))).toBeGreaterThan(50)
        return
      }
      expect(worstError(c, withTransform(c.edl, { flip_h: false, flip_v: false }))).toBeGreaterThan(20)
    })
  }

  it('mirrors BEFORE the rotation: the effect-order mirror (after it) is another picture', () => {
    const c = FLIPS.find((x) => x.name === 'tflip_h_rotate')!
    const asEffect = withTransform(c.edl, { flip_h: false })
    const clip = (asEffect.tracks![0].clips[0]) as EdlClip
    clip.effects = [{ type: 'hflip' }]
    expect(worstError(c, asEffect)).toBeGreaterThan(20)
  })
})

describe('flipStage', () => {
  it('is an exact, self-inverse mirror about the frame centre', () => {
    const m = flipStage({ flip_h: true, flip_v: true }, 640, 360)
    expect(apply(m, 0.5, 0.5)).toEqual([639.5, 359.5])
    const twice = apply(m, ...apply(m, 17.25, 300.5))
    expect(twice).toEqual([17.25, 300.5])
    expect(apply(flipStage({}, 640, 360), 12, 34)).toEqual([12, 34])
  })

  it('leaves an unflipped clip geometry untouched', () => {
    const clip = { id: 'c', src: 's', in: 0, out: 1, transform: { rotation: 12, scale: 0.8 } } as EdlClip
    const flipped = { ...clip, transform: { rotation: 12, scale: 0.8, flip_h: false, flip_v: false } } as EdlClip
    const a = computeGeometry({ canvas: { w: 640, h: 360 }, source: { w: 1280, h: 720 }, clip, tSrc: 0 })
    const b = computeGeometry({ canvas: { w: 640, h: 360 }, source: { w: 1280, h: 720 }, clip: flipped, tSrc: 0 })
    expect(b).toEqual(a)
  })
})

describe('fidelity class', () => {
  it('a flipped clip is EXACT: no cause in any phase', () => {
    expect(TRANSFORM_FLIP_MODE).toBe(MODE_EXACT)
    const clip = { id: 'c', src: 's', in: 0, out: 1, transform: { flip_h: true, flip_v: true, rotation: 30 } } as EdlClip
    for (const phase of [1, 2, 3, 4, 5] as const) expect(clipFeatures(clip, PHASE_CAPS[phase])).toEqual([])
  })
})
