// Loads a §5.1 proxy directory (index.json, init.mp4, v/NNNN.bin) whole, for
// the WK acceptance pages. The product path (on-demand spans, 202 retry, the
// span LRU) is media/proxyIndex.ts; this is the eager test-side loader.
import { parseInitSegment, type FrameSample, type TrackFormat } from '../media/fmp4Writer'
import { parseSpanPack } from '../media/spanPack'

export interface ProxyIndexJson {
  readonly src_rate: { num: number; den: number }
  readonly frames: number
  readonly w: number
  readonly h: number
  readonly span_frames: number
  /** Span states (fixtures) or the span count (ingest/proxy.py). */
  readonly spans: readonly string[] | number
  readonly src_id?: number
  /** ingest/proxy.py's init class key (hex of the avcC); fixtures omit it. */
  readonly init_key?: string
}

export interface ProxySource {
  readonly name: string
  readonly srcId: number
  readonly index: ProxyIndexJson
  readonly format: TrackFormat
  sample(frame: number): FrameSample
}

async function fetchOk(url: string): Promise<Response> {
  const r = await fetch(url)
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`)
  return r
}

/** `srcId` overrides the index's bar source id (real proxies carry none). */
export async function loadProxy(base: string, name: string, srcId?: number): Promise<ProxySource> {
  const dir = `${base.replace(/\/$/, '')}/${name}`
  const index = (await (await fetchOk(`${dir}/index.json`)).json()) as ProxyIndexJson
  const init = new Uint8Array(await (await fetchOk(`${dir}/init.mp4`)).arrayBuffer())
  const format = parseInitSegment(init)
  // One init-class identity on both sides (review RD1): the writer's key for
  // this init must be the key the server published for it.
  if (index.init_key !== undefined && index.init_key !== format.initKey) {
    throw new Error(`${name}: init_key ${index.init_key} != writer initKey ${format.initKey}`)
  }
  const spanCount = typeof index.spans === 'number' ? index.spans : index.spans.length
  const packs = await Promise.all(
    Array.from({ length: spanCount }, async (_s, n) => parseSpanPack(new Uint8Array(await (await fetchOk(`${dir}/v/${String(n).padStart(4, '0')}.bin`)).arrayBuffer()))),
  )
  const frames: Uint8Array[] = new Array(index.frames)
  for (const p of packs) p.samples.forEach((s, i) => { frames[p.first + i] = s })
  for (let i = 0; i < index.frames; i++) if (!frames[i]) throw new Error(`${name}: frame ${i} missing from the span packs`)
  return {
    name,
    srcId: srcId ?? index.src_id ?? 0,
    index,
    format,
    sample(frame: number): FrameSample {
      const bytes = frames[frame]
      if (!bytes) throw new Error(`${name}: no frame ${frame}`)
      return { format, bytes }
    },
  }
}
