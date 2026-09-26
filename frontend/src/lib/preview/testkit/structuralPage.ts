// P1-S1 structural agreement (instant preview spec §13) as a PAGE for the WK
// harness (tests/wk/test_wk_structural_agreement.py). The page is served by
// a REAL backend (the FastAPI app, fixture sources, a scratch workdir) and
// drives it exactly as the engine will: every edit is a real
// `POST /dispatch?include=edl`; the client program map is built from the
// EDL that answer carried; `DivergenceChecker` fetches the server's
// `/frame_map` of that render hash and compares. Run in JavaScriptCore, the
// engine the app ships in.

import type { EdlLike } from '../timeline/framePlan'
import type { SourceInfoJson } from '../timeline/frameMap'
import { buildProgramMap, lookupFromJson } from '../timeline/programMap'
import { DivergenceChecker, type DivergenceEvent } from '../verify/divergence'

interface Clip { id: string; src: string; start: number; in: number; out: number; speed?: unknown; reverse?: boolean }
interface Edl { canvas: { fps: number }; tracks: Array<{ id: string; clips: Clip[] }> }

export interface StructuralOptions {
  sid: string
  edits: number
  seed: number
  /** SourceInfo of every fixture source, keyed by the src the EDL stores. */
  sources: Record<string, SourceInfoJson>
}

export interface StructuralResult {
  ua: string
  attempted: number
  applied: number
  rejected: Record<string, number>
  byTool: Record<string, number>
  outcomes: Record<string, number>
  mismatches: DivergenceEvent[]
  others: DivergenceEvent[]
  clipsMax: number
  framesMax: number
  checkMs: { p50: number; p95: number; max: number }
  /** Negative control: the last map with one frame altered must be caught. */
  control: { outcome: string; demote: Array<readonly [number, number]> } | null
  elapsedMs: number
}

function prng(seed: number): () => number {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

const round3 = (x: number) => Math.round(x * 1000) / 1000

function v1Clips(edl: Edl): Clip[] {
  return [...(edl.tracks.find((t) => t.id === 'v1')?.clips ?? [])].filter((c) => c && c.src)
    .sort((a, b) => a.start - b.start)
}

const effective = (c: Clip) => (c.out - c.in) / (typeof c.speed === 'number' && c.speed > 0 ? c.speed : 1)

/** One random structural edit against the current timeline. */
function pickEdit(edl: Edl, rnd: () => number, srcs: string[], frames: Record<string, number>,
  fps: number): { tool: string; args: Record<string, unknown> } {
  const clips = v1Clips(edl)
  const pick = <T>(xs: T[]) => xs[Math.floor(rnd() * xs.length)]
  const r = rnd()
  if (!clips.length || r < 0.12) {
    const src = pick(srcs)
    const dur = frames[src] / fps
    const a = round3(rnd() * dur * 0.6)
    const end = clips.length ? Math.max(...clips.map((c) => c.start + effective(c))) : 0
    return { tool: 'add_clip', args: { src, track: 'v1', in: a, out: round3(Math.min(dur, a + 0.4 + rnd() * 2)),
      start: round3(end + (rnd() < 0.3 ? rnd() * 0.7 : 0)) } }
  }
  const c = pick(clips)
  if (r < 0.26) {
    return { tool: 'split_at', args: { track: 'v1', time: round3(c.start + effective(c) * (0.2 + 0.6 * rnd())) } }
  }
  if (r < 0.40) {
    const edge = rnd() < 0.5
    const span = c.out - c.in
    return edge
      ? { tool: 'trim_clip', args: { clip_id: c.id, in: round3(Math.max(0, c.in + (rnd() - 0.4) * span * 0.4)) } }
      : { tool: 'trim_clip', args: { clip_id: c.id, out: round3(c.in + span * (0.5 + rnd() * 0.6)) } }
  }
  if (r < 0.52) {
    return { tool: 'move_clip', args: { clip_id: c.id, new_start: round3(Math.max(0, c.start + (rnd() - 0.5) * 3)),
      close_gap: rnd() < 0.3 } }
  }
  if (r < 0.60) return { tool: 'ripple_delete', args: { clip_id: c.id } }
  if (r < 0.66) return { tool: 'bulk_delete', args: { clip_ids: [c.id] } }
  if (r < 0.71) return { tool: 'duplicate_clip', args: { clip_id: c.id } }
  if (r < 0.79) return { tool: 'set_speed', args: { clip_id: c.id, factor: pick([0.5, 1, 1.5, 2, 0.75]) } }
  if (r < 0.85) return { tool: 'set_clip_reverse', args: { clip_id: c.id, reverse: !c.reverse } }
  if (r < 0.93) return { tool: 'undo', args: {} }
  return { tool: 'redo', args: {} }
}

function pct(xs: number[], p: number): number {
  if (!xs.length) return 0
  const s = [...xs].sort((a, b) => a - b)
  return s[Math.min(s.length - 1, Math.floor(p * (s.length - 1) + 0.5))]
}

function dispatchOnce(sid: string, op: unknown): Promise<Response> {
  return fetch(`/api/sessions/${sid}/dispatch?include=edl`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(op),
  })
}

