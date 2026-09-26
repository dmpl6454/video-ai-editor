// Shared probe for the ENGINE's WK pages (wkEnginePage.ts, wkEngineFaultPage.ts):
// builds the real PreviewEngine from a fixture EDL, records every AudioSink
// call, and reads the bar burned into each source frame back off the engine
// canvas (inside the compositor's onDrawn hook: the drawing buffer is only
// readable in the task that drew it).
import {
  createPreviewEngine, type AudioSink, type ClientPreviewEngine, type EngineSourceLookup, type EngineStatus,
} from '../engine'
import type { EdlLike } from '../timeline/framePlan'
import { sourceFromJson, type SourceInfoJson } from '../timeline/frameMap'
import { KIND_GAP, type ProgramMap } from '../timeline/programMap'
import { canvasPointOf, frameGeometry } from '../render/geometry'
import { BAR_BITS, BAR_CELL, NO_PICTURE, barCode, barFrame, barSrc } from './barcode'
import { now } from './mseKit'

const q = new URLSearchParams(location.search)

export interface FixtureSource { info: SourceInfoJson; key: string; srcId?: number }
export interface Fixture {
  edl: EdlLike
  sources: Record<string, FixtureSource>
  canvas: [number, number]
  /** geometry parity: groups of (edl, ks) */
  groups?: Array<{ name: string; edl: EdlLike; ks: number[] }>
  /** the span the Python side delays (P1-F5): source frames [first, last] */
  delayed?: { src: string; first: number; last: number }
}
export type Result = Record<string, unknown>

export function lookupOf(fx: Fixture): EngineSourceLookup {
  const cache = new Map<string, ReturnType<EngineSourceLookup>>()
  return (src) => {
    if (!cache.has(src)) {
      const s = fx.sources[src]
      cache.set(src, s ? { info: sourceFromJson(s.info), proxy: { key: s.key, state: 'ready' } } : null)
    }
    return cache.get(src)!
  }
}

/** Records every call the engine makes to its AudioSink, with the frame on
 *  screen at that moment; ctx time = performance.now()/1000 (one clock). */
export class RecordingSink implements AudioSink {
  readonly calls: Array<{ op: string; at: number; k: number; playing: boolean; ctx?: number; sample?: number }> = []
  engine: ClientPreviewEngine | null = null
  private log(op: string, extra: Record<string, number> = {}): void {
    this.calls.push({ op, at: +now().toFixed(2), k: this.engine?.presentedK ?? -1, playing: this.engine?.playing ?? false, ...extra })
  }
  prepare(): void { this.log('prepare') }
  start(atCtxTime: number, fromSample: number): void { this.log('start', { ctx: atCtxTime, sample: fromSample }) }
  stop(rampMs: number): void { this.log('stop', { ramp: rampMs }) }
  reschedule(): void { this.log('reschedule') }
  setParams(): void { this.log('params') }
  ctxTimeAt(perfMs: number): number | null { return perfMs / 1000 }
}

export interface Probe {
  engine: ClientPreviewEngine
  sink: RecordingSink
  fx: Fixture
  /** k → the bar read on the last draw of k */
  bars: Map<number, number>
  draws: Array<{ k: number; bar: number; playing: boolean; at: number }>
  srcIds: Map<string, number>
  lastStatus: EngineStatus | null
  statuses: Array<{ at: number; spinner: boolean; buffering: boolean; playing: boolean }>
  /** The fixture's source lookup (the same object across setTimeline calls). */
  lookup: EngineSourceLookup
}

export function expectedCode(pm: ProgramMap, k: number, srcIds: Map<string, number>): number {
  if (pm.kind[k] === KIND_GAP) return NO_PICTURE
  return barCode(srcIds.get(pm.sources[pm.srcKey[k]]) ?? 0, pm.srcFrame[k])
}

