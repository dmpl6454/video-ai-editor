// The preview's master limiter as an AudioWorklet: ALimiterCore (the port of
// ffmpeg's `alimiter`, alimiter.ts) run per render quantum. The module is
// built from the class's own source text and loaded from a Blob URL, so the
// app, the test pages and any bundler ship the SAME code the vitest golden
// pins (alimiter.test.ts). Where AudioWorklet is missing or refuses the
// module, the graph keeps the DynamicsCompressorNode and the plan calls the
// loud stretches APPROX (limiting.ts) — `limiterWorkletReady` says which.

import { ALimiterCore, alimiterRingLatency } from './alimiter'

export const LIMITER_PROCESSOR = 'vae-alimiter'
/** alimiter's defaults, as the server's preview limiters run them. */
export const ALIMITER_ATTACK_MS = 5
export const ALIMITER_RELEASE_MS = 50

export interface LimiterProcessorOptions {
  /** Linear ceiling (alimiter `limit`). */
  limit: number
  /** alimiter `level` (auto-level: ×1/limit after the clip). */
  autoLevel: boolean
  attackMs: number
  releaseMs: number
  /** Frames from input to output: the ring's own latency plus padding. */
  latency: number
}

/** The worklet module's source text. */
export function limiterWorkletSource(): string {
  return `const ALimiterCore = (${ALimiterCore.toString()});
const ringLatency = (${alimiterRingLatency.toString()});
class VaeALimiter extends AudioWorkletProcessor {
  static get parameterDescriptors() {
    return [{ name: 'limit', defaultValue: 1, minValue: 0.0625, maxValue: 1, automationRate: 'k-rate' }]
  }
  constructor(options) {
    super()
    const o = options.processorOptions
    this.autoLevel = !!o.autoLevel
    this.core = new ALimiterCore(o.limit, this.autoLevel, o.attackMs, o.releaseMs, sampleRate,
      o.latency - ringLatency(sampleRate, o.attackMs))
    this.zero = new Float32Array(128)
    // an AudioParam holds float32; the server writes its limits with 6
    // decimals (\`limit=0.97\`, \`0.891251\`), so a change snaps back to them
    this.param = Math.fround(o.limit)
  }
  process(inputs, outputs, parameters) {
    const out = outputs[0]
    const L = out[0]
    const R = out.length > 1 ? out[1] : out[0]
    const n = L.length
    const lim = parameters.limit[0]
    if (lim !== this.param) {
      this.param = lim
      this.core.setLimit(Math.round(lim * 1e6) / 1e6, this.autoLevel)
    }
    const inp = inputs[0]
    if (this.zero.length < n) this.zero = new Float32Array(n)
    const iL = inp && inp.length ? inp[0] : this.zero
    const iR = inp && inp.length > 1 ? inp[1] : iL
    this.core.process(iL, iR, L, R, n)
    return true
  }
}
registerProcessor(${JSON.stringify(LIMITER_PROCESSOR)}, VaeALimiter)
`
}

const loads = new WeakMap<BaseAudioContext, Promise<boolean>>()
const ready = new WeakSet<BaseAudioContext>()

/** Register the limiter processor on `ctx` (once per context); resolves
 *  whether it can be used. Never rejects. */
export function loadLimiterWorklet(ctx: BaseAudioContext): Promise<boolean> {
  const known = loads.get(ctx)
  if (known) return known
  const worklet = (ctx as { audioWorklet?: AudioWorklet }).audioWorklet
  let p: Promise<boolean>
  if (!worklet || typeof worklet.addModule !== 'function' || typeof AudioWorkletNode === 'undefined'
      || typeof Blob === 'undefined' || typeof URL.createObjectURL !== 'function') {
    p = Promise.resolve(false)
  } else {
    const url = URL.createObjectURL(new Blob([limiterWorkletSource()], { type: 'text/javascript' }))
    p = worklet.addModule(url).then(() => { ready.add(ctx); return true }, () => false)
      .finally(() => URL.revokeObjectURL(url))
  }
  loads.set(ctx, p)
  return p
}

/** Whether the limiter processor is registered on `ctx` (loadLimiterWorklet
 *  resolved true). */
export function limiterWorkletReady(ctx: BaseAudioContext): boolean {
  return ready.has(ctx)
}

/** alimiter's `limit` for a ceiling in dBFS, as the server writes it (6
 *  decimals: 0 dB → 1, −1 dB → 0.891251). */
export const alimiterLimit = (ceilingDb: number): number => Math.round(Math.pow(10, ceilingDb / 20) * 1e6) / 1e6

/** A stereo limiter node on `ctx` (the processor must be registered). */
export function createLimiterNode(ctx: BaseAudioContext, o: LimiterProcessorOptions): AudioWorkletNode {
  const node = new AudioWorkletNode(ctx, LIMITER_PROCESSOR, {
    numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [2],
    channelCount: 2, channelCountMode: 'explicit', channelInterpretation: 'speakers',
    processorOptions: o, parameterData: { limit: o.limit },
  })
  return node
}
