// Audio acceptance page (tests/wk/pages/audio.html → this bundle), run in
// real WKWebView and in Chromium/WebKit under Playwright. One scenario per
// load (`?scenario=`); the page posts JSON to `/__result/<token>`. Everything
// it measures runs the shipped modules: audio/audioChunks (fetch + WebKit's
// own FLAC decode), audio/audioPlan + audio/mixGraph (the mix, rendered on an
// OfflineAudioContext — the §8.4 verification path) and audio/audioEngine
// (the live sink on a real AudioContext, tapped by an AudioWorklet).
import { AudioChunks, chunkReader } from '../audio/audioChunks'
import { AudioEngine } from '../audio/audioEngine'
import { planFromProgram, type AudioPlan } from '../audio/audioPlan'
import { LIMITER_LATENCY, limiterMakeupUndo, renderOffline } from '../audio/mixGraph'
import type { EdlLike } from '../timeline/framePlan'
import { sourceFromJson, type SourceInfoJson } from '../timeline/frameMap'
import { audioPlacements, buildProgramMap, lookupFromJson } from '../timeline/programMap'
import { samplesForFrames } from '../timeline/timebase'

const q = new URLSearchParams(location.search)
const token = q.get('token') ?? 'none'
const scenario = q.get('scenario') ?? ''
const SR = 48000
const MEDIA = '/media'

type Result = Record<string, unknown>

interface CaseJson {
  name: string
  edl: EdlLike
  /** EDL src → proxy key + frame_map SourceInfo json. */
  sources: Record<string, { key: string; info: SourceInfoJson }>
  range: [number, number]
  loudness_gain_db?: number | null
}

async function json<T>(url: string): Promise<T> {
  const r = await fetch(url)
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`)
  return (await r.json()) as T
}

async function f32(url: string): Promise<Float32Array> {
  const r = await fetch(url)
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`)
  return new Float32Array(await r.arrayBuffer())
}

function b64(a: Float32Array): string {
  const bytes = new Uint8Array(a.buffer, a.byteOffset, a.byteLength)
  let s = ''
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000))
  return btoa(s)
}

function loadCase(c: CaseJson) {
  const table: Record<string, SourceInfoJson> = {}
  for (const [src, s] of Object.entries(c.sources)) table[src] = s.info
  const lookup = lookupFromJson(table)
  const chunks = new AudioChunks({ base: `${MEDIA}/proxies` })
  const reader = chunkReader(chunks, (src) => c.sources[src]?.key ?? null)
  return { lookup, chunks, reader }
}

async function planOf(c: CaseJson): Promise<{ plan: AudioPlan; reader: ReturnType<typeof chunkReader>; chunks: AudioChunks }> {
  const { lookup, chunks, reader } = loadCase(c)
  for (const s of Object.values(c.sources)) await chunks.layout(s.key)
  const pm = buildProgramMap(c.edl, lookup)
  const plan = planFromProgram(c.edl, pm, lookup, {
    loudnessGainDb: c.loudness_gain_db ?? null, silent: (s) => reader.silent(s), peak: (s, a, b) => reader.peak?.(s, a, b) ?? null,
  })
  return { plan, reader, chunks }
}

const planSummary = (p: AudioPlan) => ({
  total: p.total, master: p.master, approx: p.approx, limiting: p.limiting, duck: p.duck,
  buses: p.buses,
  clips: p.clips.map((c) => ({ id: c.id, bus: c.bus, out0: c.out0, n: c.n, exact: c.exact, xIn: c.xIn, xOut: c.xOut, map: c.map })),
})

