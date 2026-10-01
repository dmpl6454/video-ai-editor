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

import { promptOpRef, type PromptOpRef } from './promptUndo'
import { cleanText, normalizeBrainCard, type BrainCardInfo } from './brainDecisions'
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
export type StepStatus = 'running' | 'ok' | 'failed' | 'skipped' | 'cancelled'

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
  /** K3: a blocking check that fails rolls the run back; an advisory one
   *  (blocking false) is reported and the edit is kept. */
  blocking?: boolean
}
export interface VerifyEvent {
  type: 'verify'; plan_id: string; checks: VerifyCheck[]; passed: number; total: number; rendered: boolean
}
/** 0.8.0 "Preview, then apply": what a dry run WOULD change, from the EDL
 *  diff (agent/prompt/preview.py). Rides on the `clarify` frame whose only
 *  question is Apply, so a client that does not know it still gets a yes/no. */
export interface PreviewInfo {
  summary: string
  lines: string[]
  more: number
  total: number
  /** The lines past the cap ("and N more changes" opens to show them). */
  hidden?: string[]
  note?: string | null
  nothing_changed?: string
  /** Editor Brain (EB1): a brain run's decisions and reasons (lib/brainDecisions);
   *  absent for an ordinary prompt or with `brain.enabled` off. */
  brain?: BrainCardInfo | null
}
export interface ClarifyEvent {
  type: 'clarify'; token: string; plan_id: string; questions: NeedsInput[]; expires_in_s: number
  preview?: PreviewInfo | null
}

/** Editor Brain (EB1): the footage analysis job's progress frame
 *  `analysis{layer, pct, eta_s}` (service.py / brain_seams.analysis_event),
 *  read tolerantly: the optional fields a later backend adds (`job_id`, a
 *  `layers` list, `fraction`) are used when present and never required. */
export interface AnalysisLayer { name: string; pct: number | null; state: 'waiting' | 'running' | 'done' | 'failed' }
export interface AnalysisProgress {
  /** The layer being read now ('' when the job names none). */
  layer: string
  /** 0–100. */
  pct: number
  etaS: number | null
  /** The job to cancel (POST /api/jobs/{id}/cancel); null when the frame carries none. */
  jobId: string | null
  layers: AnalysisLayer[]
}

const finite = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)
const LAYER_STATES = new Set(['waiting', 'running', 'done', 'failed'])

function analysisLayer(raw: unknown): AnalysisLayer | null {
  if (typeof raw === 'string') return raw ? { name: raw, pct: null, state: 'waiting' } : null
  if (!raw || typeof raw !== 'object') return null
  const r = raw as Record<string, unknown>
  const name = typeof r.name === 'string' ? r.name : typeof r.layer === 'string' ? r.layer : ''
  if (!name) return null
  const frac = finite(r.fraction)
  const pct = finite(r.pct) ?? (frac === null ? null : frac * 100)
  const state = typeof r.state === 'string' && LAYER_STATES.has(r.state) ? (r.state as AnalysisLayer['state'])
    : pct !== null && pct >= 100 ? 'done' : pct !== null && pct > 0 ? 'running' : 'waiting'
  return { name, pct: pct === null ? null : Math.max(0, Math.min(100, pct)), state }
}

/** A wire `analysis` frame → AnalysisProgress (never throws on a partial frame). */
export function normalizeAnalysis(raw: unknown): AnalysisProgress {
  const r = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>
  const frac = finite(r.fraction) ?? finite(r.progress)
  const pct = finite(r.pct) ?? (frac === null ? 0 : frac <= 1 ? frac * 100 : frac)
  return {
    layer: typeof r.layer === 'string' ? r.layer : '',
    pct: Math.max(0, Math.min(100, pct)),
    etaS: finite(r.eta_s),
    jobId: typeof r.job_id === 'string' && r.job_id ? r.job_id : typeof r.job === 'string' && r.job ? r.job : null,
    layers: Array.isArray(r.layers) ? r.layers.map(analysisLayer).filter((l): l is AnalysisLayer => l !== null) : [],
  }
}

const LAYER_WORDS: Record<string, string> = {
  speech: 'speech', semantic: 'meaning', audio: 'sound', sound: 'sound', speakers: 'speakers', scenes: 'scenes',
  shots: 'shots', energy: 'energy', moments: 'moments', angles: 'camera angles',
}
/** "speech" → "speech", "unheard_voice" → "unheard voice" (the layer, as words). */
export const layerWords = (layer: string): string => LAYER_WORDS[layer] ?? layer.replace(/[_-]+/g, ' ').trim()

