// The fidelity classes of spec §7, per phase (§12), on golden timelines.
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import type { EdlLike } from './framePlan'
import type { SourceInfoJson } from './frameMap'
import { buildProgramMap, KIND_BLEND, lookupFromJson } from './programMap'
import {
  classify, CUSTOM_TRANSITIONS, MODE_APPROX, MODE_BAKED, MODE_EXACT, MODE_PENDING, PHASE_CAPS,
  POST_TRANSITIONS, type Phase,
} from './support'

interface Case { name: string; edl: EdlLike; sources: Record<string, SourceInfoJson> }
const load = (group: string): Case[] => JSON.parse(readFileSync(fileURLToPath(
  new URL(`../../../../../tests/goldens/frame_map/${group}.json`, import.meta.url)), 'utf8')).cases
const byName = (group: string, name: string) => load(group).find((c) => c.name === name)!

function mapOf(c: Case, edit?: (e: EdlLike) => void) {
  const edl = structuredClone(c.edl)
  edit?.(edl)
  return { edl, pm: buildProgramMap(edl, lookupFromJson(c.sources)) }
}
const v1 = (e: EdlLike) => e.tracks!.find((t) => t.id === 'v1')!
const clip = (e: EdlLike, i: number) => v1(e).clips[i] as Record<string, unknown>

