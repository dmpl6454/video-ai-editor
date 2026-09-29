// Where the master limiter works (gate RX finding 3): the browser's limiter is
// not the export's alimiter, so a stretch whose pre-limiter peak may top the
// ceiling is APPROX. The WK/Chromium/PW-WebKit parity of an over-ceiling mix
// against the server is tests/wk/test_wk_audio.py (`mix_hot`).
import { describe, expect, it } from 'vitest'
import type { EdlLike } from '../timeline/framePlan'
import type { SourceInfo } from '../timeline/frameMap'
import { buildProgramMap } from '../timeline/programMap'
import { planFromProgram, type AudioPlan, type ClipAudio } from './audioPlan'
import { compileEnv } from './curves'
import { clipPeakBound, envMaxLinear, LIMITING_SPREAD } from './limiting'
import { voicePeakBound } from '../../voice/voiceFx'

const SRC: SourceInfo = { rate: { num: 30, den: 1 }, tb: { num: 1, den: 15360 }, frames: 30 * 600, startTicks: 0, w: 64, h: 36 }
const lookup = () => SRC

interface ClipSpec { id: string; src?: string; start: number; in?: number; out: number; audio?: Record<string, unknown>; speed?: number }

function edl(parts: { v1?: ClipSpec[]; music?: ClipSpec[]; a1?: ClipSpec[]; v2?: ClipSpec[]; muted?: string[] }): EdlLike {
  const clip = (c: ClipSpec) => ({ src: c.src ?? '/m/a.mp4', in: c.in ?? 0, speed: null, reverse: false, audio: {}, ...c })
  const track = (id: string, type: string, clips: ClipSpec[] = []) =>
    ({ id, type, clips: clips.map(clip), transitions: [], muted: (parts.muted ?? []).includes(id), solo: false })
  const e: EdlLike = {
    canvas: { fps: 30, w: 64, h: 36, loudness_lufs: null },
    tracks: [track('v1', 'video', parts.v1), track('v2', 'video', parts.v2), track('a1', 'audio', parts.a1),
      track('music', 'music', parts.music), track('vo', 'vo')],
  }
  let d = 0
  for (const t of e.tracks!) for (const c of t.clips as ClipSpec[]) d = Math.max(d, c.start + (c.out - (c.in ?? 0)) / (c.speed || 1))
  e.duration = d
  return e
}

const plan = (e: EdlLike, peak: (src: string) => number | null): AudioPlan =>
  planFromProgram(e, buildProgramMap(e, lookup), lookup, { peak: (src) => peak(src) })

// v1 [0, 4 s), a bed on the music lane [1, 3 s).
const bed = (audio: Record<string, unknown> = {}, muted: string[] = []) =>
  edl({ v1: [{ id: 'a', start: 0, out: 4 }], music: [{ id: 'm', src: '/m/bed.m4a', start: 1, in: 0, out: 2, audio }], muted })

