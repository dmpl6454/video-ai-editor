// The Prompt Editor's wire contract on the desktop side, and the pure
// reducer that folds one SSE turn into run state.
//
// Everything here mirrors `agent/prompt/service.py` (spec §4.1): the six
// legacy chat events plus `brain`, `plan`, `step`, `verify`, `clarify`.
// Frames are single-line `json.dumps` objects behind `data: `, separated by a
// blank line, `done` last. The reader below is the one loop both the Prompt
// bar and ChatOverlay use — it was extracted from ChatOverlay rather than
// duplicated so a fix to frame handling lands in both.
//
// WHY the reducer is pure and unknown events are counted, not thrown: the
// backend may add event types before this file learns them (the phone does
// the same in mobile/lib/sse.ts, which returns `{kind:"empty"}` for anything
// outside its KNOWN_TYPES). A run must never stall on a frame it does not
// understand, and a test can drive the whole state machine from a recorded
// stream without a DOM or a network.

import type { Op } from '../types'

// ---------------------------------------------------------------------------
// Plan shapes (agent/prompt/schema.py::PLAN_JSON_SCHEMA, the parts the UI reads)
// ---------------------------------------------------------------------------

export type BrainId = 'recipes' | 'apple_intelligence' | 'local_model' | 'claude'
export type NeedsInputKind = 'choice' | 'number' | 'text' | 'duration' | 'path' | 'confirm'

export interface NeedsInputOption { value: unknown; label: string; hint?: string; synonyms?: string[] }
export interface NeedsInput {
  key: string
  question: string
  kind?: NeedsInputKind
  options?: NeedsInputOption[]
  default?: unknown
  required: boolean
  min?: number
  max?: number
  unit?: string
}
export interface PlanStep { tool: string; args: Record<string, unknown>; why: string; optional?: boolean; stage?: number }
export interface Postcondition { check: string; args: Record<string, unknown>; human: string; needs_render?: boolean; headline?: boolean }
export interface DownloadNeeded { what: string; bytes: number; tool: string }
export interface Plan {
  version: number
  id?: string
  intent: string
  title?: string
  steps: PlanStep[]
  needs_input: NeedsInput[]
  postconditions: Postcondition[]
  downloads_needed?: DownloadNeeded[]
  estimated_seconds?: number
  confidence: number
  brain: BrainId
  content_brain?: BrainId | null
  reply?: string
}

// ---------------------------------------------------------------------------
// Events
// ---------------------------------------------------------------------------

export type BrainStatus = 'trying' | 'answered' | 'failed'
export type StepStatus = 'running' | 'ok' | 'failed' | 'skipped'

export interface BrainEvent {
  type: 'brain'; status: BrainStatus; brain: string; label: string
  model?: string; detail?: string; latency_ms?: number
}
export interface StepEvent {
  type: 'step'; index: number; total: number; tool: string; status: StepStatus
  progress?: number; summary?: string; effect?: 'none'; error?: string
}
export interface VerifyCheck {
  check: string; human: string; pass: boolean | null
  measured?: unknown; expected?: unknown; unit?: string; detail?: string; headline?: boolean
}
export interface VerifyEvent {
  type: 'verify'; plan_id: string; checks: VerifyCheck[]; passed: number; total: number; rendered: boolean
}
export interface ClarifyEvent { type: 'clarify'; token: string; plan_id: string; questions: NeedsInput[]; expires_in_s: number }

export type PromptEvent =
  | { type: 'text_delta'; text: string }
  | { type: 'tool_use'; name: string; args: Record<string, unknown>; id: string }
  | { type: 'tool_result'; name: string; result: unknown; id: string; is_error?: boolean }
  | { type: 'op'; op: Op }
  | { type: 'done' }
  | { type: 'error'; message: string }
  | BrainEvent
  | { type: 'plan'; plan: Plan }
  | StepEvent
  | VerifyEvent
  | ClarifyEvent

export const PROMPT_EVENT_TYPES = new Set<string>([
  'text_delta', 'tool_use', 'tool_result', 'op', 'done', 'error',
  'brain', 'plan', 'step', 'verify', 'clarify',
])

