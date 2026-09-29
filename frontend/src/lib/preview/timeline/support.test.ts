// The fidelity classes of spec §7, per phase (§12), on golden timelines.
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import type { EdlLike } from './framePlan'
import type { SourceInfoJson } from './frameMap'
import { buildProgramMap, KIND_BLEND, lookupFromJson } from './programMap'
import {
  classify, CUSTOM_TRANSITIONS, LOUDNESS_AUDIBLE_DB, MODE_APPROX, MODE_BAKED, MODE_EXACT, MODE_PENDING, PHASE_CAPS,
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

  it('plain cuts, gaps and reverse are EXACT in phase 1; retimed sound is not', () => {
    const { edl, pm } = mapOf(byName('rates', 'rates_p30_s30'))
    const s = classify(pm, edl, { phase: 1 })
    expect(s.engine).toEqual({ ok: true })
    // Every PICTURE here is exact. The sound of a retimed clip is not: the
    // speed curve's (a warped intermediate), the reversed 2x clip's (a
    // resampled intermediate) and — Final QA r3 — every constant-speed clip's
    // with Keep pitch off (keep_pitch false in the goldens), which the client
    // plays as a playbackRate resample, not ffmpeg's (audioPlan.ts marks it
    // 'varispeed'; tests/wk/audio_fixture.py measures it at the APPROX 1 dB).
    // This test used to call those clips sample-exact, from before the D2
    // audio engine existed.
    const span = (i: number) => [pm.clipStart[i], pm.clipStart[i] + pm.clipLen[i]]
    const expected: Array<{ k0: number; k1: number; mode: number; reasons: string[] }> = []
    let k = 0
    const push = (k0: number, k1: number, mode: number, reasons: string[]) => {
      const last = expected[expected.length - 1]
      if (last && last.k1 === k0 && last.mode === mode && last.reasons.join() === reasons.join()) last.k1 = k1
      else expected.push({ k0, k1, mode, reasons })
    }
    pm.clips.forEach((c, i) => {
      const [a, b] = span(i)
      const why = typeof c.speed === 'object' && c.speed !== null ? 'audio:curve'
        : typeof c.speed === 'number' && c.speed !== 1 ? (c.reverse ? 'audio:reverse-speed' : 'audio:varispeed') : null
      if (!why) return
      if (a > k) push(k, a, MODE_EXACT, [])
      push(a, b, MODE_APPROX, [why])
      k = b
    })
    if (k < pm.total) push(k, pm.total, MODE_EXACT, [])
    expect(expected.map((r) => r.reasons[0] ?? '')).toEqual(['', 'audio:varispeed', 'audio:curve', '',
      'audio:reverse-speed', 'audio:varispeed', ''])
    expect(s.ranges).toEqual(expected)
    expect(classify(mapOf(gaps).pm, mapOf(gaps).edl, { phase: 1 }).ranges.every((r) => r.mode === MODE_EXACT)).toBe(true)
  })

  it('a freeze is EXACT (one proxy frame, silence); curve sound is APPROX in every phase', () => {
    const { edl, pm } = mapOf(byName('speed', 'speed_freeze_p30'))
    const s = classify(pm, edl, { phase: 1 })
    expect(s.ranges.every((r) => r.mode === MODE_EXACT || r.reasons.every((x) => x.startsWith('transition')))).toBe(true)
    const fz = pm.clips.findIndex((c) => c.id === 'fz_mid')
    expect(s.mode[pm.clipStart[fz]]).toBe(MODE_EXACT)
    const cv = mapOf(byName('speed', 'speed_curves_p30'))
    for (const phase of [1, 2, 3, 4, 5] as Phase[]) {
      const cs = classify(cv.pm, cv.edl, { phase })
      const ru = cv.pm.clips.findIndex((c) => c.id === 'ru')
      expect(cs.mode[cv.pm.clipStart[ru]]).toBe(MODE_APPROX)
      expect(cs.ranges.find((r) => r.k0 <= cv.pm.clipStart[ru] && cv.pm.clipStart[ru] < r.k1)!.reasons).toEqual(['audio:curve'])
    }
    // A curve on a music lane: its sound is APPROX over its footprint.
    const lane = mapOf(gaps, (e) => {
      e.tracks!.find((t) => t.id === 'music')!.clips = [{
        id: 'mc', src: 'bar30', in: 0, out: 2, start: 0.5, speed: { curve: [[0, 0.5], [1, 2]] },
      }]
    })
    const ls = classify(lane.pm, lane.edl, { phase: 4 })
    expect(ls.ranges.some((r) => r.reasons.includes('audio:curve'))).toBe(true)
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

  it('Keep pitch off: a retimed clip\'s sound is a resample, APPROX in every phase (v1 and overlays)', () => {
    // Final QA r3: support.ts only named keep-pitch speed, while the audio
    // plan plays these as a playbackRate resample (audioPlan approx
    // 'varispeed') — no ≈ over a 2x v1 clip or a 0.5x overlay.
    const { edl, pm } = mapOf(gaps, (e) => {
      Object.assign(clip(e, 2), { speed: 2, audio: { keep_pitch: false } })
      e.tracks!.push({ id: 'v2', type: 'video', clips: [
        { id: 'slow', src: 'bar30', start: 0, in: 0, out: 0.2, speed: 0.5, audio: { keep_pitch: false } },
        { id: 'back', src: 'bar30', start: 2.2, in: 1, out: 1.3, reverse: true },
      ] } as unknown as NonNullable<EdlLike['tracks']>[number])
    })
    for (const phase of [1, 4, 5] as Phase[]) {
      const s = classify(pm, edl, { phase })
      const at = (k: number) => s.ranges.find((r) => k >= r.k0 && k < r.k1)!
      expect(at(pm.clipStart[2])).toMatchObject({ mode: MODE_APPROX, reasons: ['audio:varispeed'] })
      expect(at(5)).toMatchObject({ mode: MODE_APPROX, reasons: ['audio:varispeed'] })     // the 0.5x overlay
      expect(at(70)).toMatchObject({ mode: MODE_APPROX, reasons: ['audio:reverse'] })      // the reversed overlay (1x)
      expect(at(pm.clipStart[1]).mode).toBe(MODE_EXACT)
    }
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

  // Final sweep 3 (HIGH): a music bed whose proxy was not known yet played
  // silent in Instant preview and the range still read EXACT (no chip). A
  // lane clip whose source has no proxy yet is APPROX 'audio:pending' over
  // its window; a muted lane says nothing (nothing is missing).
  it('a lane clip whose sound is not loaded yet is APPROX audio:pending, not EXACT silence', () => {
    const bedOnly = (muted: boolean) => mapOf(gaps, (e) => {
      const music = e.tracks!.find((t) => t.id === 'music')!
      ;(music as Record<string, unknown>).muted = muted
      ;(music.clips[0] as Record<string, unknown>).src = 'bed'
      ;(music.clips[0] as Record<string, unknown>).out = 1.0
    })
    const { edl, pm } = bedOnly(false)
    const s = classify(pm, edl, { phase: 1, proxyState: (src) => (src === 'bed' ? 'pending' : 'ready') })
    expect(s.ranges[0]).toEqual({ k0: 0, k1: 30, mode: MODE_APPROX, reasons: ['audio:pending'] })
    expect(classify(pm, edl, { phase: 1 }).ranges[0].mode).toBe(MODE_EXACT)
    const muted = bedOnly(true)
    const m = classify(muted.pm, muted.edl, { phase: 1, proxyState: (src) => (src === 'bed' ? 'pending' : 'ready') })
    expect(m.ranges.some((r) => r.reasons.includes('audio:pending'))).toBe(false)
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
    const off = classify(pm, edl, { phase: 1, loudnessOffDb: 1.5 })
    expect(off.mode.every((m) => m === MODE_APPROX)).toBe(true)
    expect(off.ranges.every((r) => r.reasons.includes('audio:loudness'))).toBe(true)
  })

  // K2 (0.8.0 QA): every fresh project with the beta on showed "≈ Loudness"
  // until the server had measured the gain. The chip is for a difference a
  // person would notice: a gain not measured yet says nothing, and a measured
  // one only when the sound plays more than 1 dB off it.
  it('the loudness gain is APPROX only when it plays more than 1 dB off the measured one', () => {
    const { edl, pm } = mapOf(gaps)
    const loud = (o?: number) => classify(pm, edl, { phase: 1, loudnessOffDb: o }).ranges
      .some((r) => r.reasons.includes('audio:loudness'))
    expect(loud(undefined)).toBe(false)           // not measured yet: no chip
    expect(loud(0)).toBe(false)
    expect(loud(LOUDNESS_AUDIBLE_DB)).toBe(false) // 1 dB is not a difference anyone hears
    expect(loud(1.01)).toBe(true)
    expect(loud(11)).toBe(true)
  })

  it('a loudness verdict keeps the other reasons of the frame (the chip names each)', () => {
    const { edl, pm } = mapOf(gaps, (e) => {
      const v1 = e.tracks!.find((t) => t.id === 'v1')!
      ;(v1.clips[0] as Record<string, unknown>).audio = { voice_effect: 'deep' }
    })
    const voiced = (o?: number) => classify(pm, edl, { phase: 1, loudnessOffDb: o }).ranges
      .filter((r) => r.reasons.includes('audio:voice:deep'))
    expect(voiced(undefined).length).toBeGreaterThan(0)
    expect(voiced(6).map((r) => [r.k0, r.k1])).toEqual(voiced(undefined).map((r) => [r.k0, r.k1]))
    for (const r of voiced(6)) expect(r.reasons).toEqual(['audio:loudness', 'audio:voice:deep'])
  })

  it('the master limiter\'s ranges are APPROX (gate RX: the browser limiter is not alimiter)', () => {
    const { edl, pm } = mapOf(gaps)
    const s = classify(pm, edl, { phase: 1, limiting: [[-3, 5], [20, 1e9]] })
    expect(s.ranges.filter((r) => r.reasons.includes('audio:limiting')).map((r) => [r.k0, r.k1, r.mode]))
      .toEqual([[0, 5, MODE_APPROX], [20, pm.total, MODE_APPROX]])
    expect(classify(pm, edl, { phase: 1, limiting: [] }).ranges.some((r) => r.reasons.includes('audio:limiting'))).toBe(false)
  })

  // Final sweep 3, run 3: sound the live preview could not play because its
  // chunks were not in memory yet (AudioSink.soundLoadingFrames) is silence
  // the export does not have — APPROX 'audio:pending', never EXACT.
  it('ranges whose sound is still loading are APPROX audio:pending', () => {
    const { edl, pm } = mapOf(gaps)
    const s = classify(pm, edl, { phase: 1, soundLoading: [[-3, 5], [20, 1e9]] })
    expect(s.ranges.filter((r) => r.reasons.includes('audio:pending')).map((r) => [r.k0, r.k1, r.mode]))
      .toEqual([[0, 5, MODE_APPROX], [20, pm.total, MODE_APPROX]])
    expect(classify(pm, edl, { phase: 1, soundLoading: [] }).ranges.some((r) => r.reasons.includes('audio:pending'))).toBe(false)
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
