// Answers for a `clarify` card — pre-filled from the plan's own defaults,
// coerced per question kind, and checked before `POST …/prompt/answer`.
//
// The zero-question rule (spec §2.7) means a card is rare; when it does
// appear the defaults are already the planner's best guess, so the card opens
// with every default selected and Enter runs it unchanged. Nothing here talks
// to the network: `answersPayload` is what the store posts, and the tests
// drive it from the recorded stream's `clarify` event.

import type { NeedsInput, NeedsInputOption } from './promptEvents'

export type Answers = Record<string, unknown>

// `kind:"confirm"` is a two-option choice whose values are "yes"/"no" (spec
// §1.1); the wire may omit the options, so the card supplies them.
export const CONFIRM_OPTIONS: NeedsInputOption[] = [
  { value: 'yes', label: 'Yes', synonyms: ['go', 'download', 'ok', 'haan'] },
  { value: 'no', label: 'No', synonyms: ['skip', 'cancel', 'nahi'] },
]

export const kindOf = (q: NeedsInput) => q.kind ?? 'choice'

/** The selectable options for a question (confirm gets yes/no when absent). */
export function optionsFor(q: NeedsInput): NeedsInputOption[] {
  if (q.options && q.options.length) return q.options
  return kindOf(q) === 'confirm' ? CONFIRM_OPTIONS : []
}

/** A gate: a `confirm` (`downloads`, `go`) or a feature gate (`gate_<tool>`). */
export const isGate = (q: NeedsInput) => kindOf(q) === 'confirm' || q.key.startsWith('gate_')

// What the backend reads as "end the run" for the `go` gate: planner._NO on
// `go` returns the empty "Cancelled — the timeline is unchanged" plan. Kept
// as values, not labels, because the label is the planner's ("Cancel" on
// the wire, "No" when the options are absent) and may be localised.
const GO_NO_VALUES = new Set<unknown>(['no', 'n', 'false', 'skip', 'cancel', 'abort', 'nahi', false, 0])

/**
 * The option that ENDS THE RUN, when the question offers one — the card's
 * Esc target. Only two questions carry one (planner.apply_answers /
 * pending.apply_answers): `go` answered no, and any choice answered `abort`
 * (the feature gates' "Stop"). The look-alikes are deliberately NOT aborts:
 * the `downloads` gate's "Skip" drops the download and the run continues,
 * and a feature gate's "skip" continues without that tool — so those cards
 * keep the card's own Cancel (drop the question) as a separate outcome.
 *
 * WHY: the long-run gate used to render its "Cancel" option next to the
 * card's "Cancel Esc" — two Cancels side by side with the same effect.
 */
export function abortOption(q: NeedsInput): NeedsInputOption | null {
  const opts = optionsFor(q)
  const norm = (v: unknown) => (typeof v === 'string' ? v.trim().toLowerCase() : v)
  if (q.key === 'go') return opts.find((o) => GO_NO_VALUES.has(norm(o.value))) ?? null
  return opts.find((o) => norm(o.value) === 'abort') ?? null
}

export interface EscapeTarget { question: NeedsInput; option: NeedsInputOption }

/**
 * What Esc does on a card showing `questions`: the first run-ending option
 * among them (Esc picks it and submits, the option carries the Esc hint) or
 * `null`, in which case the card renders its own Cancel that drops the
 * question. Never both.
 */
export function escapeTarget(questions: NeedsInput[]): EscapeTarget | null {
  for (const question of questions) {
    const option = abortOption(question)
    if (option) return { question, option }
  }
  return null
}

/**
 * The questions a card shows at once: when the FIRST is a gate it stands
 * alone (the backend re-pauses with what is left after it is answered), so a
 * 3 GB download confirm and an "auto captions is unavailable" gate never
 * share one card and contradict each other; otherwise all of them.
 */
export function visibleQuestions(questions: NeedsInput[]): NeedsInput[] {
  const first = questions[0]
  return first && isGate(first) && questions.length > 1 ? [first] : questions
}

const hasValue = (v: unknown) => v !== null && v !== undefined && !(typeof v === 'string' && v.trim() === '')