// Display labels — the same strings `brains/base.py::BRAIN_LABELS` uses in
// the `via <label> — ` prefix, so the badge and the reply never disagree.
export const BRAIN_LABELS: Record<string, string> = {
  recipes: 'Recipes',
  apple_intelligence: 'Apple Intelligence',
  local_model: 'Local model',
  claude: 'Claude',
}
export const brainLabel = (id: string | null | undefined): string =>
  id ? (BRAIN_LABELS[id] ?? id) : ''

// ---------------------------------------------------------------------------
// Run state + reducer
// ---------------------------------------------------------------------------

export type PromptStatus = 'idle' | 'planning' | 'running' | 'verifying' | 'clarify' | 'done' | 'error'

export interface BrainAttempt {
  status: BrainStatus; brain: string; label: string
  model?: string; detail?: string; latency_ms?: number
}
export interface StepRow {
  index: number; total: number; tool: string; status: StepStatus
  progress?: number; summary?: string; effect?: 'none'; error?: string
  args?: Record<string, unknown>; result?: unknown
}
export interface ClarifyState { token: string; planId: string; questions: NeedsInput[]; expiresInS: number }

export interface PromptRunState {
  status: PromptStatus
  // The last `answered` brain; `attempts` keeps the whole ladder for the badge.
  brain: BrainAttempt | null
  attempts: BrainAttempt[]
  plan: Plan | null
  steps: StepRow[]
  verify: VerifyEvent | null
  reply: string
  clarify: ClarifyState | null
  lastError: string | null
  opSeen: boolean
  unknownEvents: number
}

export const EMPTY_RUN: PromptRunState = {
  status: 'idle', brain: null, attempts: [], plan: null, steps: [], verify: null,
  reply: '', clarify: null, lastError: null, opSeen: false, unknownEvents: 0,
}

/** The state a fresh turn starts from: everything cleared, status `planning`. */
export const startRun = (): PromptRunState => ({ ...EMPTY_RUN, status: 'planning' })

// Step events for the verify render carry this tool name (spec §4.4).
const VERIFY_RENDER_TOOL = 'verify_render'

// `tool_use`/`tool_result` ids are `f"{plan.id}_s{index}"` (spec §4.1).
const STEP_ID_RE = /_s(\d+)$/
function stepIndexFromId(id: string | undefined): number | null {
  const m = id ? STEP_ID_RE.exec(id) : null
  return m ? Number(m[1]) : null
}

const TERMINAL: ReadonlySet<StepStatus> = new Set(['ok', 'failed', 'skipped'])

function upsertStep(steps: StepRow[], next: StepRow): StepRow[] {
  const i = steps.findIndex((s) => s.index === next.index && s.tool === next.tool)
  if (i === -1) return [...steps, next].sort((a, b) => a.index - b.index)
  // A progress tick that raced the terminal frame must not flip a finished
  // row back to "running" — there is no later event to fix it.
  if (next.status === 'running' && TERMINAL.has(steps[i].status)) return steps
  return steps.map((s, j) => (j === i ? { ...s, ...next } : s))
}

function patchStep(steps: StepRow[], index: number | null, tool: string, patch: Partial<StepRow>): StepRow[] {
  // Prefer the id-derived index; fall back to the most recent row for that
  // tool that has not received the patch yet (a `$v1_all` fan-out shares one
  // step index across several dispatches, so several results may land on it).
  let i = index === null ? -1 : steps.findIndex((s) => s.index === index)
  if (i === -1) {
    for (let j = steps.length - 1; j >= 0; j--) {
      if (steps[j].tool === tool) { i = j; break }
    }
  }
  if (i === -1) return steps
  return steps.map((s, j) => (j === i ? { ...s, ...patch } : s))
}

/**
 * Fold one event into the run state. Pure: returns a new object, never
 * mutates `state` or `evt`.
 */
