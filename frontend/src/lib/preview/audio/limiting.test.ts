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

  it('a voice effect or a muted clip', () => {
    const p = plan(bed({ voice_effect: 'robot', voice_intensity: 1 }), () => 0.01)
    const m = p.clips.find((c) => c.id === 'm') as ClipAudio
    expect(clipPeakBound(m, 1, () => 0.01)).toBe(Infinity)
    expect(clipPeakBound({ ...m, mute: true }, 1, () => 0.01)).toBe(0)
    expect(p.limiting.length).toBe(1)
  })
})
