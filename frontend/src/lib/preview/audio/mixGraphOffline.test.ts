// renderOffline renders until two renders agree (wave E, d2-followups item
// 30): WebKit's AudioBufferSourceNode skips a render quantum — zeros, its
// playhead not advanced — when another thread holds its process lock, so a
// render can come out with one source a quantum late. The fake contexts
// below hand back scripted buffers: the true mix, or one with a skipped
// quantum; the function must return the true mix and never a lone render.
import { describe, expect, it } from 'vitest'
import type { EdlLike } from '../timeline/framePlan'
import type { SourceInfo } from '../timeline/frameMap'
import { buildProgramMap } from '../timeline/programMap'
import type { PcmReader } from './audioChunks'
import { planFromProgram } from './audioPlan'
import { FakeOfflineContext } from './fakeAudio'
import { OFFLINE_RENDER_ATTEMPTS, renderOffline } from './mixGraph'

const SRC: SourceInfo = { rate: { num: 30, den: 1 }, tb: { num: 1, den: 15360 }, frames: 30 * 600, startTicks: 0, w: 64, h: 36 }
const reader: PcmReader = {
  async load() { /* in memory */ },
  ready() { return true },
  silent() { return false },
  copy(_s, first, count, dir, L, R, off) {
    for (let i = 0; i < count; i++) { L[off + i] = (first + dir * i) / 1e7; R[off + i] = -(first + dir * i) / 1e7 }
    return true
  },
}
const edl: EdlLike = {
  canvas: { fps: 30 }, duration: 1,
  tracks: [{ id: 'v1', type: 'video', transitions: [],
             clips: [{ id: 'a', src: '/a.mp4', in: 0, out: 1, start: 0, speed: null, reverse: false, audio: {} }] }],
}
const plan = planFromProgram(edl, buildProgramMap(edl, () => SRC), () => SRC)
const N = 1024

function mix(skipAt: number | null): Float32Array {
  const x = new Float32Array(N)
  for (let j = 0; j < N; j++) x[j] = (j + 1) / N
  if (skipAt === null) return x
  const y = new Float32Array(N)                 // 128 zeros, then the rest a quantum late
  for (let j = skipAt + 128; j < N; j++) y[j] = x[j - 128]
  y.set(x.subarray(0, skipAt), 0)
  return y
}

/** A `make` whose renders come out as `script` says (null: the true mix). */
function scripted(script: Array<number | null>) {
  let n = 0
  const make = (_c: number, length: number) => {
    const skip = script[Math.min(n, script.length - 1)]
    n++
    const ctx = new FakeOfflineContext()
    const L = mix(skip).slice(0, length)
    ;(ctx as unknown as { startRendering: () => Promise<unknown> }).startRendering = () =>
      Promise.resolve({ getChannelData: () => L })
    return ctx as unknown as OfflineAudioContext
  }
  return { make, renders: () => n }
}

describe('renderOffline agreement', () => {
  it('returns the first mix two renders agree on', async () => {
    const s = scripted([null, null])
    const r = await renderOffline(plan, reader, 0, N, s.make)
    expect(Array.from(r.L)).toEqual(Array.from(mix(null)))
    expect(s.renders()).toBe(2)
  })

  it('never returns a render with a skipped quantum', async () => {
    for (const script of [[256, null, null], [null, 640, null, null], [256, 512, null, null]]) {
      const s = scripted(script)
      const r = await renderOffline(plan, reader, 0, N, s.make)
      expect(Array.from(r.L)).toEqual(Array.from(mix(null)))
      expect(s.renders()).toBe(script.length)
    }
  })

  it('fails loudly when no two renders agree', async () => {
    const s = scripted(Array.from({ length: OFFLINE_RENDER_ATTEMPTS }, (_v, i) => 128 * (i + 1)))
    await expect(renderOffline(plan, reader, 0, N, s.make)).rejects.toThrow(/never agreed/)
    expect(s.renders()).toBe(OFFLINE_RENDER_ATTEMPTS)
  })
})
