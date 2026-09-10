// The reducer and the SSE reader, driven by the recorded stream in
// __fixtures__/prompt_stream.txt — three turns as `agent/prompt/service.py`
// frames them (single-line json.dumps behind `data: `, blank-line separated,
// `done` last): a full run with a brain ladder, two unknown event types and
// an `effect:"none"` step; a clarify with a `confirm` question; and a
// required-step failure. Splitting the fixture into arbitrary byte chunks
// (including through a multi-byte "—") is what proves the reader is stateful.
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import {
  EMPTY_RUN, PROMPT_EVENT_TYPES, humanBytes, humanDuration, isLoopbackOrigin, normalizeBrainsReport,
  parseSseText, promptRunningFromError, readSseStream, reduce, runProgress, shortModel, startRun,
  terminalAnnouncement,
  type PromptEvent, type PromptRunState,
} from './promptEvents'

const FIXTURE = readFileSync(fileURLToPath(new URL('./__fixtures__/prompt_stream.txt', import.meta.url)), 'utf-8')

/** The fixture's turns, keyed by the `: turn <name>` comment that opens each. */
function turns(): Record<string, string> {
  const out: Record<string, string> = {}
  let name = ''
  for (const line of FIXTURE.split('\n')) {
    const m = /^: turn (\S+)/.exec(line)
    if (m) { name = m[1]; out[name] = ''; continue }
    if (name) out[name] += line + '\n'
  }
  return out
}

const fold = (events: { type: string }[], from: PromptRunState = startRun()) =>
  events.reduce((s, e) => reduce(s, e as PromptEvent), from)

describe('the fixture itself keeps the wire invariants', () => {
  it('has three turns, every frame single-line JSON, done last and unique per turn', () => {
    const t = turns()
    expect(Object.keys(t)).toEqual(['tighten_and_captions', 'clarify_downloads', 'step_failure'])
    for (const text of Object.values(t)) {
      const dataLines = text.split('\n').filter((l) => l.startsWith('data: '))
      for (const l of dataLines) expect(() => JSON.parse(l.slice(6))).not.toThrow()
      const events = parseSseText(text)
      expect(events.filter((e) => e.type === 'done')).toHaveLength(1)
      expect(events[events.length - 1].type).toBe('done')
      // The first text_delta of every turn starts with `via <label> — ` (§4.1).
      const first = events.find((e) => e.type === 'text_delta') as unknown as { text: string }
      expect(first.text).toMatch(/^via (Recipes|Apple Intelligence|Local model|Claude) — /)
    }
  })

  it('covers what the spec asks the fixture to cover', () => {
    const run = parseSseText(turns().tighten_and_captions)
    expect(run.filter((e) => e.type === 'brain' && e.status === 'trying').length).toBeGreaterThanOrEqual(2)
    expect(run.filter((e) => !PROMPT_EVENT_TYPES.has(e.type)).map((e) => e.type)).toEqual(['typing', 'heartbeat'])
    expect(run.some((e) => e.type === 'step' && e.effect === 'none')).toBe(true)
    const clarify = parseSseText(turns().clarify_downloads).find((e) => e.type === 'clarify') as unknown as { questions: { kind: string }[] }
    expect(clarify.questions[0].kind).toBe('confirm')
  })
})