export function reduce(state: PromptRunState, evt: PromptEvent | { type: string }): PromptRunState {
  switch (evt.type) {
    case 'text_delta': {
      const e = evt as Extract<PromptEvent, { type: 'text_delta' }>
      return { ...state, reply: state.reply + e.text }
    }
    case 'brain': {
      const e = evt as BrainEvent
      const attempt: BrainAttempt = {
        status: e.status, brain: e.brain, label: e.label || brainLabel(e.brain),
        model: e.model, detail: e.detail, latency_ms: e.latency_ms,
      }
      return {
        ...state,
        attempts: [...state.attempts, attempt],
        brain: e.status === 'answered' ? attempt : state.brain,
      }
    }
    case 'plan': {
      const e = evt as Extract<PromptEvent, { type: 'plan' }>
      // A plan re-sent after a clarification replaces the paused one; the
      // steps it will run start fresh.
      return { ...state, plan: e.plan, clarify: null,
               status: e.plan.steps.length ? 'running' : state.status }
    }
    case 'step': {
      const e = evt as StepEvent
      const row: StepRow = {
        index: e.index, total: e.total, tool: e.tool, status: e.status,
        progress: e.progress, summary: e.summary, effect: e.effect, error: e.error,
      }
      const verifying = e.tool === VERIFY_RENDER_TOOL
      return { ...state, steps: upsertStep(state.steps, row),
               status: verifying ? 'verifying' : 'running' }
    }
    case 'tool_use': {
      const e = evt as Extract<PromptEvent, { type: 'tool_use' }>
      return { ...state, steps: patchStep(state.steps, stepIndexFromId(e.id), e.name, { args: e.args }) }
    }
    case 'tool_result': {
      const e = evt as Extract<PromptEvent, { type: 'tool_result' }>
      return { ...state, steps: patchStep(state.steps, stepIndexFromId(e.id), e.name, { result: e.result }) }
    }
    case 'verify': {
      const e = evt as VerifyEvent
      return { ...state, verify: e, status: 'verifying' }
    }
    case 'clarify': {
      const e = evt as ClarifyEvent
      return { ...state, status: 'clarify',
               clarify: { token: e.token, planId: e.plan_id, questions: e.questions, expiresInS: e.expires_in_s } }
    }
    case 'op':
      return { ...state, opSeen: true }
    case 'error': {
      const e = evt as Extract<PromptEvent, { type: 'error' }>
      return { ...state, status: 'error', lastError: e.message }
    }
    case 'done':
      // `done` closes the turn; what it means depends on what came before it.
      if (state.status === 'clarify') return state
      if (state.status === 'error') return state
      return { ...state, status: 'done' }
    default:
      return { ...state, unknownEvents: state.unknownEvents + 1 }
  }
}

/** Overall run progress in [0,1] from the step rows (verify render excluded). */
export function runProgress(steps: StepRow[]): number | null {
  const real = steps.filter((s) => s.tool !== VERIFY_RENDER_TOOL)
  if (!real.length) return null
  const total = Math.max(real[0].total || real.length, real.length)
  let done = 0
  for (const s of real) {
    if (s.status === 'ok' || s.status === 'skipped' || s.status === 'failed') done += 1
    else if (s.status === 'running' && typeof s.progress === 'number') done += Math.max(0, Math.min(0.99, s.progress))
  }
  return Math.max(0, Math.min(1, done / total))
}

/** The one-line terminal announcement for the live region (spec §5.4). */
export function terminalAnnouncement(state: PromptRunState): string | null {
  if (state.status === 'done') {
    if (state.verify) return `Done — ${state.verify.passed} of ${state.verify.total} checks passed`
    return state.plan && state.plan.steps.length ? 'Done' : 'Answered'
  }
  if (state.status === 'error') return `Failed — ${state.lastError ?? 'unknown error'}`
  if (state.status === 'clarify') return 'One question before running'
  return null
}

// ---------------------------------------------------------------------------
// SSE reader — shared by PromptBar and ChatOverlay
// ---------------------------------------------------------------------------

/**
 * Read a `data: {json}\n\n` stream to the end, calling `onEvent` per frame.
 * One malformed frame is counted and skipped, never thrown — an uncaught
 * parse error here once escaped ChatOverlay's read loop and stopped chat
 * mid-sentence with nothing on screen to say so. Resolves with the number of
 * frames dropped. A rejected `reader.read()` (network drop) propagates so the
 * caller can say the connection dropped.
 */