describe('support.classify (§7)', () => {
  const gaps = byName('structure', 'gaps_p30')

  it('plain cuts, gaps, speed and reverse are EXACT in phase 1', () => {
    const { edl, pm } = mapOf(byName('rates', 'rates_p30_s30'))
    const s = classify(pm, edl, { phase: 1 })
    expect(s.engine).toEqual({ ok: true })
    // Varispeed clips (keep_pitch false in the goldens) are sample-exact;
    // only the reversed 2x clip's sound (a resampled intermediate) is not.
    const rev2 = pm.clips.findIndex((c) => c.reverse && c.speed === 2)
    const [a, b] = [pm.clipStart[rev2], pm.clipStart[rev2] + pm.clipLen[rev2]]
    expect(s.ranges).toEqual([
      { k0: 0, k1: a, mode: MODE_EXACT, reasons: [] },
      { k0: a, k1: b, mode: MODE_APPROX, reasons: ['audio:reverse-speed'] },
      { k0: b, k1: pm.total, mode: MODE_EXACT, reasons: [] },
    ])
    expect(classify(mapOf(gaps).pm, mapOf(gaps).edl, { phase: 1 }).ranges.every((r) => r.mode === MODE_EXACT)).toBe(true)
  })

  it('colour is BAKED in P1 and EXACT from P2; flips are geometry', () => {
    const { edl, pm } = mapOf(gaps, (e) => {
      clip(e, 1).effects = [{ type: 'color', params: { saturation: 1.4 } }]
      clip(e, 2).effects = [{ type: 'hflip', params: {} }]
    })
    const p1 = classify(pm, edl, { phase: 1 })
    const k0 = pm.clipStart[1]
    const k1 = k0 + pm.clipLen[1]
    expect(p1.ranges).toContainEqual({ k0, k1, mode: MODE_BAKED, reasons: ['effect:color'] })
    expect(p1.mode[pm.clipStart[2]]).toBe(MODE_EXACT)
    expect(classify(pm, edl, { phase: 2 }).mode[k0]).toBe(MODE_EXACT)
  })

  it('keyframed effect params and unknown effects stay BAKED in every phase', () => {
    const { edl, pm } = mapOf(gaps, (e) => {
      clip(e, 0).effects = [{ type: 'blur', params: { radius: [[0, 1], [1, 4]] } }]
      clip(e, 3).effects = [{ type: 'mystery', params: {} }]
    })
    for (const phase of [1, 4, 5] as Phase[]) {
      const s = classify(pm, edl, { phase })
      expect(s.mode[pm.clipStart[0]]).toBe(MODE_BAKED)
      expect(s.mode[pm.clipStart[3]]).toBe(MODE_BAKED)
    }
  })

  it('transitions: BAKED in P1/P2, native ports APPROX in P3, custom/post always BAKED, nested BAKED', () => {
    const c = byName('transitions', 'xfade_edge_p30')
    const { edl, pm } = mapOf(c)
    const blend = [...pm.kind.keys()].filter((k) => pm.kind[k] === KIND_BLEND)
    const plain = [...pm.kind.keys()].find((k) => pm.kind[k] !== KIND_BLEND)!
    expect(blend.length).toBeGreaterThan(10)
    for (const k of blend) expect(classify(pm, edl, { phase: 1 }).mode[k]).toBe(MODE_BAKED)
    const p3 = classify(pm, edl, { phase: 3 })
    const nested = blend.filter((k) => pm.nested[k])
    expect(nested.length).toBeGreaterThan(0)
    for (const k of blend) expect(p3.mode[k]).toBe(pm.nested[k] ? MODE_BAKED : MODE_APPROX)
    expect(p3.mode[plain]).toBe(MODE_EXACT)
    const glitch = mapOf(c, (e) => { v1(e).transitions = v1(e).transitions!.map((t) => ({ ...t, type: 'glitch' })) })
    const g3 = classify(glitch.pm, glitch.edl, { phase: 3 })
    for (const k of blend) expect(g3.mode[k]).toBe(MODE_BAKED)
    expect(g3.ranges.some((r) => r.reasons.includes('transition:glitch'))).toBe(true)
  })

  it('keep-pitch speed sound is APPROX until the tempo sidecars of P4', () => {
    const { edl, pm } = mapOf(gaps, (e) => {
      Object.assign(clip(e, 2), { speed: 2, out: (clip(e, 2).in as number) + 2 * ((clip(e, 2).out as number) - (clip(e, 2).in as number)) })
      clip(e, 2).audio = { keep_pitch: true }
    })
    const k = pm.clipStart[2]
    expect(classify(pm, edl, { phase: 1 }).mode[k]).toBe(MODE_APPROX)
    expect(classify(pm, edl, { phase: 4 }).mode[k]).toBe(MODE_EXACT)
  })

  it('a retimed REVERSED clip has no sample-exact sound: APPROX in every phase', () => {
    const { edl, pm } = mapOf(gaps, (e) => { Object.assign(clip(e, 1), { reverse: true, speed: 2 }) })
    for (const phase of [1, 4] as Phase[]) {
      const s = classify(pm, edl, { phase })
      expect(s.mode[pm.clipStart[1]]).toBe(MODE_APPROX)
      expect(s.ranges.some((r) => r.reasons.includes('audio:reverse-speed'))).toBe(true)
    }
  })

  it('proxy readiness: pending → PENDING, failed → degraded BAKED, per-span holes', () => {
    const { edl, pm } = mapOf(gaps)
    const pending = classify(pm, edl, { phase: 1, proxyState: () => 'pending' })
    expect(pending.mode[pm.clipStart[0]]).toBe(MODE_PENDING)
    const failed = classify(pm, edl, { phase: 1, proxyState: () => 'failed' })
    expect(failed.ranges.find((r) => r.mode === MODE_BAKED)!.reasons).toEqual(['proxy:degraded'])
    const f0 = pm.srcFrame[pm.clipStart[1]]
    const holes = classify(pm, edl, { phase: 1, spanReady: (_s, f) => f !== f0 })
    expect(holes.ranges.filter((r) => r.mode === MODE_PENDING)).toEqual([
      { k0: pm.clipStart[1], k1: pm.clipStart[1] + 1, mode: MODE_PENDING, reasons: ['proxy:pending'] }])
  })

  it('a structural mismatch (R14) demotes exactly its range to BAKED', () => {
    const { edl, pm } = mapOf(gaps)
    const s = classify(pm, edl, { phase: 1, demote: [[10, 20]] })
    expect(s.ranges.filter((r) => r.mode !== MODE_EXACT)).toEqual([
      { k0: 10, k1: 20, mode: MODE_BAKED, reasons: ['structure:mismatch'] }])
  })

  it('a ducked music bed is APPROX over its render window until the curve lands', () => {
    const { edl, pm } = mapOf(gaps, (e) => {
      const music = e.tracks!.find((t) => t.id === 'music')!
      ;(music as Record<string, unknown>).duck = { to_db: -18, track_ref: 'a1' }
      ;(music.clips[0] as Record<string, unknown>).out = 1.0
    })
    const s = classify(pm, edl, { phase: 1 })
    expect(s.ranges[0]).toEqual({ k0: 0, k1: 30, mode: MODE_APPROX, reasons: ['audio:duck'] })
    expect(classify(pm, edl, { phase: 2, duckCurveReady: true }).ranges[0].mode).toBe(MODE_EXACT)
    const stale = classify(pm, edl, { phase: 1, loudnessCurrent: false })
    expect(stale.mode.every((m) => m === MODE_APPROX)).toBe(true)
    expect(stale.ranges.every((r) => r.reasons.includes('audio:loudness'))).toBe(true)
  })

  it('a non-standard project rate refuses the engine (R1)', () => {
    const { edl, pm } = mapOf(gaps, (e) => { e.canvas = { ...e.canvas, fps: 7 } })
    expect(classify(pm, edl, { phase: 1 }).engine).toEqual({ ok: false, reason: 'rate' })
  })

  it('the capability table only ever improves with the phase', () => {
    const keys = Object.keys(PHASE_CAPS[1]) as (keyof typeof PHASE_CAPS[1])[]
    for (const k of keys) {
      for (let p = 2; p <= 5; p++) expect(PHASE_CAPS[p as Phase][k]).toBeLessThanOrEqual(PHASE_CAPS[(p - 1) as Phase][k])
    }
  })

  it('knows every server transition that has no client port', () => {
    const kinds = JSON.parse(readFileSync(fileURLToPath(
      new URL('../../../../../tests/goldens/transition_kinds.json', import.meta.url)), 'utf8'))
    expect([...CUSTOM_TRANSITIONS].sort()).toEqual([...kinds.custom].sort())
    expect([...POST_TRANSITIONS].sort()).toEqual([...kinds.post].sort())
  })
})