export async function runStructural(opts: StructuralOptions): Promise<StructuralResult> {
  const t0 = performance.now()
  const rnd = prng(opts.seed)
  const srcs = Object.keys(opts.sources)
  const frames = Object.fromEntries(srcs.map((s) => [s, opts.sources[s].frames]))
  const events: DivergenceEvent[] = []
  const checker = new DivergenceChecker({ sessionId: opts.sid, telemetry: (e) => events.push(e), warn: null })
  const lookup = lookupFromJson(opts.sources)
  let edl = await (await fetch(`/api/sessions/${opts.sid}/edl`)).json() as Edl
  const fps = edl.canvas.fps
  const res: StructuralResult = {
    ua: navigator.userAgent, attempted: 0, applied: 0, rejected: {}, byTool: {}, outcomes: {},
    mismatches: [], others: [], clipsMax: 0, framesMax: 0, checkMs: { p50: 0, p95: 0, max: 0 },
    control: null, elapsedMs: 0,
  }
  let last: { hash: string; pm: ReturnType<typeof buildProgramMap> } | null = null
  const checkMs: number[] = []
  while (res.applied < opts.edits && res.attempted < opts.edits * 4) {
    const op = pickEdit(edl, rnd, srcs, frames, fps)
    res.attempted++
    let r = await dispatchOnce(opts.sid, op)
    // the per-path rate bucket (60 rps): this loop is faster than any user
    // once dispatch no longer probes its sources every time — wait and resend
    for (let i = 0; r.status === 429 && i < 50; i++) {
      const ra = Number.parseFloat(r.headers.get('Retry-After') ?? '')
      await new Promise((ok) => setTimeout(ok, Number.isFinite(ra) && ra > 0 ? Math.min(2000, ra * 1000) : 100))
      r = await dispatchOnce(opts.sid, op)
    }
    if (!r.ok) {
      res.rejected[`${op.tool}:${r.status}`] = (res.rejected[`${op.tool}:${r.status}`] ?? 0) + 1
      continue
    }
    const body = await r.json() as { edl?: Edl; render_hash: string; edl_omitted?: boolean }
    if (!body.edl) throw new Error(`dispatch ${op.tool} answered without an EDL`)
    edl = body.edl
    res.applied++
    res.byTool[op.tool] = (res.byTool[op.tool] ?? 0) + 1
    const pm = buildProgramMap(edl as unknown as EdlLike, lookup)
    res.clipsMax = Math.max(res.clipsMax, pm.clips.length)
    res.framesMax = Math.max(res.framesMax, pm.total)
    const c0 = performance.now()
    const out = await checker.check(body.render_hash, pm, opts.sources)
    checkMs.push(performance.now() - c0)
    res.outcomes[out.outcome] = (res.outcomes[out.outcome] ?? 0) + 1
    last = { hash: body.render_hash, pm }
  }
  const k = last ? last.pm.kind.findIndex((kd) => kd !== 1) : -1
  if (last && k >= 0) {
    // One source frame off by one at the first clip frame: the server must disagree there.
    last.pm.srcFrame[k] += 1
    const probe = new DivergenceChecker({ sessionId: opts.sid, warn: null })
    const out = await probe.check(last.hash, last.pm)
    res.control = { outcome: out.outcome, demote: out.demote }
  }
  res.mismatches = events.filter((e) => e.type === 'mismatch').slice(0, 5)
  res.others = events.filter((e) => e.type !== 'mismatch' && e.type !== 'match').slice(0, 5)
  res.checkMs = { p50: pct(checkMs, 0.5), p95: pct(checkMs, 0.95), max: Math.max(0, ...checkMs) }
  res.elapsedMs = performance.now() - t0
  return res
}