describe('reduce — a full run', () => {
  const events = parseSseText(turns().tighten_and_captions)
  const final = fold(events)

  it('keeps the whole brain ladder and makes the last `answered` authoritative', () => {
    expect(final.attempts.map((a) => `${a.brain}:${a.status}`)).toEqual([
      'recipes:trying', 'recipes:failed', 'apple_intelligence:trying', 'apple_intelligence:failed',
      'local_model:trying', 'local_model:answered',
    ])
    expect(final.brain?.brain).toBe('local_model')
    expect(final.brain?.model).toBe('mlx-community/Qwen2.5-7B-Instruct-4bit')
    expect(final.brain?.latency_ms).toBe(6100)
    // The failed FM attempt carries the honest reason the badge shows.
    expect(final.attempts[3].detail).toBe('appleIntelligenceNotEnabled')
  })

  it('walks planning → running → verifying → done and counts unknown events instead of failing', () => {
    const seen: string[] = []
    let s = startRun()
    for (const e of events) {
      s = reduce(s, e as PromptEvent)
      if (seen[seen.length - 1] !== s.status) seen.push(s.status)
    }
    expect(seen).toEqual(['planning', 'running', 'verifying', 'done'])
    expect(final.unknownEvents).toBe(2)
  })

  it('upserts step rows by index, attaches tool args/results by id, and keeps effect:none', () => {
    expect(final.steps.map((r) => `${r.index}:${r.tool}:${r.status}`)).toEqual([
      '0:remove_silences:ok', '1:remove_fillers:ok', '2:add_caption_track:ok', '3:verify_render:ok',
    ])
    expect(final.steps[0].effect).toBe('none')
    expect(final.steps[0].summary).toBe('Removed 0 silences')
    expect(final.steps[1].args).toEqual({ words: ['um', 'uh', 'hmm', 'erm', 'uhh', 'umm'], pad: 0.05, track: 'v1' })
    expect((final.steps[1].result as { cuts: number }).cuts).toBe(4)
    expect(final.steps[2].args).toMatchObject({ style: 'ig_chunky' })
  })

  it('records the verify table, the op and the accumulated reply', () => {
    expect(final.verify?.passed).toBe(4)
    expect(final.verify?.total).toBe(5)
    expect(final.verify?.checks.find((c) => c.check === 'silence_total_leq')?.pass).toBe(false)
    expect(final.opSeen).toBe(true)
    expect(final.reply.startsWith('via Local model — working on')).toBe(true)
    expect(final.reply).toContain('4 of 5 checks passed')
    expect(final.lastError).toBeNull()
    expect(terminalAnnouncement(final)).toBe('Done — 4 of 5 checks passed')
  })

  it('reports fractional progress from the step rows, excluding the verify render', () => {
    const idx = events.findIndex((e) => e.type === 'step' && e.tool === 'remove_fillers' && e.progress === 0.5)
    const mid = fold(events.slice(0, idx + 1))
    expect(runProgress(mid.steps)).toBeCloseTo(1.5 / 3, 5)
    expect(runProgress(final.steps)).toBe(1)
    expect(runProgress([])).toBeNull()
  })

  it('never mutates its inputs', () => {
    const s0 = startRun()
    const frozen = Object.freeze({ ...s0, steps: Object.freeze([]) as unknown as PromptRunState['steps'] })
    const e = events.find((x) => x.type === 'step')!
    const before = JSON.stringify(e)
    expect(() => reduce(frozen as PromptRunState, e as PromptEvent)).not.toThrow()
    expect(JSON.stringify(e)).toBe(before)
    expect(EMPTY_RUN.steps).toHaveLength(0)
  })
})

describe('reduce — clarify and failure turns', () => {
  it('a clarify turn pauses in `clarify` and `done` does not overwrite it', () => {
    const events = parseSseText(turns().clarify_downloads)
    const s = fold(events)
    expect(s.status).toBe('clarify')
    expect(s.clarify?.token).toBe('clr_5e6f7a8b')
    expect(s.clarify?.questions[0]).toMatchObject({ key: 'downloads', kind: 'confirm', required: true })
    expect(s.plan?.downloads_needed?.[0].bytes).toBe(3_000_000_000)
    expect(s.brain?.brain).toBe('recipes')
    expect(terminalAnnouncement(s)).toBe('One question before running')
    // The plan re-sent after an answer clears the pending question.
    const resumed = reduce(s, { type: 'plan', plan: { ...s.plan!, needs_input: [] } })
    expect(resumed.clarify).toBeNull()
    expect(resumed.status).toBe('running')
  })

  it('a required-step failure ends in `error` with the server sentence verbatim', () => {
    const events = parseSseText(turns().step_failure)
    const s = fold(events)
    expect(s.status).toBe('error')
    expect(s.lastError).toBe('Step 1/2 auto_reframe failed: cv2 not importable. Timeline unchanged; transcript restored.')
    expect(s.steps[0]).toMatchObject({ tool: 'auto_reframe', status: 'failed', error: 'cv2 not importable' })
    expect((s.steps[0].result as { error: string }).error).toBe('cv2 not importable')
    expect(s.opSeen).toBe(false)
    expect(terminalAnnouncement(s)).toMatch(/^Failed — Step 1\/2/)
  })
})