const scenarios: Record<string, () => Promise<Result>> = {
  /** Engine facts the mix relies on. */
  async probe() {
    // The limiter's look-ahead: an impulse through the compressor as the
    // master configures it, for both ceilings the plan uses.
    const lat: Record<string, number> = {}
    const gainDb: Record<string, number> = {}
    for (const ceiling of [0, -1]) {
      const ctx = new OfflineAudioContext(1, SR, SR)
      const b = ctx.createBuffer(1, SR, SR)
      const d = b.getChannelData(0)
      d[1000] = 0.25
      for (let i = 10000; i < 40000; i++) d[i] = 0.2 * Math.sin(2 * Math.PI * 441 * i / SR)
      const s = ctx.createBufferSource()
      s.buffer = b
      const lim = ctx.createDynamicsCompressor()
      lim.threshold.value = ceiling
      lim.knee.value = 0
      lim.ratio.value = 20
      lim.attack.value = 0.001
      lim.release.value = 0.05
      const g = ctx.createGain()
      g.gain.value = limiterMakeupUndo(ceiling)
      s.connect(lim).connect(g).connect(ctx.destination)
      s.start(0)
      const out = (await ctx.startRendering()).getChannelData(0)
      let pk = 0, at = -1
      for (let i = 0; i < 9000; i++) if (Math.abs(out[i]) > pk) { pk = Math.abs(out[i]); at = i }
      lat[String(ceiling)] = at - 1000
      let num = 0, den = 0
      for (let i = 20000; i < 38000; i++) { num += out[i + (at - 1000)] ** 2; den += d[i] ** 2 }
      gainDb[String(ceiling)] = 10 * Math.log10(num / den)
    }
    const p = OfflineAudioContext.prototype as unknown as Record<string, unknown>
    const param = (new OfflineAudioContext(1, 1, SR)).createGain().gain as unknown as Record<string, unknown>
    const rate48 = await (async () => {
      try {
        const c = new AudioContext({ sampleRate: SR })
        const ok = c.sampleRate === SR
        await c.close()
        return ok
      } catch {
        return false
      }
    })()
    return {
      limiterLatency: lat, expectedLatency: LIMITER_LATENCY, limiterSineGainDb: gainDb,
      cancelAndHold: typeof param.cancelAndHoldAtTime === 'function',
      valueCurve: typeof param.setValueCurveAtTime === 'function',
      offline: typeof p.startRendering === 'function', audioContext48k: rate48,
    }
  },

  /** WebKit's decode of the proxy FLAC chunks, through AudioChunks, against
   *  ffmpeg's decode of the same chunks and of the master. */
  async flac() {
    const keys = (q.get('keys') ?? '').split(',').filter(Boolean)
    const out: Result[] = []
    const chunks = new AudioChunks({ base: `${MEDIA}/proxies` })
    for (const key of keys) {
      const l = await chunks.layout(key)
      const n = l.samples ?? 0
      const flacRef = await f32(`${MEDIA}/ref/${key}.flac.f32`)
      const masterRef = await f32(`${MEDIA}/ref/${key}.master.f32`)
      let maxFlac = 0, maxMaster = 0, firstFlac = -1, checked = 0, maxGain = 1
      let maxAbs = 0
      const t0 = performance.now()
      for (let c = 0; c * l.chunkSamples < n; c++) {
        const pcm = await chunks.chunk(key, c)
        const g = l.chunkGain[String(c)] ?? 1
        maxGain = Math.max(maxGain, g)
        for (let i = 0; i < pcm.L.length; i++) {
          const s = c * l.chunkSamples + i
          for (const [ch, v] of [[0, pcm.L[i]], [1, pcm.R[i]]] as const) {
            const df = Math.abs(v - flacRef[2 * s + ch])
            const dm = Math.abs(v - masterRef[2 * s + ch])
            if (df > maxFlac) { maxFlac = df; if (firstFlac < 0 && df > 0) firstFlac = s }
            if (dm > maxMaster) maxMaster = dm
            maxAbs = Math.max(maxAbs, Math.abs(v))
          }
          checked++
        }
      }
      out.push({
        key, samples: n, checked, refSamples: flacRef.length / 2, masterSamples: masterRef.length / 2,
        maxDiffVsFfmpegFlac: maxFlac, firstDiff: firstFlac, maxDiffVsMaster: maxMaster, maxGain, maxAbs,
        decodeMs: +(performance.now() - t0).toFixed(1), chunkStats: chunks.stats,
      })
    }
    return { keys: out }
  },

  /** The offline mix of a case (P1-A1 / P1-A2): the rendered PCM. */
  async render() {
    const name = q.get('case') ?? ''
    const c = await json<CaseJson>(`${MEDIA}/cases/${name}.json`)
    const { plan, reader } = await planOf(c)
    const [p0, p1] = c.range
    const t0 = performance.now()
    const { L, R, stats } = await renderOffline(plan, reader, p0, p1)
    return { name, ms: +(performance.now() - t0).toFixed(1), plan: planSummary(plan), stats, L: b64(L), R: b64(R) }
  },

  /** The same offline render N times: WebKit's OfflineAudioContext must be
   *  deterministic (a hole of silent render quanta once showed up under load). */
  async render_repeat() {
    const name = q.get('case') ?? ''
    const n = Number(q.get('n') ?? 10)
    const c = await json<CaseJson>(`${MEDIA}/cases/${name}.json`)
    const { plan, reader } = await planOf(c)
    const [p0, p1] = c.range
    const first = await renderOffline(plan, reader, p0, p1)
    const diffs: Array<{ i: number; at: number; count: number; trace?: Result }> = []
    // One clip's own contribution to the mix (rendered alone): what a render
    // that lost that clip is missing — the trace names the silenced source.
    const alone = new Map<string, Float32Array>()
    const contribution = async (id: string): Promise<Float32Array> => {
      let x = alone.get(id)
      if (!x) {
        x = (await renderOffline({ ...plan, clips: plan.clips.filter((c) => c.id === id) }, reader, p0, p1)).L
        alone.set(id, x)
      }
      return x
    }
    for (let i = 1; i < n; i++) {
      const r = await renderOffline(plan, reader, p0, p1)
      let at = -1, last = -1, count = 0
      const where: Array<[number, number, number, number, number]> = []
      for (let j = 0; j < r.L.length; j++) {
        if (r.L[j] !== first.L[j] || r.R[j] !== first.R[j]) {
          if (at < 0) at = j
          last = j
          count++
          if (where.length < 24) where.push([j, first.L[j], r.L[j], first.R[j], r.R[j]])
        }
      }
      if (!count) continue
      const scores: Record<string, number> = {}
      for (const c of plan.clips) {
        if (c.out0 + c.n <= p0 + at || c.out0 >= p0 + last + 1) continue
        const x = await contribution(c.id)
        let dd = 0, res = 0
        for (let j = at; j <= last; j++) {
          const d = r.L[j] - first.L[j]
          dd += d * d
          res += (d + x[j]) ** 2
        }
        scores[c.id] = dd > 0 ? Math.sqrt(res / dd) : 1
      }
      const best = Object.entries(scores).sort((a, b) => a[1] - b[1])[0]
      diffs.push({ i, at, count, trace: {
        last, quantum: Math.floor(at / 128), statsFirst: first.stats, statsBad: r.stats,
        where, plan: plan.clips.map((c) => [c.id, c.out0, c.n, c.map.kind]),
        badSilentFrom: (() => { let k = at; while (k <= last && r.L[k] === 0 && r.R[k] === 0) k++; return k - at })(),
        silenced: best ? { clip: best[0], residual: best[1] } : null, scores,
      } })
    }
    return { name, n, diffs }
  },

  /** The LIVE sink on a real AudioContext, tapped at its output: start at an
   *  anchor, stop (5 ms ramp), restart from a fresh anchor, and a gain edit
   *  while playing. */
  async live() {
    const name = q.get('case') ?? ''
    const c = await json<CaseJson>(`${MEDIA}/cases/${name}.json`)
    const { lookup } = loadCase(c)
    const pm = buildProgramMap(c.edl, lookup)
    const info = {
      R: pm.R, totalFrames: pm.total, renderHash: 'test',
      lookup: (src: string) => (c.sources[src] ? { info: sourceFromJson(c.sources[src].info), proxy: { key: c.sources[src].key } } : null),
    }
    const ctx = new AudioContext({ sampleRate: SR, latencyHint: 'interactive' })
    await ctx.audioWorklet.addModule('/pages/audio_tap.js')
    const tap = new AudioWorkletNode(ctx, 'tap', { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [2] })
    const blocks: Array<{ frame: number; L: Float32Array }> = []
    tap.port.onmessage = (e: MessageEvent) => { blocks.push(e.data as { frame: number; L: Float32Array }) }
    tap.connect(ctx.destination)
    const interrupted: string[] = []
    const engine = new AudioEngine({
      proxyBase: `${MEDIA}/proxies`, createContext: () => ctx, destination: () => tap,
      onInterrupted: (st) => interrupted.push(st),
    })
    engine.prepare(c.edl, audioPlacements(pm, lookup), info)
    // Chunks for the first 4 s before the clock starts (the engine would
    // otherwise start on the blocks it has and fill the rest as they land).
    await Promise.all(Object.keys(c.sources).map((src) => engine.reader.load(src, 0, 6 * SR)))
    const events: Result[] = []
    const k0 = 15
    const s0 = samplesForFrames(k0, pm.R)
    const at1 = ctx.currentTime + 0.1
    engine.start(at1, s0)
    events.push({ ev: 'start', at: at1, sample: s0, ctxState: ctx.state })
    await new Promise((r) => setTimeout(r, 1500))           // across a refill tick
    const tStop = ctx.currentTime
    engine.stop(5)
    events.push({ ev: 'stop', at: tStop })
    await new Promise((r) => setTimeout(r, 300))
    const suspendedAfterStop = ctx.state
    await ctx.resume()
    const s1 = samplesForFrames(60, pm.R)
    const at2 = ctx.currentTime + 0.1
    engine.start(at2, s1)
    events.push({ ev: 'restart', at: at2, sample: s1 })
    await new Promise((r) => setTimeout(r, 400))
    // A structural edit while playing (case2: a clip rippled out): the new
    // program from ≈ 6 frames past the playhead.
    const c2 = await json<CaseJson>(`${MEDIA}/cases/${q.get('case2') ?? name}.json`)
    const pmCut = buildProgramMap(c2.edl, lookup)
    const pe = Math.ceil(s1 + (ctx.currentTime - at2) * SR) + samplesForFrames(6, pm.R)
    engine.prepare(c2.edl, audioPlacements(pmCut, lookup), { ...info, totalFrames: pmCut.total })
    engine.reschedule({ dirtyFrames: [], dirtyParams: new Set() }, pe)
    events.push({ ev: 'cut', sample: pe })
    await new Promise((r) => setTimeout(r, 600))
    // Re-anchor while running (the engine's first-rVFC correction, §3.5):
    // the same program on a new sample ↔ context-time mapping.
    const s3 = samplesForFrames(45, pm.R)
    const call = ctx.currentTime
    const at3 = call + 0.05
    engine.start(at3, s3)
    events.push({ ev: 'reanchor', at: at3, sample: s3, call })
    await new Promise((r) => setTimeout(r, 500))
    // A pause the engine did not issue: the context suspended under it (as
    // the system does). The sink must stop and say so; the engine then
    // resumes BOTH from a fresh anchor.
    const tExt = ctx.currentTime
    await ctx.suspend()
    await new Promise((r) => setTimeout(r, 100))
    events.push({ ev: 'external', at: tExt, running: engine.isRunning, interrupted: [...interrupted] })
    await ctx.resume()
    const s5 = samplesForFrames(75, pm.R)
    const at5 = ctx.currentTime + 0.08
    engine.start(at5, s5)
    events.push({ ev: 'resume', at: at5, sample: s5 })
    await new Promise((r) => setTimeout(r, 500))
    // A gain edit while playing: clip gain −60 dB on every v1 clip.
    const edl2 = JSON.parse(JSON.stringify(c2.edl)) as EdlLike
    for (const t of edl2.tracks ?? []) for (const cl of t.clips as Array<{ audio?: Record<string, unknown> }>) cl.audio = { ...(cl.audio ?? {}), gain_db: -60 }
    const pm2 = buildProgramMap(edl2, lookup)
    const tEdit = ctx.currentTime
    engine.prepare(edl2, audioPlacements(pm2, lookup), info)
    engine.setParams('any')
    events.push({ ev: 'edit', at: tEdit })
    await new Promise((r) => setTimeout(r, 400))
    engine.stop(5)
    await new Promise((r) => setTimeout(r, 100))
    tap.port.postMessage('flush')
    await new Promise((r) => setTimeout(r, 50))
    // Stitch the tap's blocks on the context frame clock.
    const first = blocks.length ? blocks[0].frame : 0
    const last = blocks.length ? blocks[blocks.length - 1].frame + blocks[blocks.length - 1].L.length : 0
    const L = new Float32Array(Math.max(0, last - first))
    for (const b of blocks) L.set(b.L, b.frame - first)
    const res = {
      name, events, firstFrame: first, sampleRate: ctx.sampleRate, suspendedAfterStop,
      stats: engine.stats, L: b64(L),
      outputLatency: (ctx as { outputLatency?: number }).outputLatency ?? null, baseLatency: ctx.baseLatency,
    }
    engine.dispose()
    return res
  },
}

async function post(body: Result): Promise<void> {
  await fetch(`/__result/${token}`, { method: 'POST', body: JSON.stringify(body) })
}

;(async () => {
  const result: Result = { scenario, ua: navigator.userAgent }
  try {
    const run = scenarios[scenario]
    if (!run) throw new Error(`unknown scenario ${JSON.stringify(scenario)}`)
    Object.assign(result, await run())
  } catch (e) {
    result.fatal = String((e as Error)?.stack ?? e)
  }
  ;(window as unknown as { __result: Result }).__result = result
  await post(result)
})()