export async function readSseStream(
  body: ReadableStream<Uint8Array>,
  onEvent: (evt: { type: string } & Record<string, unknown>) => void,
): Promise<{ dropped: number }> {
  const reader = body.getReader()
  const dec = new TextDecoder()
  let buf = ''
  let dropped = 0
  const handleBlock = (block: string) => {
    // A block may hold several lines (SSE comments `: …`, `event:` lines);
    // only `data:` lines carry frames and each is one whole JSON object.
    for (const line of block.split('\n')) {
      if (!line.startsWith('data: ')) continue
      let evt: { type: string } & Record<string, unknown>
      try {
        evt = JSON.parse(line.slice(6))
      } catch {
        dropped += 1
        console.warn('[prompt] skipping malformed SSE frame:', line.slice(0, 120))
        continue
      }
      if (!evt || typeof evt !== 'object' || typeof evt.type !== 'string') { dropped += 1; continue }
      onEvent(evt)
    }
  }
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buf += dec.decode(value, { stream: true })
    const blocks = buf.split('\n\n')
    buf = blocks.pop() ?? ''
    for (const block of blocks) handleBlock(block)
  }
  if (buf.trim()) handleBlock(buf)
  return { dropped }
}

/** Parse a whole recorded transcript (the test fixture, or a replay body). */
export function parseSseText(text: string): ({ type: string } & Record<string, unknown>)[] {
  const out: ({ type: string } & Record<string, unknown>)[] = []
  for (const line of text.split('\n')) {
    if (!line.startsWith('data: ')) continue
    try {
      const evt = JSON.parse(line.slice(6))
      if (evt && typeof evt.type === 'string') out.push(evt)
    } catch { /* a malformed frame is dropped here exactly as the live reader drops it */ }
  }
  return out
}

// ---------------------------------------------------------------------------
// 409 prompt_running (spec §4.2) — raised by /dispatch while a run holds the lock
// ---------------------------------------------------------------------------

export const PROMPT_RUNNING_CODE = 'prompt_running'
export const PROMPT_RUNNING_MESSAGE = 'Prompt running — wait or cancel'

/**
 * `api.ts::http()` throws `Error("409 Conflict: <body>")`. The body is either
 * the hardening envelope — `{error:{code:"CONFLICT", message:"request
 * failed", details:{code:"prompt_running", run_id}}}`, because HTTPException
 * with a dict detail lands under `details` — or a bare
 * `{code:"prompt_running", run_id}` from a JSONResponse. Returns the run id
 * (possibly empty) when the error is that 409, else null.
 */
export function promptRunningFromError(e: unknown): { runId: string } | null {
  const raw = e instanceof Error ? e.message : String(e)
  const jsonStart = raw.indexOf('{')
  if (jsonStart === -1) return null
  try {
    const body = JSON.parse(raw.slice(jsonStart)) as {
      code?: unknown; run_id?: unknown
      error?: { code?: unknown; details?: { code?: unknown; run_id?: unknown } }
      detail?: { code?: unknown; run_id?: unknown }
    }
    const candidates = [body, body.error?.details, body.detail]
    for (const c of candidates) {
      if (c && c.code === PROMPT_RUNNING_CODE) return { runId: typeof c.run_id === 'string' ? c.run_id : '' }
    }
  } catch { /* not a JSON tail */ }
  return null
}

// store.ts must tell the prompt store about a 409 without importing it
// (promptStore imports store; a static edge back would be a cycle and a
// dynamic import() would not split — rolldown flags it as ineffective). A
// one-slot listener in this dependency-free module is the whole bridge.
type PromptRunningListener = (sid: string) => void
let _promptRunningListener: PromptRunningListener | null = null
export function onPromptRunning(listener: PromptRunningListener | null): void { _promptRunningListener = listener }
export function firePromptRunning(sid: string): boolean {
  if (!_promptRunningListener) return false
  _promptRunningListener(sid)
  return true
}

// ---------------------------------------------------------------------------
// brains_report() (spec §3.4) — tolerant of the two shapes B may emit
// ---------------------------------------------------------------------------

export type AvailabilityAction = 'none' | 'enable_in_settings' | 'install' | 'download' | 'add_key'

export interface BrainRow {
  id: string
  label: string
  available: boolean
  detail: string
  fix: string | null
  action: AvailabilityAction
  model: string | null
  // Present on the local_model row when the model manager knows it.
  bytes?: number | null
}
export interface BrainsReport { brains: BrainRow[]; summary?: string; default?: string | null }

