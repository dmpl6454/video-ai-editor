// A recording test double of the Web Audio node API (vitest runs in node,
// which has none). It does not render sound — the real engines do that in
// tests/wk/test_wk_audio.py — it records what the mix graph ASKED for: every
// node, connection, source start/stop and automation event, so the
// scheduling rules (integer-sample starts, 1 s blocks, per-sample curves,
// dirty-lane reschedules, 5 ms ramps) can be asserted exactly.

export type ParamEvent =
  | { type: 'set'; v: number; t: number }
  | { type: 'ramp'; v: number; t: number }
  | { type: 'curve'; values: Float32Array; t: number; d: number }
  | { type: 'target'; v: number; t: number; tau: number }
  | { type: 'hold'; t: number }
  | { type: 'cancel'; t: number }

export class FakeParam {
  value: number
  events: ParamEvent[] = []
  constructor(v = 1) { this.value = v }
  setValueAtTime(v: number, t: number) { this.events.push({ type: 'set', v, t }); return this }
  linearRampToValueAtTime(v: number, t: number) { this.events.push({ type: 'ramp', v, t }); return this }
  setValueCurveAtTime(values: Float32Array, t: number, d: number) {
    const last = [...this.events].reverse().find((e) => e.type === 'curve') as { t: number; d: number } | undefined
    if (last && t < last.t + last.d - 1e-12 && t >= last.t) throw new Error('NotSupportedError: overlapping curve')
    this.events.push({ type: 'curve', values: values.slice(), t, d })
    return this
  }
  setTargetAtTime(v: number, t: number, tau: number) { this.events.push({ type: 'target', v, t, tau }); return this }
  cancelAndHoldAtTime(t: number) { this.events.push({ type: 'hold', t }); return this }
  cancelScheduledValues(t: number) { this.events.push({ type: 'cancel', t }); return this }
}

let nextId = 1

export class FakeNode {
  readonly id = nextId++
  readonly kind: string
  outputs: Array<{ to: FakeNode; out?: number; in?: number }> = []
  disconnected = false
  readonly ctx: FakeContext
  constructor(ctx: FakeContext, kind: string) {
    this.ctx = ctx
    this.kind = kind
    ctx.nodes.push(this)
  }
  get context(): FakeContext { return this.ctx }
  connect<T extends FakeNode>(to: T, out?: number, inp?: number): T {
    this.outputs.push({ to, out, in: inp })
    return to
  }
  /** Web Audio's semantics: no argument drops every connection, a node
   *  drops only the connections to it (and throws when there is none). */
  disconnect(to?: FakeNode) {
    if (to === undefined) { this.disconnected = true; this.outputs = []; return }
    const kept = this.outputs.filter((o) => o.to !== to)
    if (kept.length === this.outputs.length) throw new Error('InvalidAccessError: not connected')
    this.outputs = kept
  }
}

export class FakeGain extends FakeNode {
  gain = new FakeParam(1)
  constructor(ctx: FakeContext) { super(ctx, 'gain') }
}

export class FakeBuffer {
  readonly data: Float32Array[]
  readonly numberOfChannels: number
  readonly length: number
  readonly sampleRate: number
  constructor(numberOfChannels: number, length: number, sampleRate: number) {
    this.numberOfChannels = numberOfChannels
    this.length = length
    this.sampleRate = sampleRate
    this.data = Array.from({ length: numberOfChannels }, () => new Float32Array(length))
  }
  copyToChannel(src: Float32Array, ch: number) { this.data[ch].set(src.subarray(0, this.length)) }
  getChannelData(ch: number) { return this.data[ch] }
}

export class FakeSource extends FakeNode {
  buffer: FakeBuffer | null = null
  playbackRate = new FakeParam(1)
  startedAt: number | null = null
  offset = 0
  stoppedAt: number | null = null
  onended: (() => void) | null = null
  constructor(ctx: FakeContext) { super(ctx, 'source') }
  start(t = 0, offset = 0) {
    if (this.startedAt !== null) throw new Error('InvalidStateError: start twice')
    this.startedAt = t
    this.offset = offset
  }
  stop(t = 0) {
    if (this.startedAt === null) throw new Error('InvalidStateError: not started')
    this.stoppedAt = t
  }
}

export class FakeCompressor extends FakeNode {
  threshold = new FakeParam(-24)
  knee = new FakeParam(30)
  ratio = new FakeParam(12)
  attack = new FakeParam(0.003)
  release = new FakeParam(0.25)
  constructor(ctx: FakeContext) { super(ctx, 'compressor') }
}

export class FakeConvolver extends FakeNode {
  buffer: FakeBuffer | null = null
  normalize = true
  constructor(ctx: FakeContext) { super(ctx, 'convolver') }
}

export class FakeContext {
  nodes: FakeNode[] = []
  currentTime = 0
  sampleRate = 48000
  state: 'running' | 'suspended' | 'closed' | 'interrupted' = 'suspended'
  baseLatency = 0.005
  outputLatency = 0.015
  destination: FakeNode
  resumes = 0
  suspends = 0
  private listeners: Array<() => void> = []
  readonly offline: boolean
  constructor(offline = false) {
    this.offline = offline
    this.destination = new FakeNode(this, 'destination')
  }
  createGain() { return new FakeGain(this) }
  createBufferSource() { return new FakeSource(this) }
  createChannelSplitter() { return new FakeNode(this, 'splitter') }
  createChannelMerger() { return new FakeNode(this, 'merger') }
  createDynamicsCompressor() { return new FakeCompressor(this) }
  createConvolver() { return new FakeConvolver(this) }
  createBuffer(ch: number, len: number, sr: number) { return new FakeBuffer(ch, len, sr) }
  getOutputTimestamp() { return { contextTime: 0, performanceTime: 0 } }
  resume() { this.resumes++; this.setState('running'); return Promise.resolve() }
  suspend() { this.suspends++; this.setState('suspended'); return Promise.resolve() }
  close() { this.setState('closed'); return Promise.resolve() }
  addEventListener(_e: string, fn: () => void) { this.listeners.push(fn) }
  setState(s: FakeContext['state']) { this.state = s; for (const l of this.listeners) l() }
  get sources(): FakeSource[] { return this.nodes.filter((n): n is FakeSource => n instanceof FakeSource) }
}

/** An OfflineAudioContext-shaped fake (has startRendering). */
export class FakeOfflineContext extends FakeContext {
  constructor() { super(true); this.state = 'running' }
  startRendering() { return Promise.resolve(null) }
}

export const asCtx = (c: FakeContext) => c as unknown as AudioContext
