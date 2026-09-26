// P1-B1 bake splice (instant preview spec §13, §5.3, §7) as a PAGE for the WK
// harness (tests/wk/test_wk_bake_splice.py), served by a REAL backend.
//
// A graded clip is BAKED in Phase 1. The page plays the program the way the
// engine does: RAW client frames (the source proxy, per the server's
// /frame_map) everywhere first; then "the render lands" and the bake spans
// of the BAKED range are appended OVER those frames in the same
// SourceBuffer — a different init class (the bake's own avcC), so the
// writer re-appends an init. Every k is read back through paused
// (k+0.5)/R seeks: the bar (which source frame) and a picture patch's level
// (raw vs graded), before and after the splice, and the <video> is watched
// for any src swap.

import { parseInitSegment, type FrameEntry, type TrackFormat } from '../media/fmp4Writer'
import { parseSpanPack } from '../media/spanPack'
import { rleFrames, type Run } from '../timeline/programMap'
import { BarReader, barCode } from './barcode'
import { Lane, midFrame, seekTo, sleep } from './mseKit'
import { loadProxy } from './proxySource'

export interface BakeSpliceOptions {
  sid: string
  h: string
  /** The source's path as the EDL stores it (the proxy route's `src`). */
  src: string
  srcId: number
  /** The BAKED output range [k0, k1). */
  baked: [number, number]
}

/** Mean green level of a patch below the bar (rows 200..299, cols 100..399). */
class PatchReader {
  private readonly gl: WebGL2RenderingContext
  private readonly tex: WebGLTexture
  private readonly fb: WebGLFramebuffer
  private readonly px = new Uint8Array(300 * 100 * 4)

  constructor() {
    const gl = document.createElement('canvas').getContext('webgl2')
    if (!gl) throw new Error('no WebGL2')
    this.gl = gl
    this.tex = gl.createTexture()!
    gl.bindTexture(gl.TEXTURE_2D, this.tex)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST)
    this.fb = gl.createFramebuffer()!
  }

  read(video: HTMLVideoElement): number {
    const gl = this.gl
    gl.bindTexture(gl.TEXTURE_2D, this.tex)
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, video)
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.fb)
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, this.tex, 0)
    gl.readPixels(100, 200, 300, 100, gl.RGBA, gl.UNSIGNED_BYTE, this.px)
    let sum = 0
    for (let i = 1; i < this.px.length; i += 4) sum += this.px[i]
    return sum / (this.px.length / 4)
  }
}

async function fetchReady(url: string, tries = 100): Promise<Response> {
  for (let i = 0; i < tries; i++) {
    const r = await fetch(url)
    if (r.status === 202) { await sleep(200); continue }
    if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`)
    return r
  }
  throw new Error(`${url}: still pending`)
}

interface Read { k: number; code: number; level: number }

export async function runBakeSplice(o: BakeSpliceOptions) {
  const t0 = performance.now()
  const base = `/api/sessions/${o.sid}`
  const fm = await (await fetchReady(`${base}/frame_map?h=${o.h}`)).json() as { R: [number, number]; total: number; runs: Run[] }
  const R = { num: fm.R[0], den: fm.R[1] }
  const frames = rleFrames(fm.runs)
  const proxyRow = await (await fetchReady(`${base}/proxy?src=${encodeURIComponent(o.src)}`)).json() as { key: string }
  const proxy = await loadProxy('/api/proxies', proxyRow.key, o.srcId)

  // The engine's first picture: RAW client frames for every k.
  const lane = await Lane.open(R, proxy.format.codec, { w: 480, h: 270 })
  const video = lane.video
  const srcAtStart = video.src
  let emptied = 0
  let loadstarts = 0
  video.addEventListener('emptied', () => { emptied++ })
  video.addEventListener('loadstart', () => { loadstarts++ })
  const plan = frames.map((f) => (f.kind === 1 ? null : [proxy, f.frame] as const))
  await lane.write(0, plan)
  lane.setDuration(plan.length)
  const bar = new BarReader()
  const patch = new PatchReader()
  const readAll = async (): Promise<Read[]> => {
    const out: Read[] = []
    for (let k = 0; k < plan.length; k++) {
      await seekTo(video, midFrame(k, R))
      out.push({ k, code: bar.read(video), level: +patch.read(video).toFixed(2) })
    }
    return out
  }
  const before = await readAll()

  // The render lands: the bake of this hash, for the BAKED range only.
  const [k0, k1] = o.baked
  const idx = await (await fetchReady(`${base}/bake/${o.h}/index.json?ranges=${k0}-${k1}`)).json() as {
    queued: number[]; span_frames: number; frames: number; init_key?: string; w: number; h: number
  }
  const bakeFormat: TrackFormat = parseInitSegment(new Uint8Array(await (await fetchReady(`${base}/bake/${o.h}/init.mp4`)).arrayBuffer()))
  const bakeFrames = new Map<number, Uint8Array>()
  for (const n of idx.queued) {
    const pack = parseSpanPack(new Uint8Array(await (await fetchReady(`${base}/bake/${o.h}/v/${n}.bin`)).arrayBuffer()))
    pack.samples.forEach((s, i) => bakeFrames.set(pack.first + i, s))
  }
  const entries: FrameEntry[] = []
  for (let k = k0; k < k1; k++) {
    const bytes = bakeFrames.get(k)
    if (!bytes) throw new Error(`bake frame ${k} missing (spans ${idx.queued})`)
    entries.push({ format: bakeFormat, bytes })
  }
  const initsBefore = lane.inits
  await lane.appendSegments(lane.writer.write(k0, entries))
  const after = await readAll()
  const result = {
    ua: navigator.userAgent,
    R, total: fm.total, k0, k1,
    expected: frames.map((f) => (f.kind === 1 ? null : barCode(o.srcId, f.frame))),
    before, after,
    proxyInitKey: proxy.format.initKey, bakeInitKey: bakeFormat.initKey,
    bakeIndexInitKey: idx.init_key ?? null, bakeSize: [idx.w, idx.h], bakeFrames: idx.frames,
    spansFetched: idx.queued, initsAppendedForSplice: lane.inits - initsBefore,
    srcUnchanged: video.src === srcAtStart, emptied, loadstarts,
    elapsedMs: performance.now() - t0,
  }
  lane.dispose()
  return result
}
