// The v1 keyframe matrix (Wave D3, lane E1a): tests/goldens/keyframe_matrix.json
// holds every output frame of 16 real compositor renders — every keyframed
// property (x, y, scale, rotation, opacity) on every clock (1x, 0.5x, 2x, a
// speed curve, reverse, reverse 2x, a freeze), clips at 0, 2 and 30 s, all
// seven interpolations — measured on the decoded frames (marker centroids,
// gain). Here the ENGINE (programMap → frameGeometry → the inverse chain) must
// put every marker where ffmpeg did, and it must key every property at the
// UI's clock: lib/overlay.ts `sampleKF` at `playhead - clip.start`.
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import { sampleKF, type KFNum } from '../../overlay'
import type { EdlClip, EdlLike } from '../timeline/framePlan'
import type { SourceInfoJson } from '../timeline/frameMap'
import { buildProgramMap, lookupFromJson } from '../timeline/programMap'
import { canvasPointOf, computeGeometry, frameGeometry, lrint } from './geometry'

type Pt = [number, number] | null
interface Row { k: number; clip: string; white: Pt; red: Pt; green: Pt; gain: number }
interface Render { name: string; group: 'pan' | 'zoom'; edl: EdlLike; rows: Row[] }
interface Doc {
  canvas: [number, number]; fps: number
  source: { key: string; w: number; h: number; info: SourceInfoJson }
  markers: Record<string, [number, number]>; renders: Render[]
}

const DOC = JSON.parse(readFileSync(fileURLToPath(new URL('../../../../../tests/goldens/keyframe_matrix.json', import.meta.url)), 'utf8')) as Doc
const [W, H] = DOC.canvas
const sources: Record<string, SourceInfoJson> = { [DOC.source.key]: DOC.source.info }

/** px: a value ON an integer boundary (x = 97.5 → lrint, s = 1.7 →
 *  trunc(640·s/2)) rounds either way in floats: one 2 px step of the chroma
 *  grid. With the clock nudged 1e-7 s either way (the tie resolved the
 *  other way) EVERY frame lands within 0.5, so the allowance cannot hide a
 *  systematic error. */
const TIE_PX = 2.1
const CLOSE_PX = 0.5
const GAIN_TOL = 0.02

function clipsOf(r: Render): Map<string, EdlClip> {
  const v1 = (r.edl.tracks ?? []).find((t) => t.id === 'v1')!
  return new Map((v1.clips as EdlClip[]).map((c) => [c.id, c]))
}