describe('readSseStream', () => {
  function chunked(text: string, size: number): ReadableStream<Uint8Array> {
    const bytes = new TextEncoder().encode(text)
    let i = 0
    return new ReadableStream<Uint8Array>({
      pull(controller) {
        if (i >= bytes.length) { controller.close(); return }
        controller.enqueue(bytes.slice(i, i + size))
        i += size
      },
    })
  }

  it('yields the same events as the whole-text parser at every chunk size, including mid-glyph splits', async () => {
    const text = turns().tighten_and_captions
    const expected = parseSseText(text)
    for (const size of [1, 7, 64, 4096]) {
      const got: { type: string }[] = []
      const { dropped } = await readSseStream(chunked(text, size), (e) => got.push(e))
      expect(dropped).toBe(0)
      expect(got).toEqual(expected)
    }
    // The reply's em dash survived the 1-byte split intact.
    const first = expected.find((e) => e.type === 'text_delta') as unknown as { text: string }
    expect(first.text).toContain('—')
  })

  it('drops one malformed frame and keeps reading', async () => {
    const text = 'data: {"type":"brain","status":"trying","brain":"recipes","label":"Recipes"}\n\n'
      + 'data: {not json\n\n'
      + 'data: "a string is not a frame"\n\n'
      + 'data: {"type":"done"}\n\n'
    const got: string[] = []
    const { dropped } = await readSseStream(chunked(text, 5), (e) => got.push(e.type))
    expect(got).toEqual(['brain', 'done'])
    expect(dropped).toBe(2)
  })

  it('flushes a final frame that arrived without its trailing blank line', async () => {
    const got: string[] = []
    await readSseStream(chunked('data: {"type":"done"}', 3), (e) => got.push(e.type))
    expect(got).toEqual(['done'])
  })
})

describe('409 prompt_running detection', () => {
  it('reads the hardening envelope (details.code), a bare body, and a FastAPI detail', () => {
    const envelope = new Error('409 Conflict: ' + JSON.stringify({
      error: { code: 'CONFLICT', message: 'request failed', request_id: 'abc', details: { code: 'prompt_running', run_id: 'run_1' } },
    }))
    expect(promptRunningFromError(envelope)).toEqual({ runId: 'run_1' })
    expect(promptRunningFromError(new Error('409 Conflict: {"code":"prompt_running","run_id":"run_2"}'))).toEqual({ runId: 'run_2' })
    expect(promptRunningFromError(new Error('409 Conflict: {"detail":{"code":"prompt_running"}}'))).toEqual({ runId: '' })
  })
  it('is null for every other failure', () => {
    expect(promptRunningFromError(new Error('409 Conflict: {"error":{"code":"CONFLICT","message":"LAN mode is off"}}'))).toBeNull()
    expect(promptRunningFromError(new Error('422 Unprocessable Entity: {"error":{"message":"no clip on v1"}}'))).toBeNull()
    expect(promptRunningFromError(new Error('network down'))).toBeNull()
    expect(promptRunningFromError('409 Conflict: {broken')).toBeNull()
  })
})

