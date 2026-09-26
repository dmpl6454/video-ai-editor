// The timeline-parity suite as a PAGE, for the WKWebView harness
// (tests/wk/test_wk_timeline_parity.py). vitest proves the port under V8;
// the product runs JavaScriptCore, whose number formatting, BigInt, typed
// arrays and JSON float parsing the exact arithmetic leans on. This runs the
// same golden comparisons in the real engine and posts a summary.

import { audioJson, audioTotalSamples, buildProgramMap, clearMemo, KIND_BLEND, KIND_GAP, lookupFromJson, toRle } from '../timeline/programMap'
import type { EdlLike } from '../timeline/framePlan'
import type { SourceInfoJson } from '../timeline/frameMap'
import {
  ceilToFrame, ffmpegMicros, ffmpegRate, floorToFrame, fpsFloat, frameDuration, frameOf, quantize,
  rateOf, samplesForFrames, seekPreroll, sourceRate, timeOf,
} from '../timeline/timebase'

type Json = null | boolean | number | string | Json[] | { [k: string]: Json }

/** Structural equality (key order free, numbers by value). */
export function deepEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true
  if (typeof a === 'number' && typeof b === 'number') return Object.is(a, b) || a === b
  if (Array.isArray(a) || Array.isArray(b)) {
    if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false
    return a.every((x, i) => deepEqual(x, b[i]))
  }
  if (a && b && typeof a === 'object' && typeof b === 'object') {
    const ka = Object.keys(a as object).filter((k) => (a as Record<string, unknown>)[k] !== undefined)
    const kb = Object.keys(b as object).filter((k) => (b as Record<string, unknown>)[k] !== undefined)
    if (ka.length !== kb.length) return false
    return ka.every((k) => deepEqual((a as Record<string, unknown>)[k], (b as Record<string, unknown>)[k]))
  }
  return false
}

export interface ParityResult {
  ua: string
  checks: Record<string, number>
  mismatches: string[]
  bench: { coldMs: number; warmMs: number; frames: number }
  elapsedMs: number
}

const FRAME_BITS = 12