describe('limiting ranges', () => {
  it('stay empty while the bound on the summed peak is under the ceiling', () => {
    const p = plan(bed(), () => 0.3)                   // (0.3 + 0.3) / 0.97 = 0.62
    expect(p.master.ceilingDb).toBe(0)
    expect(p.limiting).toEqual([])
    expect(p.approx).not.toContain('limiting')
  })

  it('cover the overlap (± the limiters\' spread) where the bound tops it', () => {
    const p = plan(bed(), () => 0.6)                   // 1.2 / 0.97 = 1.24 > 1
    expect(p.limiting).toEqual([[48000 - LIMITING_SPREAD, 3 * 48000 + LIMITING_SPREAD]])
    expect(p.approx).toContain('limiting')
  })

  // P2 limiter tail: through the alimiter worklet the mix over the ceiling
  // is the server's, so the plan names no range (no "Limiter on loud sound").
  it('are none when the mix plays through the alimiter worklet', () => {
    const e = bed()
    const p = planFromProgram(e, buildProgramMap(e, lookup), lookup, { peak: () => 0.6, exactLimiter: true })
    expect(p.master.ceilingDb).toBe(0)
    expect(p.limiting).toEqual([])
    expect(p.approx).not.toContain('limiting')
  })

  it('count the clip gain, the envelope maximum and the bus gain', () => {
    expect(plan(bed({ gain_db: -12 }), () => 0.6).limiting).toEqual([])       // 0.6 + 0.15 → 0.77
    expect(plan(bed({ gain_db: -12, gain_env: { keyframes: [[0, 0], [1, 12]] } }), () => 0.6).limiting.length).toBe(1)
    expect(plan(bed({}, ['music']), () => 0.6).limiting).toEqual([])        // a muted lane adds nothing
  })

  it('an unknown peak is unbounded (APPROX until the layout lands)', () => {
    const p = plan(bed(), (src) => (src === '/m/bed.m4a' ? null : 0.1))
    expect(p.limiting).toEqual([[48000 - LIMITING_SPREAD, 3 * 48000 + LIMITING_SPREAD]])
  })

  it('a timeline with no limiter has no limiting range, however hot', () => {
    const p = plan(edl({ v1: [{ id: 'a', start: 0, out: 4 }] }), () => 3)
    expect(p.master.ceilingDb).toBeNull()
    expect(p.limiting).toEqual([])
  })

  // Final sweep 2: one hot 5 s chunk anywhere in a 200 s talking head put
  // "≈ Limiter on loud sound" over all 200 s — the clip was bounded once by
  // its whole source span. A sample-exact clip is bounded chunk by chunk.
  it('one hot chunk in a long clip flags that chunk\'s stretch, not the whole clip', () => {
    const CHUNK = 240000
    const e = edl({ v1: [{ id: 'a', start: 0, out: 200 }] })
    ;(e.canvas as { loudness_lufs: number | null }).loudness_lufs = -16
    const peak = (_src: string, s0: number, s1: number) => {
      let p = 0
      for (let n = Math.floor(s0 / CHUNK); n < Math.ceil(s1 / CHUNK); n++) p = Math.max(p, n === 29 ? 0.753 : 0.70)
      return p
    }
    const p = planFromProgram(e, buildProgramMap(e, lookup), lookup, { peak, loudnessGainDb: 1.89 })
    expect(p.master.ceilingDb).toBe(-1)
    expect(p.limiting.length).toBe(1)
    const [a, b] = p.limiting[0]
    expect(a).toBeGreaterThanOrEqual(29 * CHUNK - LIMITING_SPREAD)
    expect(b).toBeLessThanOrEqual(30 * CHUNK + LIMITING_SPREAD)
    // never less than it should: the hot chunk itself is covered
    expect(a).toBeLessThanOrEqual(29 * CHUNK)
    expect(b).toBeGreaterThanOrEqual(30 * CHUNK)
    expect(p.approx).toContain('limiting')
    // and none at all when no chunk is hot
    const cool = planFromProgram(e, buildProgramMap(e, lookup), lookup, { peak: () => 0.70, loudnessGainDb: 1.89 })
    expect(cool.limiting).toEqual([])
  })

  it('two overlapping lanes: the Σ is still taken per overlapping stretch', () => {
    // v1 hot only in its 3rd second, a bed at 0.5 everywhere over [1, 3 s):
    // 0.6 + 0.5 tops 1 only where the hot second meets the bed.
    const e = edl({ v1: [{ id: 'a', start: 0, out: 4 }], music: [{ id: 'm', src: '/m/bed.m4a', start: 1, in: 0, out: 2 }] })
    const peak = (src: string, s0: number, s1: number) => {
      if (src === '/m/bed.m4a') return 0.5
      return s0 < 3 * 48000 && s1 > 2 * 48000 ? 0.6 : 0.3
    }
    const p = planFromProgram(e, buildProgramMap(e, lookup), lookup, { peak })
    expect(p.limiting).toEqual([[2 * 48000 - LIMITING_SPREAD, 3 * 48000 + LIMITING_SPREAD]])
  })

  it('a lone hot clip on a mixed timeline is flagged on its own stretch only', () => {
    const e = edl({ v1: [{ id: 'a', start: 0, out: 4 }], a1: [{ id: 'q', src: '/m/q.m4a', start: 5, in: 0, out: 1 }] })
    const p = plan(e, (src) => (src === '/m/q.m4a' ? 0.1 : 1.2))
    expect(p.limiting).toEqual([[0, 4 * 48000 + LIMITING_SPREAD]])
  })
})