describe('normalizeBrainsReport', () => {
  const rows = {
    recipes: { available: true, detail: '27 recipes, 81 tools', fix: null, action: 'none', model: null },
    apple_intelligence: { available: false, detail: 'appleIntelligenceNotEnabled', fix: 'Turn on Apple Intelligence in System Settings', action: 'enable_in_settings', model: null },
    local_model: { available: false, detail: 'installed; model not downloaded', fix: 'Download Qwen2.5-7B-Instruct-4bit (~4.3 GB)', action: 'download', model: 'mlx-community/Qwen2.5-7B-Instruct-4bit', bytes: 4_300_000_000 },
  }
  it('accepts a list, a keyed map, or a top-level map and fills missing brains as unavailable', () => {
    const asList = normalizeBrainsReport({ brains: Object.entries(rows).map(([id, r]) => ({ id, ...r })) })
    const asMap = normalizeBrainsReport({ brains: rows })
    const topLevel = normalizeBrainsReport(rows)
    for (const rep of [asList, asMap, topLevel]) {
      expect(rep.brains.map((b) => b.id)).toEqual(['recipes', 'apple_intelligence', 'local_model', 'claude'])
      expect(rep.brains[0]).toMatchObject({ available: true, label: 'Recipes' })
      expect(rep.brains[1]).toMatchObject({ available: false, action: 'enable_in_settings', fix: 'Turn on Apple Intelligence in System Settings' })
      expect(rep.brains[2]).toMatchObject({ action: 'download', bytes: 4_300_000_000 })
      expect(rep.brains[3]).toMatchObject({ id: 'claude', available: false, detail: 'not reported', label: 'Claude' })
    }
  })
  it('never invents an action it cannot offer', () => {
    const rep = normalizeBrainsReport({ brains: [{ id: 'claude', available: false, action: 'phone_home' }] })
    expect(rep.brains.find((b) => b.id === 'claude')?.action).toBe('none')
    expect(normalizeBrainsReport(null).brains).toHaveLength(4)
  })
})

describe('human formatting', () => {
  it('bytes and durations read like the phone copy', () => {
    expect(humanBytes(3_000_000_000)).toBe('3.0 GB')
    expect(humanBytes(480_000_000)).toBe('480 MB')
    expect(humanBytes(60_000_000)).toBe('60 MB')
    expect(humanBytes(0)).toBe('0 B')
    expect(humanDuration(14)).toBe('about 15 seconds')
    expect(humanDuration(40)).toBe('about 40 seconds')
    expect(humanDuration(150)).toBe('about 3 minutes')
    expect(humanDuration(60 * 1)).toBe('about 60 seconds')
    expect(humanDuration(95)).toBe('about 2 minutes')
  })
})

describe('badge helpers', () => {
  it('names the local model the way the pill has room for', () => {
    expect(shortModel('mlx-community/Qwen2.5-7B-Instruct-4bit')).toBe('Qwen 7B')
    expect(shortModel('mlx-community/Qwen2.5-3B-Instruct-4bit')).toBe('Qwen 3B')
    expect(shortModel('apple-fm')).toBe('apple-fm')
    expect(shortModel('org/a-very-long-model-name-that-keeps-going-4bit')).toBe('a-very-long-model-nam…')
    expect(shortModel(null)).toBe('')
  })
  it('offers Download only on loopback origins — the route is loopback-only', () => {
    for (const h of ['localhost', '127.0.0.1', '::1', '[::1]']) expect(isLoopbackOrigin(h)).toBe(true)
    for (const h of ['10.120.2.82', 'sudhanshus-mac.local', '', 'localhost.evil.com']) expect(isLoopbackOrigin(h)).toBe(false)
  })
})

describe('reduce — a late progress tick', () => {
  it('never flips a finished step back to running', () => {
    let st = startRun()
    st = reduce(st, { type: 'step', index: 0, total: 1, tool: 'auto_reframe', status: 'running', progress: 0.2 })
    st = reduce(st, { type: 'step', index: 0, total: 1, tool: 'auto_reframe', status: 'ok', summary: 'reframed' })
    st = reduce(st, { type: 'step', index: 0, total: 1, tool: 'auto_reframe', status: 'running', progress: 0.3 })
    expect(st.steps).toHaveLength(1)
    expect(st.steps[0].status).toBe('ok')
    expect(st.steps[0].summary).toBe('reframed')
    // a download pre-step and the verify render share the index after the real steps: separate rows
    st = reduce(st, { type: 'step', index: 1, total: 1, tool: 'download', status: 'ok', summary: 'downloaded x' })
    st = reduce(st, { type: 'step', index: 1, total: 1, tool: 'verify_render', status: 'running', progress: 0 })
    expect(st.steps.map((s) => s.tool)).toEqual(['auto_reframe', 'download', 'verify_render'])
  })
})