async function getJson(url: string): Promise<Json> {
  const r = await fetch(url)
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`)
  return r.json()
}

export async function runParity(goldens: string): Promise<ParityResult> {
  const t0 = performance.now()
  const checks: Record<string, number> = {}
  const mismatches: string[] = []
  const bad = (what: string) => { if (mismatches.length < 40) mismatches.push(what) }
  const count = (k: string, n = 1) => { checks[k] = (checks[k] ?? 0) + n }

  // ---- timebase ----
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const tb = await getJson(`${goldens}/timebase_cases.json`) as any
  const grid = tb.grid
  const fns: Array<[string, (t: number, f: number | null) => number]> = [
    ['frame_of', frameOf], ['quantize', quantize], ['floor_to_frame', floorToFrame],
    ['ceil_to_frame', ceilToFrame], ['seek_preroll', seekPreroll],
  ]
  for (const [name, fn] of fns) {
    grid.fps.forEach((f: number | null, i: number) => grid.times.forEach((t: number, j: number) => {
      count('timebase')
      if (fn(t, f) !== grid[name][i][j]) bad(`${name}(${t}, ${f}) = ${fn(t, f)} ≠ ${grid[name][i][j]}`)
    }))
  }
  grid.fps.forEach((f: number | null, i: number) => grid.frames.forEach((n: number, j: number) => {
    count('timebase', 2)
    if (timeOf(n, f) !== grid.time_of[i][j]) bad(`time_of(${n}, ${f})`)
    if (samplesForFrames(n, f) !== grid.samples_for_frames[i][j]) bad(`samples_for_frames(${n}, ${f})`)
  }))
  for (const [f, [num, den]] of tb.rate_of) {
    count('timebase')
    const r = rateOf(f)
    if (r.num !== num || r.den !== den) bad(`rate_of(${f})`)
  }
  for (const [f, ff, fr, fd] of tb.scalars) {
    count('timebase', 3)
    if (fpsFloat(f) !== ff || ffmpegRate(f) !== fr || frameDuration(f) !== fd) bad(`scalars(${f})`)
  }
  for (const [avg, nom, [num, den]] of tb.source_rate) {
    count('timebase')
    const r = sourceRate(avg, nom)
    if (r.num !== num || r.den !== den) bad(`source_rate(${avg}, ${nom})`)
  }
  for (const [x, us] of tb.ffmpeg_us) {
    count('timebase')
    if (ffmpegMicros(x) !== us) bad(`ffmpeg_us(${x}) = ${ffmpegMicros(x)} ≠ ${us}`)
  }

  // ---- frame map goldens (real renders) ----
  // `speed`: speed curves (their setpts is sqrt-based — JSC's Math.sqrt must
  // be correctly rounded, as IEEE 754 requires) and freeze frames.
  for (const group of ['rates', 'structure', 'transitions', 'segments', 'fuzz', 'speed']) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const doc = await getJson(`${goldens}/frame_map/${group}.json`) as any
    for (const c of doc.cases) {
      const sources = c.sources as Record<string, SourceInfoJson & { sid: number }>
      const lookup = lookupFromJson(sources)
      const pm = buildProgramMap(c.edl as EdlLike, lookup)
      const top: number[] = c.measured.top
      const bot: number[] = c.measured.bot ?? top
      if (pm.total !== top.length) { bad(`${c.name}: total ${pm.total} ≠ ${top.length}`); continue }
      for (let k = 0; k < pm.total; k++) {
        count('frames')
        if (group === 'speed') count('speedFrames')
        let t = 0
        let b = 0
        if (pm.kind[k] !== KIND_GAP) {
          t = (sources[pm.clips[pm.clip[k]].src].sid << FRAME_BITS) | pm.srcFrame[k]
          b = pm.kind[k] === KIND_BLEND ? (sources[pm.clips[pm.bClip[k]].src].sid << FRAME_BITS) | pm.bSrcFrame[k] : t
        }
        if (t !== top[k] || b !== bot[k]) bad(`${c.name} k=${k}: ${t}/${b} ≠ render ${top[k]}/${bot[k]}`)
      }
      count('models')
      if (!deepEqual(toRle(pm), c.model.runs)) bad(`${c.name}: RLE`)
      if (!deepEqual(pm.seams, c.model.seams)) bad(`${c.name}: seams`)
      if (!deepEqual(audioJson(pm, lookup), c.model.audio)) bad(`${c.name}: audio`)
      if (audioTotalSamples(pm) !== c.model.audio_total) bad(`${c.name}: audio total`)
    }
  }

  // ---- frame plan corpus (dispatch-made EDLs) ----
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const plan = await getJson(`${goldens}/frame_plan_cases.json`) as any
  const planLookup = lookupFromJson(plan.sources)
  for (const c of plan.cases) {
    count('plans')
    const pm = buildProgramMap(c.edl as EdlLike, planLookup)
    if (pm.total !== c.model.total || !deepEqual(toRle(pm), c.model.runs)
      || !deepEqual(audioJson(pm, planLookup), c.model.audio)) bad(`plan seq ${c.seq}`)
  }

  // ---- §11.2 budget: 12 min, 300 clips, 30 fps ----
  const long = { rate: [30, 1], tb: [1, 15360], frames: 30 * 60 * 15, start_ticks: 0, w: 1920, h: 1080 } as SourceInfoJson
  const clips = Array.from({ length: 300 }, (_, i) => ({
    id: `L${i}`, src: 'long', in: i * 2.4, out: i * 2.4 + 2.4, start: i * 2.4, speed: null, reverse: false,
  }))
  const edl: EdlLike = { duration: 720, canvas: { fps: 30 }, tracks: [{ id: 'v1', clips }] }
  const lk = lookupFromJson({ long })
  clearMemo()
  const c0 = performance.now()
  const pm = buildProgramMap(edl, lk)
  const coldMs = performance.now() - c0
  const w0 = performance.now()
  buildProgramMap(edl, lk)
  const warmMs = performance.now() - w0

  return {
    ua: navigator.userAgent, checks, mismatches,
    bench: { coldMs, warmMs, frames: pm.total }, elapsedMs: performance.now() - t0,
  }
}
