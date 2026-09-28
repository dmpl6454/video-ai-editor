// Voice effects in the mix graph (wave E, F3), on the recording fake of Web
// Audio: a voice clip's blocks hold exactly `voiceFx.processStereo` of its
// own samples (the channel mode applied first, zero outside the clip), so
// blocks cut on the 1 s grid join into the whole clip's effect; the node
// matrix is then the identity; the Hall convolves with the export's IR,
// unnormalised, and falls silent at the clip's end; an effect edit is a
// timing edit (the lane reschedules); the sound is APPROX in support.ts.
import { describe, expect, it } from 'vitest'
import type { EdlLike } from '../timeline/framePlan'
import type { SourceInfo } from '../timeline/frameMap'
import { buildProgramMap } from '../timeline/programMap'
import { classify, MODE_APPROX, MODE_EXACT, VOICE_FX_PARITY } from '../timeline/support'
import { processStereo, reverbIr, voicePlan } from '../../voice/voiceFx'
import type { PcmReader } from './audioChunks'
import { diffPlans, planFromProgram, type AudioPlan } from './audioPlan'
import { asCtx, FakeContext, FakeConvolver, FakeGain, FakeOfflineContext, FakeSource } from './fakeAudio'
import { BLOCK_SAMPLES, MixGraph, primeOf, sourceRange } from './mixGraph'

const SR = 48000
const SRC: SourceInfo = { rate: { num: 30, den: 1 }, tb: { num: 1, den: 15360 }, frames: 30 * 600, startTicks: 0, w: 64, h: 36 }

/** Source sample s: L = sin-ish ramp, R = its negative (recognisable). */
const valL = (s: number) => Math.sin(s * 0.013) * 0.5
const valR = (s: number) => Math.cos(s * 0.007) * 0.25

function reader(): PcmReader {
  return {
    async load() { /* in memory */ },
    ready() { return true },
    silent() { return false },
    copy(_src, first, count, dir, L, R, off) {
      for (let i = 0; i < count; i++) {
        const s = first + dir * i
        if (s < 0) continue
        L[off + i] = valL(s)
        R[off + i] = valR(s)
      }
      return true
    },
  }
}

function edl(audio: Record<string, unknown>, extra: Record<string, unknown> = {}, vo?: Record<string, unknown>): EdlLike {
  const e: EdlLike = {
    canvas: { fps: 30 },
    tracks: [
      { id: 'v1', type: 'video', clips: [{ id: 'a', src: '/a.mp4', in: 1, out: 4, start: 0, speed: null, reverse: false, audio, ...extra }], transitions: [] },
      { id: 'vo', type: 'vo', clips: vo ? [{ id: 'v', src: '/v.m4a', in: 0, out: 2, start: 0.5, speed: null, reverse: false, audio: vo }] : [], transitions: [] },
    ],
  }
  e.duration = 3
  return e
}

const planOf = (e: EdlLike): AudioPlan => planFromProgram(e, buildProgramMap(e, () => SRC), () => SRC)

function render(plan: AudioPlan): { ctx: FakeContext; g: MixGraph } {
  const ctx = new FakeOfflineContext()
  const g = new MixGraph(asCtx(ctx), plan, { reader: reader() })
  g.restart({ ctxTime: 0, sample: 0 }, plan.total)
  return { ctx, g }
}

/** The clip's expected effected samples, computed on the whole clip at once
 *  — the export's stream: primed with `primeOf(c)` samples of real sound
 *  before the head for a pitch / vibrato effect (review RE), the stages
 *  counted from there, the priming then dropped. */
function whole(plan: AudioPlan, i = 0): [Float64Array, Float64Array] {
  const c = plan.clips[i]
  const pr = primeOf(c)
  const L = new Float64Array(pr + c.n)
  const R = new Float64Array(pr + c.n)
  const run = c.map.kind === 'runs' ? c.map.runs[0] : null
  for (let j = 0; j < pr + c.n; j++) {
    const s = run ? run[2] - pr + j : 0
    L[j] = Math.fround(valL(s))
    R[j] = Math.fround(valR(s))
  }
  const [yL, yR] = processStereo(c.voice!, L, R, 0)
  return [yL.subarray(pr), yR.subarray(pr)]
}