/** Every question that carries a default, pre-answered with it. */
export function defaultAnswers(questions: NeedsInput[]): Answers {
  const out: Answers = {}
  for (const q of questions) {
    if (hasValue(q.default)) out[q.key] = q.default
  }
  return out
}

/** "30s" / "1.5m" / "1:30" / "45" → seconds; null when unreadable. */
export function parseDuration(text: string): number | null {
  const t = text.trim().toLowerCase()
  if (!t) return null
  const clock = /^(\d+):(\d{1,2})$/.exec(t)
  if (clock) return Number(clock[1]) * 60 + Number(clock[2])
  const m = /^(\d+(?:\.\d+)?)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes)?$/.exec(t)
  if (!m) return null
  const n = Number(m[1])
  if (!Number.isFinite(n)) return null
  return m[2] && m[2].startsWith('m') ? n * 60 : n
}

/**
 * Turn what the user typed or picked into the value the planner expects for
 * this question's kind. `null` means "not an answer" (an unparsable number,
 * an empty text) so the card can keep the default and say why.
 */
export function coerceAnswer(q: NeedsInput, raw: unknown): unknown {
  const kind = kindOf(q)
  if (kind === 'number') {
    const n = typeof raw === 'number' ? raw : Number(String(raw ?? '').trim())
    if (!Number.isFinite(n)) return null
    return clamp(n, q.min, q.max)
  }
  if (kind === 'duration') {
    const n = typeof raw === 'number' ? raw : parseDuration(String(raw ?? ''))
    if (n === null || !Number.isFinite(n)) return null
    return clamp(n, q.min, q.max)
  }
  if (kind === 'text' || kind === 'path') {
    const s = String(raw ?? '').trim()
    return s ? s : null
  }
  // choice / confirm: must be one of the offered values (or a label/synonym).
  const opts = optionsFor(q)
  if (!opts.length) return hasValue(raw) ? raw : null
  const hit = opts.find((o) => Object.is(o.value, raw) || String(o.value) === String(raw))
  if (hit) return hit.value
  const s = String(raw ?? '').trim().toLowerCase()
  const byText = opts.find((o) => o.label.toLowerCase() === s || (o.synonyms ?? []).some((x) => x.toLowerCase() === s))
  return byText ? byText.value : null
}

function clamp(n: number, min?: number, max?: number): number {
  let v = n
  if (typeof min === 'number') v = Math.max(min, v)
  if (typeof max === 'number') v = Math.min(max, v)
  return v
}

/** The answer to use for `q`: what was given, else the plan's own default. */
const effective = (q: NeedsInput, answers: Answers): unknown =>
  hasValue(answers[q.key]) ? answers[q.key] : q.default

/**
 * Required questions with no usable answer yet — the card's blocking list.
 * A required question WITH a default never blocks (spec §1.1: a default
 * pre-fills and the executor runs immediately; only `required && default is
 * None` pauses), so the default counts as answered here too.
 */
export function missingRequired(questions: NeedsInput[], answers: Answers): string[] {
  return questions.filter((q) => q.required && !hasValue(effective(q, answers))).map((q) => q.key)
}

/**
 * The body for `POST …/prompt/answer`: only keys the plan asked for, each
 * coerced to its kind; unanswered optional keys are omitted so the planner
 * keeps its own default. Throws when a required answer is missing — the
 * card checks `missingRequired` first, so a throw here is a programming error.
 */
export function answersPayload(questions: NeedsInput[], answers: Answers): Answers {
  const missing = missingRequired(questions, answers)
  if (missing.length) throw new Error(`unanswered: ${missing.join(', ')}`)
  const out: Answers = {}
  for (const q of questions) {
    const given = effective(q, answers)
    if (!hasValue(given)) continue
    const v = coerceAnswer(q, given)
    if (v !== null) out[q.key] = v
  }
  return out
}

/** Sum of a plan's `downloads_needed[].bytes` for the confirm card headline. */
export function totalDownloadBytes(items: { bytes: number }[] | undefined): number {
  return (items ?? []).reduce((n, d) => n + (Number.isFinite(d.bytes) ? d.bytes : 0), 0)
}