const ORDER: string[] = ['recipes', 'apple_intelligence', 'local_model', 'claude']
const ACTIONS = new Set<string>(['none', 'enable_in_settings', 'install', 'download', 'add_key'])

/**
 * Normalise `/api/prompt/brains` into `BrainsReport`. Accepts `{brains:[…]}`,
 * `{brains:{id:{…}}}` or a top-level `{id:{…}}` map, and fills the honest
 * defaults (`available:false`, `fix:null`) for anything missing — a missing
 * row is shown as unavailable, never hidden.
 */
export function normalizeBrainsReport(raw: unknown): BrainsReport {
  const obj = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>
  const src = 'brains' in obj ? obj.brains : obj
  const rows: BrainRow[] = []
  const push = (id: string, r: unknown) => {
    const v = (r && typeof r === 'object' ? r : {}) as Record<string, unknown>
    const action = typeof v.action === 'string' && ACTIONS.has(v.action) ? (v.action as AvailabilityAction) : 'none'
    rows.push({
      id,
      label: typeof v.label === 'string' && v.label ? v.label : brainLabel(id),
      available: v.available === true,
      detail: typeof v.detail === 'string' ? v.detail : '',
      fix: typeof v.fix === 'string' ? v.fix : null,
      action,
      model: typeof v.model === 'string' ? v.model : null,
      bytes: typeof v.bytes === 'number' ? v.bytes : undefined,
    })
  }
  if (Array.isArray(src)) {
    for (const r of src) {
      const id = r && typeof r === 'object' && typeof (r as { id?: unknown }).id === 'string' ? (r as { id: string }).id : null
      if (id) push(id, r)
    }
  } else if (src && typeof src === 'object') {
    for (const [id, r] of Object.entries(src as Record<string, unknown>)) {
      if (r && typeof r === 'object' && !Array.isArray(r) && ORDER.includes(id)) push(id, r)
    }
  }
  const known = new Set(rows.map((r) => r.id))
  for (const id of ORDER) {
    if (!known.has(id)) rows.push({ id, label: brainLabel(id), available: false,
                                    detail: 'not reported', fix: null, action: 'none', model: null })
  }
  rows.sort((a, b) => ORDER.indexOf(a.id) - ORDER.indexOf(b.id))
  return {
    brains: rows,
    summary: typeof obj.summary === 'string' ? obj.summary : undefined,
    default: typeof obj.default === 'string' ? obj.default : null,
  }
}

// The Download action is offered only from a loopback origin: the route is
// loopback-only on the backend (403 otherwise), so offering it on a LAN page
// would be a button that cannot work.
const LOOPBACK_HOSTS = new Set(['localhost', '127.0.0.1', '::1', '[::1]'])
export function isLoopbackOrigin(hostname = typeof location === 'undefined' ? '' : location.hostname): boolean {
  return LOOPBACK_HOSTS.has(hostname)
}

/** "mlx-community/Qwen2.5-7B-Instruct-4bit" → "Qwen 7B"; anything else → its last path segment, trimmed. */
export function shortModel(model: string | null | undefined): string {
  if (!model) return ''
  const leaf = model.split('/').pop() ?? model
  const m = /^(qwen)[\d.]*-(\d+(?:\.\d+)?b)/i.exec(leaf)
  if (m) return `${m[1][0].toUpperCase()}${m[1].slice(1).toLowerCase()} ${m[2].toUpperCase()}`
  return leaf.length > 22 ? leaf.slice(0, 21) + '…' : leaf
}

/** Human size for a byte count — "4.3 GB", "480 MB", "60 MB". */
export function humanBytes(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return '0 B'
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)} GB`
  if (n >= 1e6) return `${Math.round(n / 1e6)} MB`
  if (n >= 1e3) return `${Math.round(n / 1e3)} kB`
  return `${n} B`
}

/** "about 2 minutes" / "about 40 seconds" for the plan header. */
export function humanDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return 'a moment'
  if (seconds < 90) return `about ${Math.max(5, Math.round(seconds / 5) * 5)} seconds`
  const m = Math.round(seconds / 60)
  return `about ${m} minute${m === 1 ? '' : 's'}`
}