describe('a voice clip in the mix graph', () => {
  it('primes a pitch / vibrato effect with the source before the head, never before the file (review RE)', () => {
    const chip = planOf(edl({ voice_effect: 'chipmunk' })).clips[0]        // in = 1 s: 48000 samples before
    expect(chip.voice?.prime).toBe(2400)
    expect(primeOf(chip)).toBe(2400)
    // a block at the head reads back its margin, into the priming
    expect(sourceRange(chip, chip.out0, chip.out0 + 10)![0]).toBe(48000 - Math.min(2400, chip.voice!.back))
    expect(primeOf(planOf(edl({ voice_effect: 'echo' })).clips[0])).toBe(0)      // no latency, no priming
    // final QA round 3 (`audio_mix.PRIMED_STAGES`): the filter presets' IIR
    // state is primed too, or every split clicks; the ring / echo are not
    for (const fx of ['telephone', 'radio', 'megaphone', 'underwater', 'monster']) {
      expect(primeOf(planOf(edl({ voice_effect: fx })).clips[0])).toBe(2400)
    }
    expect(primeOf(planOf(edl({ voice_effect: 'robot' })).clips[0])).toBe(0)
    const atHead = planFromProgram(edl({ voice_effect: 'vibrato' }, { in: 0.02, out: 3.02 }),
      buildProgramMap(edl({ voice_effect: 'vibrato' }, { in: 0.02, out: 3.02 }), () => SRC), () => SRC).clips[0]
    expect(primeOf(atHead)).toBe(960)                                          // clamped at the file head
  })

  it('plays blocks that join into the whole clip\'s effect, sample for sample', () => {
    for (const effect of ['echo', 'chipmunk', 'megaphone', 'underwater']) {
      const plan = planOf(edl({ voice_effect: effect }))
      const c = plan.clips[0]
      expect(c.voice?.effect).toBe(effect)
      const { ctx } = render(plan)
      const [wL, wR] = whole(plan)
      const srcs = ctx.sources.filter((s) => s.buffer)
      expect(srcs.length).toBe(Math.ceil(c.n / BLOCK_SAMPLES))
      let worst = 0
      for (const s of srcs) {
        const p0 = Math.round(s.startedAt! * SR)
        const b = s.buffer!
        for (let j = 0; j < b.length; j++) {
          worst = Math.max(worst, Math.abs(b.data[0][j] - wL[p0 + j - c.out0]), Math.abs(b.data[1][j] - wR[p0 + j - c.out0]))
        }
      }
      expect(worst, effect).toBeLessThan(1e-6)
    }
  })

  it('applies the channel mode to the samples, before the effect, and leaves the node matrix at identity', () => {
    const plan = planOf(edl({ voice_effect: 'telephone', channels: 'left' }))
    const { ctx } = render(plan)
    const b = ctx.sources[0].buffer!
    for (let j = 0; j < 2000; j += 97) expect(b.data[1][j]).toBeCloseTo(b.data[0][j], 7)
    const gains = ctx.nodes.filter((n): n is FakeGain => n instanceof FakeGain).map((g) => g.gain.value)
    // [1, 0, 0, 1] (the four matrix gains follow the splitter)
    expect(gains.slice(0, 20).join(',')).toContain('1,0,0,1')
  })

  it('convolves the Hall with the export\'s impulse response, unnormalised, and stops its tail at the clip\'s end', () => {
    const plan = planOf(edl({ voice_effect: 'reverb' }))
    const { ctx } = render(plan)
    const conv = ctx.nodes.find((n): n is FakeConvolver => n instanceof FakeConvolver)!
    expect(conv).toBeTruthy()
    expect(conv.normalize).toBe(false)
    const ir = reverbIr(plan.clips[0].voice!.reverb!, 1)
    expect(conv.buffer!.data[1].subarray(0, 4000)).toEqual(ir.subarray(0, 4000))
    const shape = conv.outputs[0].to as FakeGain
    const end = plan.clips[0].out0 + plan.clips[0].n
    const last = shape.gain.events[shape.gain.events.length - 1]
    expect(last).toEqual({ type: 'set', v: 0, t: end / SR })
  })

  it('reads its margins: the source range of a block reaches back for the echo', () => {
    const plan = planOf(edl({ voice_effect: 'echo' }))
    const c = plan.clips[0]
    const [lo] = sourceRange(c, c.out0 + 2 * BLOCK_SAMPLES, c.out0 + 3 * BLOCK_SAMPLES)!
    const first = c.map.kind === 'runs' ? c.map.runs[0][2] : 0
    expect(lo).toBe(first + 2 * BLOCK_SAMPLES - voicePlan('echo', 1)!.back)
  })

  it('works on a lane clip and a resampled clip', () => {
    const plan = planOf(edl({}, { speed: 2, audio: { voice_effect: 'deep', keep_pitch: false } }, { voice_effect: 'robot', voice_intensity: 0.4 }))
    expect(plan.clips.map((c) => c.voice?.effect)).toEqual(['deep', 'robot'])
    expect(plan.clips[1].voice!.intensity).toBe(0.4)
    const { ctx } = render(plan)
    const max = ctx.sources.reduce((m, s) => s.buffer!.data[0].reduce((q, v) => Math.max(q, Math.abs(v)), m), 0)
    expect(max).toBeGreaterThan(0.05)
    expect(ctx.sources.every((s: FakeSource) => s.playbackRate.value === 1)).toBe(true)   // resampled in the block
    expect(plan.approx).toContain('voice')
  })

  it('an effect or intensity edit reschedules the lane; no effect keeps the old timing', () => {
    const a = planOf(edl({}))
    const b = planOf(edl({ voice_effect: 'echo' }))
    const c = planOf(edl({ voice_effect: 'echo', voice_intensity: 0.5 }))
    expect(a.clips[0].voice).toBeNull()
    expect(a.clips[0].timing).not.toContain('voice')
    expect(diffPlans(a, b).dirtyBuses.has('v1')).toBe(true)
    expect(diffPlans(b, c).dirtyBuses.has('v1')).toBe(true)
    expect(diffPlans(b, planOf(edl({ voice_effect: 'echo' }))).dirtyBuses.size).toBe(0)
  })
})

