// Frame-exact parity with the REAL compositor (spec §6 R3/R4, §13): every
// output frame of every golden case — decoded from ffmpeg renders of
// bar-coded sources (tests/goldens/frame_map/*.json) — must be reproduced by
// framePlan.ts + frameMap.ts + programMap.ts exactly, and the RLE, the seam
// rows and the sound placement must equal what render/frame_map.py emits.
import { readdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import type { EdlLike } from './framePlan'
import { timeOf } from './timebase'
import type { SourceInfoJson } from './frameMap'
import {
  KIND_BLEND, KIND_GAP, audioJson, audioTotalSamples, buildProgramMap, clearMemo, compareWithRle,
  decodeSeq, diffPrograms, lookupFromJson, memoStats, rleFrames, toRle, type Run,
} from './programMap'

interface GoldenCase {
  name: string
  group: string
  live: boolean
  edl: EdlLike
  sources: Record<string, SourceInfoJson & { sid: number }>
  total: number
  measured: { top: number[]; bot?: number[]; p?: number[] }
  model: {
    R: [number, number]; T: number | null; total: number; runs: Run[]
    seams: { left: number; right: number; frames: number; type: string }[]
    audio: unknown[]
    audio_total: number
  }
}

const DIR = fileURLToPath(new URL('../../../../../tests/goldens/frame_map/', import.meta.url))
const CASES: GoldenCase[] = readdirSync(DIR).filter((f) => f.endsWith('.json')).sort().flatMap((f) => {
  const doc = JSON.parse(readFileSync(DIR + f, 'utf8')) as { cases: Omit<GoldenCase, 'group'>[] }
  return doc.cases.map((c) => ({ ...c, group: f.replace('.json', '') }))
})

const FRAME_BITS = 12
const code = (sid: number, frame: number) => (sid << FRAME_BITS) | frame

/** The model in the measured shape: top/bottom bar codes per output frame. */
function modelCodes(c: GoldenCase) {
  const pm = buildProgramMap(c.edl, lookupFromJson(c.sources))
  const sid = (src: string) => c.sources[src].sid
  const top: number[] = []
  const bot: number[] = []
  const p: (number | null)[] = []
  for (let k = 0; k < pm.total; k++) {
    if (pm.kind[k] === KIND_GAP) { top.push(0); bot.push(0); p.push(null); continue }
    const a = code(sid(pm.clips[pm.clip[k]].src), pm.srcFrame[k])
    top.push(a)
    if (pm.kind[k] === KIND_BLEND) {
      bot.push(code(sid(pm.clips[pm.bClip[k]].src), pm.bSrcFrame[k]))
      p.push(pm.pNum[k] / pm.pDen[k])
    } else { bot.push(a); p.push(null) }
  }
  return { pm, top, bot, p }
}

describe('golden coverage', () => {
  it('holds every (project, source) rate pair and every group', () => {
    const groups = new Set(CASES.map((c) => c.group))
    for (const g of ['rates', 'structure', 'transitions', 'segments', 'fuzz']) expect(groups).toContain(g)
    expect(CASES.filter((c) => c.group === 'rates')).toHaveLength(64)
    const frames = CASES.reduce((s, c) => s + c.total, 0)
    expect(frames).toBeGreaterThan(20000)
  })
})

describe.each(CASES.map((c) => [`${c.group}/${c.name}`, c] as const))('%s', (_label, c) => {
  it('every output frame equals the decoded render', () => {
    const { top, bot, p } = modelCodes(c)
    const mt = c.measured.top
    const mb = c.measured.bot ?? mt
    expect(top.length).toBe(mt.length)
    const bad: string[] = []
    for (let k = 0; k < mt.length; k++) {
      if (top[k] !== mt[k] || bot[k] !== mb[k]) bad.push(`k=${k} model ${top[k]}/${bot[k]} render ${mt[k]}/${mb[k]}`)
      const pk = p[k]
      if (pk !== null && c.measured.p && Math.abs(c.measured.p[k] / 1000 - pk) > 0.02) {
        bad.push(`k=${k} progress model ${pk} render ${c.measured.p[k] / 1000}`)
      }
    }
    expect(bad.slice(0, 10)).toEqual([])
  })

  it('RLE, seams and sound placement equal render/frame_map.py', () => {
    const pm = buildProgramMap(c.edl, lookupFromJson(c.sources))
    expect([pm.R.num, pm.R.den]).toEqual(c.model.R)
    expect(pm.T).toBe(c.model.T)
    expect(pm.total).toBe(c.model.total)
    expect(toRle(pm)).toEqual(c.model.runs)
    expect(pm.seams).toEqual(c.model.seams)
    expect(audioJson(pm, lookupFromJson(c.sources))).toEqual(c.model.audio)
    expect(audioTotalSamples(pm)).toBe(c.model.audio_total)
    // And the structural check (R14) passes against the server's runs.
    expect(compareWithRle(pm, c.model.runs)).toEqual([])
  })
})

describe('RLE', () => {
  it('round-trips every golden map, linear and non-linear sequences alike', () => {
    let nonLinear = 0
    for (const c of CASES) {
      const pm = buildProgramMap(c.edl, lookupFromJson(c.sources))
      const runs = toRle(pm)
      const frames = rleFrames(runs)
      expect(frames).toHaveLength(pm.total)
      for (let k = 0; k < pm.total; k++) {
        expect(frames[k].frame).toBe(pm.kind[k] === KIND_GAP ? -1 : pm.srcFrame[k])
      }
      nonLinear += runs.filter((r) => r.a?.d !== undefined).length
    }
    expect(nonLinear).toBeGreaterThan(50)
  })

  it('decodes negative deltas (reverse) and multi-byte varints', () => {
    expect(decodeSeq({ f0: 500, d: 'AQEBAQ==' }, 5)).toEqual([500, 499, 498, 497, 496])
    const pm = buildProgramMap(CASES.find((c) => c.name === 'rates_p30_s30')!.edl,
      lookupFromJson(CASES.find((c) => c.name === 'rates_p30_s30')!.sources))
    const runs = toRle(pm)
    expect(runs.some((r) => r.a?.step === -1)).toBe(true)   // a reversed clip
  })

  it('a corrupted server map is caught frame by frame (R14 → BAKED)', () => {
    const c = CASES.find((x) => x.name === 'xfade_p30')!
    const pm = buildProgramMap(c.edl, lookupFromJson(c.sources))
    const runs: Run[] = JSON.parse(JSON.stringify(c.model.runs))
    const blend = runs.find((r) => r.kind === KIND_BLEND)!
    blend.p_j0 = (blend.p_j0 ?? 0) + 1
    const bad = compareWithRle(pm, runs)
    expect(bad[0]).toBe(blend.k0)
    const shifted: Run[] = JSON.parse(JSON.stringify(c.model.runs))
    shifted[0].a!.f0 += 1
    expect(compareWithRle(pm, shifted)[0]).toBe(0)
  })
})

describe('memo and diff (§4.1)', () => {
  const base = CASES.find((c) => c.name === 'gaps_p29.97')!
  const lookup = lookupFromJson(base.sources)
  const v1 = () => (base.edl.tracks ?? []).find((t) => t.id === 'v1')!

  it('a split changes no frame: dirtyFrames = ∅, only the new half rebinds', () => {
    const before = buildProgramMap(base.edl, lookup)
    const edl = structuredClone(base.edl) as EdlLike
    const t = edl.tracks!.find((x) => x.id === 'v1')!
    const c = t.clips[0] as { id: string; in: number; out: number; start: number }
    const fps = base.edl.canvas!.fps!
    // Clip 0 is in = 10, out = 22, start = 5 (frames); split 6 frames in, on
    // the grid, as `split_at` would store it.
    const leftC = { ...c, out: timeOf(16, fps) }
    const rightC = { ...c, id: 'split-right', in: timeOf(16, fps), start: timeOf(11, fps) }
    t.clips = [leftC, rightC, ...t.clips.slice(1)]
    const after = buildProgramMap(edl, lookup)
    const d = diffPrograms(before, after)
    expect(after.total).toBe(before.total)
    expect(d.dirtyFrames).toEqual([])
    expect([...d.dirtyParams]).toEqual(['split-right'])
  })

  it('a trim dirties exactly the frames it removed', () => {
    const before = buildProgramMap(base.edl, lookup)
    const edl = structuredClone(base.edl) as EdlLike
    const t = edl.tracks!.find((x) => x.id === 'v1')!
    const c = t.clips[2] as { out: number }
    c.out = timeOf(77, base.edl.canvas!.fps!)   // was 80 frames
    const after = buildProgramMap(edl, lookup)
    const d = diffPrograms(before, after)
    expect(d.dirtyFrames.length).toBeGreaterThan(0)
    const first = d.dirtyFrames[0][0]
    // Clip 2's last three frames became gap; the gap before clip 3 absorbs
    // the change, so nothing after them moves.
    expect(d.dirtyFrames).toEqual([[before.clipStart[2] + before.clipLen[2] - 3,
      before.clipStart[2] + before.clipLen[2]]])
    for (let k = 0; k < first; k++) expect(after.srcFrame[k]).toBe(before.srcFrame[k])
  })

  it('rebuilding an unchanged timeline hits the per-clip memo for every clip', () => {
    clearMemo()
    buildProgramMap(base.edl, lookup)
    const cold = memoStats()
    buildProgramMap(base.edl, lookup)
    const warm = memoStats()
    expect(cold.misses).toBe(v1().clips.length)
    expect(warm.misses).toBe(cold.misses)
    expect(warm.hits - cold.hits).toBe(v1().clips.length)
  })

  it('first build: everything dirty', () => {
    const pm = buildProgramMap(base.edl, lookup)
    expect(diffPrograms(null, pm).dirtyFrames).toEqual([[0, pm.total]])
  })

  it('builds a 12-minute, 300-clip 30 fps timeline fast (informational budget ≤ 4 ms warm)', () => {
    const src = base.sources[Object.keys(base.sources)[0]]
    const sources = { long: { ...src, frames: 30 * 60 * 15, w: 1920, h: 1080 } }
    const clips = Array.from({ length: 300 }, (_, i) => ({
      id: `L${i}`, src: 'long', in: i * 2.4, out: i * 2.4 + 2.4, start: i * 2.4,
      speed: null, reverse: false,
    }))
    const edl: EdlLike = { duration: 720, canvas: { fps: 30 }, tracks: [{ id: 'v1', clips }] }
    const lk = lookupFromJson(sources)
    const t0 = performance.now()
    const pm = buildProgramMap(edl, lk)
    const cold = performance.now() - t0
    const t1 = performance.now()
    buildProgramMap(edl, lk)
    const warm = performance.now() - t1
    expect(pm.total).toBe(21600)
    expect(cold).toBeLessThan(500)
    expect(warm).toBeLessThan(50)
  })
})