/** Bar code read from the engine canvas for frame k (inside onDrawn). */
export function readBar(p: Probe, gl: WebGL2RenderingContext, k: number, black: boolean): number {
  const pm = p.engine.program!
  const canvas = gl.canvas as HTMLCanvasElement
  const [cw, ch] = p.fx.canvas
  if (black || pm.kind[k] === KIND_GAP) {
    const px = new Uint8Array(4)
    gl.readPixels(Math.floor(canvas.width / 2), Math.floor(canvas.height / 2), 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, px)
    return px[1] > 128 ? -2 : NO_PICTURE
  }
  const fg = frameGeometry(pm, k, { w: cw, h: ch }, (src) => sourceFromJson(p.fx.sources[src].info))
  if (!fg) return -3
  const info = p.fx.sources[fg.clip.src].info
  const w = info.w ?? 1920
  const h = info.h ?? 1080
  const sx = canvas.width / cw
  const sy = canvas.height / ch
  const pts: Array<[number, number]> = []
  for (let bit = 0; bit < BAR_BITS; bit++) {
    const at = canvasPointOf(fg.geometry, (bit * BAR_CELL + BAR_CELL / 2) / w, 20 / h)
    if (!at) return -4
    pts.push([at[0] * sx, at[1] * sy])
  }
  const x0 = Math.max(0, Math.floor(Math.min(...pts.map((p2) => p2[0]))))
  const x1 = Math.min(canvas.width, Math.ceil(Math.max(...pts.map((p2) => p2[0]))) + 1)
  const ys = pts.map((p2) => Math.floor(p2[1]))
  const y = ys[0]
  let code = 0
  if (ys.every((v) => v === y)) {
    const row = new Uint8Array((x1 - x0) * 4)
    gl.readPixels(x0, canvas.height - 1 - y, x1 - x0, 1, gl.RGBA, gl.UNSIGNED_BYTE, row)
    pts.forEach(([x], bit) => { if (row[(Math.floor(x) - x0) * 4 + 1] > 128) code |= 1 << bit })
  } else {
    const px = new Uint8Array(4)
    pts.forEach(([x, yy], bit) => {
      gl.readPixels(Math.floor(x), canvas.height - 1 - Math.floor(yy), 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, px)
      if (px[1] > 128) code |= 1 << bit
    })
  }
  return barSrc(code) === 0 ? NO_PICTURE : code
}

export async function setup(fx: Fixture, opts: { mipmaps?: boolean } = {}): Promise<Probe> {
  // ?canvas=WxH draws at another backing size (the §11.2 1080p budget);
  // ?perf=1 skips the bar reads (their readPixels would sit in the handler)
  const cs = (q.get('canvas') ?? '').split('x').map(Number)
  if (cs.length === 2 && cs[0] > 0) fx = { ...fx, canvas: [cs[0], cs[1]] }
  const perf = q.get('perf') === '1'
  const host = document.createElement('div')
  host.style.cssText = `position:relative;width:${fx.canvas[0]}px;height:${fx.canvas[1]}px;`
  document.body.appendChild(host)
  const sink = new RecordingSink()
  const engine = createPreviewEngine({ audioSink: sink, canvasSize: { w: fx.canvas[0], h: fx.canvas[1] }, mipmaps: opts.mipmaps })
  sink.engine = engine
  const srcIds = new Map<string, number>()
  if (!perf) for (const [src, s] of Object.entries(fx.sources)) if (s.srcId) srcIds.set(src, s.srcId)
  const p: Probe = { engine, sink, fx, bars: new Map(), draws: [], srcIds, lastStatus: null, statuses: [], lookup: lookupOf(fx) }
  engine.on('status', (st) => { p.lastStatus = st; p.statuses.push({ at: now(), spinner: st.spinner, buffering: st.buffering, playing: st.playing }) })
  engine.attach(host)
  const comp = engine.internals.compositor
  if (!comp) throw new Error(`engine did not start: ${JSON.stringify(engine.status)}`)
  comp.onDrawn = (info, gl) => {
    if (!srcIds.size) return
    const bar = readBar(p, gl, info.k, info.black)
    p.bars.set(info.k, bar)
    p.draws.push({ k: info.k, bar, playing: engine.playing, at: now() })
  }
  engine.setTimeline(fx.edl, 'fixture', p.lookup)
  return p
}

/** Resolves when frame k is drawn while paused (or after `ms`). */
export function drawnPaused(engine: ClientPreviewEngine, k: number, ms = 5000): Promise<boolean> {
  return new Promise((resolve) => {
    if (engine.targetK === k && engine.internals.settled()) {
      resolve(true)
      return
    }
    const off = engine.on('frame', (f) => {
      if (f.k === k && !f.playing) { off(); clearTimeout(t); resolve(true) }
    })
    const t = setTimeout(() => { off(); resolve(false) }, ms)
  })
}

export function cutsOf(pm: ProgramMap): number[] {
  const out: number[] = []
  for (let k = 1; k < pm.total; k++) {
    if (pm.kind[k] !== pm.kind[k - 1] || pm.clip[k] !== pm.clip[k - 1]) out.push(k)
  }
  return out
}

export const fmt = (c: number) => (c < 0 ? String(c) : `${barSrc(c)}:${barFrame(c)}`)