/** "Reading the footage — speech · 62% · about 20 s left" (the bar's one line). */
export function analysisLine(a: AnalysisProgress): string {
  const where = a.layer ? ` — ${layerWords(a.layer)}` : ''
  const eta = a.etaS === null || a.pct <= 0 || a.pct >= 100 ? '' : ` · about ${
    a.etaS < 90 ? `${Math.max(1, Math.round(a.etaS))} s` : `${Math.round(a.etaS / 60)} min`} left`
  return `Reading the footage${where} · ${a.pct >= 100 ? 'done' : `${Math.round(a.pct)}%`}${eta}`
}

/** A wire `preview` → PreviewInfo, or null when it is not one (tolerant of
 *  an older or partial backend, like every other reader here). */
export function normalizePreview(raw: unknown): PreviewInfo | null {
  if (!raw || typeof raw !== 'object') return null
  const r = raw as Record<string, unknown>
  const lines = Array.isArray(r.lines) ? r.lines.filter((x): x is string => typeof x === 'string') : []
  if (typeof r.summary !== 'string' || !lines.length) return null
  const num = (v: unknown, d: number) => (typeof v === 'number' && Number.isFinite(v) ? v : d)
  // an ordinary card carries no `brain` key at all: with the Editor Brain off
  // (or a plain prompt) the preview is the 0.8.0 object, field for field
  const brain = normalizeBrainCard(r.brain)
  return {
    summary: r.summary,
    lines,
    more: Math.max(0, num(r.more, 0)),
    total: Math.max(lines.length, num(r.total, lines.length)),
    hidden: Array.isArray(r.hidden) ? r.hidden.filter((x): x is string => typeof x === 'string') : [],
    note: typeof r.note === 'string' && r.note ? r.note : null,
    nothing_changed: typeof r.nothing_changed === 'string' ? r.nothing_changed : 'Nothing has changed yet.',
    ...(brain ? { brain } : {}),
  }
}

/** The analysis job's progress frame; every field but `type` is optional on the wire. */
export interface AnalysisEvent {
  type: 'analysis'; layer?: string; pct?: number; eta_s?: number | null
  job_id?: string; fraction?: number; layers?: unknown
}

export type PromptEvent =
  | { type: 'text_delta'; text: string; outcome?: string }
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
  | AnalysisEvent