describe('keyframe matrix: the engine against decoded export frames', () => {
  it('covers every clock, property and interpolation', () => {
    const props = new Set<string>()
    const interps = new Set<string>()
    const starts = new Set<number>()
    for (const r of DOC.renders) {
      for (const c of clipsOf(r).values()) {
        starts.add(Number((c as { start?: number }).start ?? 0))
        for (const [p, v] of Object.entries((c.transform ?? {}) as Record<string, unknown>)) {
          if (v && typeof v === 'object') {
            props.add(p)
            interps.add((v as { interp: string }).interp)
          }
        }
      }
    }
    expect([...props].sort()).toEqual(['opacity', 'rotation', 'scale', 'x', 'y'])
    expect(interps.size).toBe(7)
    expect([...starts]).toEqual(expect.arrayContaining([0, 2, 30]))
    expect(DOC.renders.length).toBe(16)
  })

  for (const r of DOC.renders) {
    it(r.name, () => {
      const lookup = lookupFromJson(sources)
      const pm = buildProgramMap(r.edl, lookup)
      const clips = clipsOf(r)
      const names = r.group === 'pan' ? ['white'] : ['red', 'green']
      let worst = 0
      let close = 0
      let total = 0
      for (const row of r.rows) {
        const fg = frameGeometry(pm, row.k, { w: W, h: H }, lookup)
        expect(fg, `${r.name} k=${row.k}`).not.toBeNull()
        const clip = clips.get(row.clip)!
        expect(fg!.clip.id).toBe(row.clip)
        // the engine keys at the UI's clock: playhead − clip.start
        const tUi = row.k / DOC.fps - Number((clip as { start?: number }).start ?? 0)
        const atUi = computeGeometry({ canvas: { w: W, h: H }, source: { w: DOC.source.w, h: DOC.source.h }, clip, tSrc: 0, tClip: tUi })
        expect(fg!.geometry.gain, `${r.name} k=${row.k} keyed off the UI clock`).toBeCloseTo(atUi.gain, 9)
        for (const n of names) {
          const at = row[n as 'white' | 'red' | 'green']
          expect(at, `${r.name} k=${row.k} ${n}`).not.toBeNull()
          const [u0, v0] = DOC.markers[n]
          const uv: [number, number] = [lrint(u0 * DOC.source.w) / DOC.source.w, lrint(v0 * DOC.source.h) / DOC.source.h]
          const p = canvasPointOf(fg!.geometry, ...uv)
          const q = canvasPointOf(atUi, ...uv)
          expect(p, `${r.name} k=${row.k} ${n} visible`).not.toBeNull()
          // the same picture as sampling at playhead − clip.start (± a tie)
          expect(Math.max(Math.abs(p![0] - q![0]), Math.abs(p![1] - q![1])), `${r.name} k=${row.k} ${n} clock`).toBeLessThanOrEqual(TIE_PX)
          const dist = (g: typeof atUi) => {
            const c = canvasPointOf(g, ...uv)
            return c ? Math.max(Math.abs(c[0] - at![0]), Math.abs(c[1] - at![1])) : Infinity
          }
          const err = dist(fg!.geometry)
          worst = Math.max(worst, err)
          // a tie resolved the other way: the same clock a hair either side
          const nudged = Math.min(err, ...[-1e-7, 1e-7].map((dt) => dist(computeGeometry({
            canvas: { w: W, h: H }, source: { w: DOC.source.w, h: DOC.source.h }, clip, tSrc: 0, tClip: tUi + dt }))))
          if (nudged <= CLOSE_PX) close++
          total++
          expect(err, `${r.name} k=${row.k} ${n}: engine ${p} vs export ${at}`).toBeLessThanOrEqual(TIE_PX)
        }
        if (r.group === 'pan') {
          const tx = (clip.transform ?? {}) as { opacity?: KFNum }
          const o = sampleKF(tx.opacity, tUi, 1)
          expect(Math.abs(fg!.geometry.gain - o), `${r.name} k=${row.k} engine gain vs sampleKF`).toBeLessThan(1e-9)
          expect(Math.abs(row.gain - o), `${r.name} k=${row.k} export gain ${row.gain} vs sampleKF ${o}`).toBeLessThanOrEqual(GAIN_TOL)
        }
      }
      expect(worst).toBeLessThanOrEqual(TIE_PX)
      expect(close, `${r.name}: ${close}/${total} within ${CLOSE_PX} px`).toBe(total)
    })
  }

  it('a pan follows sampleKF at the playhead: +x right, on every clock', () => {
    // the white centre marker (scale 2 static): its shift from the canvas
    // centre IS sampleKF(x) (± the chroma grid), frame for frame
    for (const r of DOC.renders.filter((x) => x.group === 'pan')) {
      const clips = clipsOf(r)
      for (const row of r.rows) {
        const clip = clips.get(row.clip)!
        const tUi = row.k / DOC.fps - Number((clip as { start?: number }).start ?? 0)
        const tx = clip.transform as { x: KFNum; y: KFNum }
        expect(Math.abs(row.white![0] - (W / 2 + sampleKF(tx.x, tUi, 0))), `${r.name} k=${row.k} x`).toBeLessThanOrEqual(2.4)
        expect(Math.abs(row.white![1] - (H / 2 + sampleKF(tx.y, tUi, 0))), `${r.name} k=${row.k} y`).toBeLessThanOrEqual(2.4)
      }
    }
  })
})

describe('sampleKF: a step key shows its value on its own time', () => {
  it('holds v0 until the key, then v1 AT the key (the export\'s if(lt(t, t1), v0, …))', () => {
    const kf = { keyframes: [[0, 0], [23 / 30, 10], [1, 20]] as [number, number][], interp: 'step' as const }
    expect(sampleKF(kf, 22 / 30, -1)).toBe(0)
    expect(sampleKF(kf, 23 / 30, -1)).toBe(10)      // it returned 0 on the key's own frame
    expect(sampleKF(kf, 29 / 30, -1)).toBe(10)
    expect(sampleKF(kf, 1, -1)).toBe(20)
  })
})