describe('bounds', () => {
  it('envMaxLinear is the loudest envelope point', () => {
    expect(envMaxLinear(null)).toBe(1)
    expect(envMaxLinear(compileEnv({ keyframes: [[0, -6], [1, 6], [2, -20]] }))).toBeCloseTo(Math.pow(10, 6 / 20), 12)
    expect(envMaxLinear(compileEnv({ keyframes: [[0, -3]] }))).toBeCloseTo(Math.pow(10, -3 / 20), 12)
  })

  // K2 (0.8.0 QA): a voice effect was unbounded outright, so every voice
  // effect on a project with a loudness target — an EXACT echo too — said
  // "≈ Limiter on loud sound". Its stages bound it now (voicePeakBound, which
  // voiceFx.test.ts proves never too low); a hot one is still flagged.
  it('a voice effect is bounded by its stages; a muted clip is silent', () => {
    const p = plan(bed({ voice_effect: 'robot', voice_intensity: 1 }), () => 0.01)
    const m = p.clips.find((c) => c.id === 'm') as ClipAudio
    expect(clipPeakBound(m, 1, () => 0.01)).toBeCloseTo(voicePeakBound(m.voice!, 0.01), 12)
    expect(clipPeakBound(m, 1, () => 0.01)).toBeGreaterThan(0.01)            // the echo taps add up
    expect(clipPeakBound(m, 1, () => null)).toBe(Infinity)                    // unknown peak: unbounded
    expect(clipPeakBound({ ...m, mute: true }, 1, () => 0.01)).toBe(0)
    expect(p.limiting.length).toBe(0)
    expect(plan(bed({ voice_effect: 'robot', voice_intensity: 1 }), () => 0.9).limiting.length).toBe(1)
  })

  it('a primed voice effect reads the prime before the head (its peak counts)', () => {
    const p = plan(edl({ v1: [{ id: 'a', start: 0, in: 2, out: 4, audio: { voice_effect: 'deep' } }] }), () => 0.1)
    const a = p.clips.find((c) => c.id === 'a') as ClipAudio
    expect(a.voice!.prime).toBeGreaterThan(0)
    const asked: Array<[number, number]> = []
    clipPeakBound(a, 1, (_s, s0, s1) => { asked.push([s0, s1]); return 0.1 })
    expect(asked[0][0]).toBe(2 * 48000 - a.voice!.prime)
  })
})

// Final QA (engine): the server's preview of a mixed programme limits the RAW
// mix at 0.97 (auto-levelled to 0 dBFS) BEFORE the loudness gain, then limits
// again at −1 dBFS. The plan had one stage after the whole gain, so under a
// negative loudness gain a loud mix never reached its ceiling: the Instant
// preview played it up to 4 dB louder than the server render and the export,
// EXACT and with no chip.
describe('a mixed programme under a loudness gain (two limiter stages)', () => {
  const loud = (peak: number, gainDb: number) => {
    const e = bed()
    ;(e.canvas as { loudness_lufs: number | null }).loudness_lufs = -16
    return planFromProgram(e, buildProgramMap(e, lookup), lookup, { peak: () => peak, loudnessGainDb: gainDb })
  }

  it('stages the master as the server preview does: mix limiter, loudness gain, −1 dBFS limiter', () => {
    const p = loud(0.8, -12)
    expect(p.master.gain).toBeCloseTo(1 / 0.97, 15)
    expect(p.master.ceilingDb).toBe(0)
    expect(p.master.post!.gain).toBeCloseTo(Math.pow(10, -12 / 20), 15)
    expect(p.master.post!.ceilingDb).toBe(-1)
  })

  it('flags where the mix limiter works, however far the loudness gain lowers it', () => {
    const p = loud(0.8, -12)                            // (0.8 + 0.8) / 0.97 = 1.65 > 0 dBFS
    expect(p.limiting).toEqual([[48000 - LIMITING_SPREAD, 3 * 48000 + LIMITING_SPREAD]])
    expect(p.approx).toContain('limiting')
  })

  it('flags where a positive gain lifts a mix under 0 dBFS over −1 dBFS', () => {
    const p = loud(0.45, 3)                             // 0.9 / 0.97 = 0.93, × 1.41 = 1.31 > 0.891
    expect(p.limiting).toEqual([[48000 - LIMITING_SPREAD, 3 * 48000 + LIMITING_SPREAD]])
  })

  it('stays EXACT where neither stage is reached', () => {
    expect(loud(0.3, -12).limiting).toEqual([])
    expect(loud(0.3, 1).limiting).toEqual([])           // 0.62 × 1.12 = 0.69
  })
})