describe('support: a voice effect is classed by VOICE_FX_PARITY on every lane', () => {
  it('marks the APPROX presets (pitch, Hall) on v1 and the voice-over, and leaves the EXACT ones unmarked', () => {
    const e = edl({ voice_effect: 'chipmunk' }, {}, { voice_effect: 'reverb' })
    const pm = buildProgramMap(e, () => SRC)
    const s = classify(pm, e, { phase: 1 })
    const reasons = new Set(s.ranges.flatMap((r) => r.reasons))
    expect(reasons.has('audio:voice:chipmunk')).toBe(true)
    expect(reasons.has('audio:voice:reverb')).toBe(true)
    expect(Math.max(...s.mode)).toBe(MODE_APPROX)
    for (const exact of ['robot', 'echo', 'telephone', 'megaphone', 'radio', 'underwater', 'vibrato']) {
      const ex = edl({ voice_effect: exact }, {}, { voice_effect: exact })
      const r = classify(buildProgramMap(ex, () => SRC), ex, { phase: 1 })
      expect(r.ranges.flatMap((x) => x.reasons).some((x) => x.startsWith('audio:voice')), exact).toBe(false)
      expect(VOICE_FX_PARITY[exact as keyof typeof VOICE_FX_PARITY].mode).toBe(MODE_EXACT)
    }
    const none = classify(buildProgramMap(edl({}), () => SRC), edl({}), { phase: 1 })
    expect(none.ranges.flatMap((r) => r.reasons).some((r) => r.startsWith('audio:voice'))).toBe(false)
  })
})