export const PROMPT_EVENT_TYPES = new Set<string>([
  'text_delta', 'tool_use', 'tool_result', 'op', 'done', 'error',
  'brain', 'plan', 'step', 'verify', 'clarify',
  // Editor Brain (EB1): the analysis job's progress frame (service.BRAIN_EVENT_TYPES);
  // known, so a run never counts it as an unknown event.
  'analysis',
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

// `cancelled` (QA-064): the user stopped the run and the timeline is as it
// was — not a failure, and never labelled "Failed".
export type PromptStatus = 'idle' | 'planning' | 'running' | 'verifying' | 'clarify' | 'done' | 'error' | 'cancelled'

/** The executor's cancel sentence ("Cancelled — timeline unchanged."). */
export const isPromptCancelMessage = (m: string | null | undefined): boolean =>
  /^Cancelled\b|\(cancelled\)\. Nothing was changed/.test(m ?? '')

export interface BrainAttempt {
  status: BrainStatus; brain: string; label: string
  model?: string; detail?: string; latency_ms?: number
}
export interface StepRow {
  index: number; total: number; tool: string; status: StepStatus
  progress?: number; summary?: string; effect?: 'none'; error?: string
  args?: Record<string, unknown>; result?: unknown
}
export interface ClarifyState {
  token: string; planId: string; questions: NeedsInput[]; expiresInS: number
  /** Set when this pause is a preview card (Apply / Change), not a question. */
  preview?: PreviewInfo | null
}
/** A project a shorts run created and finished (`finish_short` records, QA-068). */
export interface ChildRun { session: string; name: string | null; status: string }

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
  /** Which op the run made (the run log's Undo checks it is still newest). */
  opRef: PromptOpRef | null
  unknownEvents: number
  /** Projects the run created and finished, in order (QA-068: Open buttons). */
  children: ChildRun[]
  /** The preview found nothing to change (`text_delta.outcome`
   *  "nothing_to_apply"): its dry-run steps are not "done" (final sweep 3 r2). */
  nothingToApply?: boolean
  /** Editor Brain: the footage analysis while the read is going (null otherwise). */
  analysis?: AnalysisProgress | null
}

export const EMPTY_RUN: PromptRunState = {
  status: 'idle', brain: null, attempts: [], plan: null, steps: [], verify: null,
  reply: '', clarify: null, lastError: null, opSeen: false, opRef: null, unknownEvents: 0, children: [],
  nothingToApply: false, analysis: null,
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

const TERMINAL: ReadonlySet<StepStatus> = new Set(['ok', 'failed', 'skipped', 'cancelled'])

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

const FINISH_SHORT_TOOL = 'finish_short'

function upsertChild(children: readonly ChildRun[] | undefined, result: unknown): ChildRun[] {
  const list = [...(children ?? [])]
  const r = (result && typeof result === 'object' ? result : {}) as { session?: unknown; name?: unknown; status?: unknown }
  if (typeof r.session !== 'string' || !r.session) return list
  const row: ChildRun = {
    session: r.session,
    name: typeof r.name === 'string' && r.name.trim() ? r.name.trim() : null,
    status: typeof r.status === 'string' ? r.status : 'ok',
  }
  const i = list.findIndex((c) => c.session === row.session)
  if (i === -1) list.push(row)
  else list[i] = row
  return list
}

/**
 * The projects a run created, in order, each with a name when one is known:
 * the finished shorts first (their records carry the name), then any other
 * session a step's result reports in `new_sessions` (a shorts run that was
 * not finished). The run log renders one Open button per entry (QA-068).
 */
export function createdProjects(state: Pick<PromptRunState, 'steps' | 'children'>): ChildRun[] {
  const out: ChildRun[] = [...(state.children ?? [])]
  for (const s of state.steps) {
    const ids = (s.result && typeof s.result === 'object' ? (s.result as { new_sessions?: unknown }).new_sessions : null)
    if (!Array.isArray(ids)) continue
    for (const id of ids) {
      if (typeof id === 'string' && id && !out.some((c) => c.session === id)) out.push({ session: id, name: null, status: 'ok' })
    }
  }
  return out
}

/** A plan the Editor Brain compiled: its sentinel steps carry the EDP's `plan_ref`. */
export function isBrainPlan(plan: Plan | null | undefined): boolean {
  return !!plan?.steps?.some((s) => typeof s.args?.plan_ref === 'string')
}

/** The tools only the Editor Brain's plans carry (a stored run record has no plan to read `plan_ref` from). */
const BRAIN_ONLY_TOOLS: ReadonlySet<string> = new Set(['cut_source_ranges', 'apply_camera_plan', 'sync_dialogue_lane'])
export const ranBrainTools = (tools: readonly string[]): boolean => tools.some((t) => BRAIN_ONLY_TOOLS.has(t))

/** A check that only ADVISES ("score is reported, not gated"): a failure is a note, never a miss in the headline. */
export const ADVISORY_CHECKS: ReadonlySet<string> = new Set(['audit_ok'])

/** The verify event of a brain run with its advisory checks moved out of the headline (`headline:false`
 *  is how the run log already lists an info check) and the counts over what gates. */
export function withAdvisoryNotes(e: VerifyEvent): VerifyEvent {
  if (!e.checks.some((c) => ADVISORY_CHECKS.has(c.check) && c.pass === false)) return e
  const checks = e.checks.map((c) => (ADVISORY_CHECKS.has(c.check) ? { ...c, headline: false } : c))
  const gating = checks.filter((c) => c.headline !== false && c.pass !== null)
  return { ...e, checks, passed: gating.filter((c) => c.pass === true).length, total: gating.length }
}

/** A step's one-line summary as a person reads it: no ids, no dangling "Reorder v1: , , ,". */
export function tidySummary(text: string | undefined): string | undefined {
  if (typeof text !== 'string') return text
  return cleanText(text).replace(/:\s*(?:,\s*)*$/, '')
}

/**
 * Fold one event into the run state. Pure: returns a new object, never
 * mutates `state` or `evt`.
 */
export function reduce(state: PromptRunState, evt: PromptEvent | { type: string }): PromptRunState {
  switch (evt.type) {
    case 'text_delta': {
      const e = evt as Extract<PromptEvent, { type: 'text_delta' }>
      return { ...state, reply: state.reply + e.text,
               nothingToApply: !!state.nothingToApply || e.outcome === NOTHING_TO_APPLY }
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
      return { ...state, plan: e.plan, clarify: null, analysis: null,
               status: e.plan.steps.length ? 'running' : state.status }
    }
    case 'step': {
      const e = evt as StepEvent
      const row: StepRow = {
        index: e.index, total: e.total, tool: e.tool, status: e.status,
        progress: e.progress, summary: isBrainPlan(state.plan) ? tidySummary(e.summary) : e.summary,
        effect: e.effect, error: e.error,
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
      // A finished short (executor._finish_children) is not a plan step: it
      // becomes an Open button, by its name (QA-068).
      if (e.name === FINISH_SHORT_TOOL) return { ...state, children: upsertChild(state.children, e.result) }
      return { ...state, steps: patchStep(state.steps, stepIndexFromId(e.id), e.name, { result: e.result }) }
    }
    case 'verify': {
      const e = evt as VerifyEvent
      return { ...state, verify: isBrainPlan(state.plan) ? withAdvisoryNotes(e) : e, status: 'verifying' }
    }
    case 'clarify': {
      const e = evt as ClarifyEvent
      return { ...state, status: 'clarify', analysis: null,
               clarify: { token: e.token, planId: e.plan_id, questions: e.questions, expiresInS: e.expires_in_s,
                          preview: normalizePreview(e.preview) } }
    }
    case 'op':
      return { ...state, opSeen: true, opRef: promptOpRef((evt as Extract<PromptEvent, { type: 'op' }>).op) }
    case 'error': {
      const e = evt as Extract<PromptEvent, { type: 'error' }>
      const cancelled = isPromptCancelMessage(e.message)
      // A cancel never sends the executing step a terminal frame: its row
      // stops as "cancelled" instead of spinning forever (QA-064).
      const steps = cancelled
        ? state.steps.map((s) => (s.status === 'running' ? { ...s, status: 'cancelled' as const } : s))
        : state.steps
      return { ...state, steps, analysis: null, status: cancelled ? 'cancelled' : 'error', lastError: e.message }
    }
    case 'done':
      // `done` closes the turn; what it means depends on what came before it.
      if (state.status === 'clarify') return state
      if (state.status === 'error' || state.status === 'cancelled') return state
      // A card dropped or a run forgotten while its stream was still open (Cancel during the read, then
      // the late `done`): nothing is running, so nothing finished.
      if (state.status === 'idle' && !state.plan) return state
      return { ...state, analysis: null, status: 'done' }
    case 'analysis':
      return { ...state, analysis: normalizeAnalysis(evt) }    // the bar's reading line and its Cancel
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
    // Final sweep 3 r2: a preview that would change nothing streamed its dry
    // run's steps as "ok" and was announced "Done" — nothing was done.
    if (state.nothingToApply) return `Nothing to change — ${replySentence(state.reply)}`
    if (state.verify) return `Done — ${state.verify.passed} of ${state.verify.total} checks passed`
    if (state.plan && state.plan.steps.length && !state.reply.trim() && !state.opSeen) return RUN_WITHOUT_RESULT
    // An undo / redo changes the timeline with no plan steps: say what it
    // undid, not "Answered" (final sweep 3 r2)
    if (!(state.plan && state.plan.steps.length) && state.opSeen && state.reply.trim()) return replySentence(state.reply)
    return state.plan && state.plan.steps.length ? 'Done' : 'Answered'
  }
  if (state.status === 'cancelled') return 'Cancelled — the timeline is unchanged'
  if (state.status === 'error') return `Failed — ${state.lastError ?? 'unknown error'}`
  if (state.status === 'clarify') {
    const p = state.clarify?.preview
    return p ? previewAnnouncement(p) : 'One question before running'
  }
  return null
}

/** The `text_delta.outcome` of a preview that would change nothing. */
export const NOTHING_TO_APPLY = 'nothing_to_apply'
/** A run that ended with a plan but no text, op or card (a failure the
 *  server could not report): never "Nothing to change" (final sweep 3 r2). */
export const RUN_WITHOUT_RESULT = 'The run ended without a result — nothing was changed.'

/** A reply without its "via Recipes — " lead, for a one-line announcement. */
export function replySentence(reply: string): string {
  return (reply ?? '').replace(/^\s*via [^—\n]{1,40} — /, '').trim()
}

/** What a screen reader hears when a preview card appears (spec: the card
 *  and the result are both announced). */
export function previewAnnouncement(p: PreviewInfo): string {
  // Final sweep 3: the note ("The timeline changed since the preview, so
  // nothing was applied") and what the card would do were never heard — a
  // screen reader got only the count, with focus back on Apply.
  const n = p.total
  const note = p.note ? `${p.note.trim().replace(/[.!?]?$/, '.')} ` : ''
  const first = p.lines.slice(0, 2).join('; ')
  const rest = n - Math.min(2, p.lines.length)
  const what = first ? `${first}${rest > 0 ? `; and ${rest} more` : ''}. ` : ''
  return `Preview ready: ${n} change${n === 1 ? '' : 's'}. ${note}${what}` +
    `${p.nothing_changed || 'Nothing has changed yet.'} Enter applies, Escape goes back to the prompt to change it.`
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

// The same bridge for a PROJECT SWITCH (QA-062): the prompt store's run log,
// pending question and run id belong to one session, and nothing reset them
// when the editor opened another — a new empty project showed the previous
// project's "Fit c_… → cover". store.ts fires this whenever `sessionId`
// changes; the prompt store listens.
type SessionSwitchListener = (sid: string | null) => void
let _sessionSwitchListener: SessionSwitchListener | null = null
export function onSessionSwitch(listener: SessionSwitchListener | null): void { _sessionSwitchListener = listener }
export function fireSessionSwitch(sid: string | null): void { _sessionSwitchListener?.(sid) }

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
