// Editor Brain (EB1-F): the Plan tab's read-only model.
//
// A brain run's preview card carries `preview.brain` (agent/prompt/brain_card.py):
// the EDP summary, the decisions with their reasons, "Not done this time",
// the rung line and a why per Changes line. This file turns that payload
// into what the tab shows — decisions grouped by kind in an editor's order,
// seek labels on the ruler's SMPTE grid (lib/timecode, the one frame rule),
// the summary and deferred lines — and parses the wire shape tolerantly (an
// older or partial backend must never break the card).

import { formatTimecode } from './timecode'

const DID_RE = /^d_[0-9a-f]{8}$/

export interface BrainRef { src: string; t0: number; t1: number }
export interface BrainDecision {
  id: string
  kind: string
  code: string
  text: string
  optional: boolean
  by: string | null
  /** true = in the diff; false = dropped or an over-claim (never shown as done); null = a kept pause. */
  applied: boolean | null
  ref: BrainRef | null
  note?: string | null
  /** Where the decision's source second plays in the CURRENT timeline (the seek button), or null. */
  timelineT: number | null
}
export interface BrainHook { quote: string; src: string; t0: number; t1: number; timelineT: number | null }
export interface BrainSummary {
  projectType: string
  target: string
  durationS: number | null
  hook: BrainHook | null
  camera: { angles: number; switches: number; atCut: number }
  pausesKept: number
  music: { bed: string | null; shape: string } | null
  captions: { mode: string; style: string; position: string } | null
  dialogue: { src: string; lane: string; seams: number } | null
  /** The length the run leaves, seconds (null from an older backend). */
  resultS: number | null
}
export interface BrainDeferred { asked: string; why: string }
export interface BrainCardInfo {
  decisionsId: string
  tabDefault: 'plan' | 'changes'
  rungs: { brain: string; contentBrain: string | null; line: string }
  summary: BrainSummary
  decisions: BrainDecision[]
  deferred: BrainDeferred[]
  /** One per Changes line (lines then hidden), the decision's reason or null. */
  whys: (string | null)[]
  /** Indices of lines that are a grouped tally ("Removed 14 stretches: …"). */
  grouped: number[]
  /** Indices of lines no decision explains (the card says so, never a made-up why). */
  unexplained: number[]
}

/** Ids and graph keys are the machine's: a person never reads `src_55e24f…`,
 *  `k_0003` or a `.normalized` copy's name, wherever the text came from. */
const ID_RE = /\b(?:src|[cdkgwsp])_[0-9a-f]{4,}(?:_[0-9a-f]{4,})*\b/g
export function cleanText(text: string): string {
  return text
    .replace(/\.normalized(?=\.)/g, '')
    .replace(/\bsrc_[0-9a-f]{6,}\b/g, 'the footage')
    .replace(ID_RE, '')
    .replace(/\s*\(\s*(?:,\s*)*\)/g, '')
    .replace(/\s{2,}/g, ' ')
    .replace(/\s+([,.;:])/g, '$1')
    .trim()
}

/** The order an editor reads a plan: what opens it, what was cut and kept,
 *  the camera, the punch-ins, then the finishing passes. */
export const KIND_ORDER: readonly string[] = [
  'open_on', 'keep_window', 'cut_range', 'keep_pause', 'switch_angle', 'jump_cut_hide', 'punch_in',
  'captions', 'music', 'dialogue', 'reframe', 'export_preset',
]
export const KIND_LABELS: Record<string, string> = {
  open_on: 'Opening', keep_window: 'Kept', cut_range: 'Cuts', keep_pause: 'Pauses kept', switch_angle: 'Camera',
  jump_cut_hide: 'Hidden jump cuts', punch_in: 'Punch-ins', captions: 'Captions', music: 'Music',
  dialogue: 'Dialogue', reframe: 'Reframe', export_preset: 'Export',
}
const TYPE_WORDS: Record<string, string> = {
  talking_head: 'Talking head', podcast: 'Podcast', interview: 'Interview', reel: 'Reel',
}
/** Reason-code words for the tally, as agent/prompt/brain_card.CODE_WORDS. */
const CODE_WORDS: Record<string, [string, string]> = {
  silence: ['silence', 'silences'], filler: ['filler', 'fillers'], filler_acoustic: ['filler', 'fillers'],
  soft_filler: ['filler', 'fillers'], false_start: ['false start', 'false starts'],
  dead_air: ['stretch of dead air', 'stretches of dead air'], repeat: ['repeat', 'repeats'],
  weak_question: ['weak question', 'weak questions'], dead_conversation: ['low-content stretch', 'low-content stretches'],
  technical: ['technical stretch', 'technical stretches'], duration_fit: ['trim to fit the length', 'trims to fit the length'],
  best_window: ['trim to the best window', 'trims to the best window'],
}

export interface DecisionGroup { kind: string; label: string; items: BrainDecision[]; dropped: number }

/** A label for a kind the table does not know: "lower_third" → "Lower third". */
export function kindLabel(kind: string): string {
  const k = KIND_LABELS[kind]
  if (k) return k
  const s = kind.replace(/_/g, ' ').trim()
  return s.charAt(0).toUpperCase() + s.slice(1)
}

/** Decisions by kind, kinds in KIND_ORDER (unknown kinds after, first seen
 *  first), items in plan order. `dropped` counts what was not applied. */
export function groupDecisions(decisions: readonly BrainDecision[]): DecisionGroup[] {
  const by = new Map<string, BrainDecision[]>()
  for (const d of decisions) {
    const list = by.get(d.kind) ?? []
    list.push(d)
    by.set(d.kind, list)
  }
  const rank = (k: string) => { const i = KIND_ORDER.indexOf(k); return i < 0 ? KIND_ORDER.length : i }
  const kinds = [...by.keys()].sort((a, b) => rank(a) - rank(b))
  return kinds.map((kind) => {
    const items = by.get(kind)!
    return { kind, label: kindLabel(kind), items, dropped: items.filter((d) => d.applied === false).length }
  })
}

/** "Seek to 00:00:12:03" — the ruler's own clock (drop-frame where the rate needs it). */
export function seekLabel(t: number, fps: unknown): string {
  return `Seek to ${formatTimecode(t, fps)}`
}

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`

/** "1 silence, 1 filler, 1 false start" — the grouped card line's tally. */
export function tallyLine(decisions: readonly BrainDecision[]): string {
  const counts = new Map<string, [string, string, number]>()
  for (const d of decisions) {
    const base = d.code.split(':', 1)[0]
    const w = CODE_WORDS[base] ?? [base.replace(/_/g, ' '), `${base.replace(/_/g, ' ')}s`]
    const cur = counts.get(w[0])
    if (cur) cur[2] += 1
    else counts.set(w[0], [w[0], w[1], 1])
  }
  return [...counts.values()].map(([one, many, n]) => plural(n, one, many)).join(', ')
}

/** "Talking head → Reel, 44.7 s: 3 cuts, 1 pause kept, 1 angle change, captions, chill bed". */
export function summaryLine(info: BrainCardInfo): string {
  const s = info.summary
  const applied = (kind: string) => info.decisions.filter((d) => d.kind === kind && d.applied !== false).length
  const parts: string[] = []
  const cuts = applied('cut_range')
  if (cuts) parts.push(plural(cuts, 'cut', 'cuts'))
  const pauses = Math.max(s.pausesKept, info.decisions.filter((d) => d.kind === 'keep_pause').length)
  if (pauses) parts.push(plural(pauses, 'pause kept', 'pauses kept'))
  const switches = Math.max(s.camera.switches, applied('switch_angle'))
  if (switches) parts.push(plural(switches, 'angle change', 'angle changes'))
  const punches = applied('punch_in')
  if (punches) parts.push(plural(punches, 'punch-in', 'punch-ins'))
  if (s.captions) parts.push('captions')
  if (s.music?.bed) parts.push(`${cleanText(s.music.bed)} bed`)
  // Never the source: in a payload that was not resolved it is a graph key (src_…), and a file name adds nothing here.
  if (s.dialogue) parts.push('dialogue on its own audio lane')
  const type = TYPE_WORDS[s.projectType] ?? kindLabel(s.projectType)
  const length = s.resultS !== null && s.resultS > 0 ? `, ${s.resultS.toFixed(1)} s` : ''
  return `${type} → ${s.target}${length}${parts.length ? `: ${parts.join(', ')}` : ''}`
}

/** "per-word caption highlight — next wave", one per deferred item. */
export function deferredLines(info: BrainCardInfo): string[] {
  return info.deferred.map((d) => cleanText(d.why ? `${d.asked} — ${d.why}` : d.asked))
}

// ---------------------------------------------------------------- the wire

const str = (v: unknown, d = ''): string => (typeof v === 'string' ? v : d)
const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)
const int = (v: unknown, d = 0): number => (typeof v === 'number' && Number.isFinite(v) ? Math.trunc(v) : d)
const obj = (v: unknown): Record<string, unknown> | null => (v && typeof v === 'object' && !Array.isArray(v)
  ? (v as Record<string, unknown>) : null)

function ref(raw: unknown): BrainRef | null {
  const r = obj(raw)
  if (!r) return null
  const t0 = num(r.t0), t1 = num(r.t1)
  return t0 === null || t1 === null ? null : { src: str(r.src), t0, t1 }
}

function decision(raw: unknown): BrainDecision | null {
  const r = obj(raw)
  if (!r || typeof r.id !== 'string' || typeof r.kind !== 'string') return null
  const applied = r.applied === true ? true : r.applied === false ? false : null
  return {
    id: r.id, kind: r.kind, code: str(r.code), text: str(r.text), optional: r.optional === true,
    by: typeof r.by === 'string' ? r.by : null, applied, ref: ref(r.ref),
    note: typeof r.note === 'string' ? r.note : null, timelineT: num(r.timeline_t),
  }
}

function summary(raw: unknown): BrainSummary {
  const r = obj(raw) ?? {}
  const hook = obj(r.hook)
  const cam = obj(r.camera) ?? {}
  const music = obj(r.music)
  const caps = obj(r.captions)
  const dia = obj(r.dialogue)
  return {
    projectType: str(r.project_type, 'edit'), target: str(r.target, 'Edit'), durationS: num(r.duration_s),
    hook: hook ? { quote: str(hook.quote), src: str(hook.src), t0: num(hook.t0) ?? 0, t1: num(hook.t1) ?? 0,
                   timelineT: num(hook.timeline_t) } : null,
    camera: { angles: int(cam.angles, 1), switches: int(cam.switches), atCut: int(cam.at_cut) },
    pausesKept: int(r.pauses_kept),
    music: music ? { bed: typeof music.bed === 'string' ? music.bed : null, shape: str(music.shape, 'bed') } : null,
    captions: caps ? { mode: str(caps.mode), style: str(caps.style), position: str(caps.position, 'bottom') } : null,
    dialogue: dia ? { src: str(dia.src), lane: str(dia.lane, 'a1'), seams: int(dia.seams) } : null,
    resultS: num(r.result_s),
  }
}

/** The wire `preview.brain` → BrainCardInfo, or null when it is not one. */
export function normalizeBrainCard(raw: unknown): BrainCardInfo | null {
  const r = obj(raw)
  if (!r || typeof r.decisions_id !== 'string' || !DID_RE.test(r.decisions_id) || !Array.isArray(r.decisions)) {
    return null
  }
  const rungs = obj(r.rungs) ?? {}
  const decisions = r.decisions.map(decision).filter((d): d is BrainDecision => d !== null)
  const deferred = (Array.isArray(r.deferred) ? r.deferred : [])
    .map(obj).filter((d): d is Record<string, unknown> => !!d && typeof d.asked === 'string')
    .map((d) => ({ asked: d.asked as string, why: str(d.why) }))
  const ints = (v: unknown) => (Array.isArray(v) ? v.filter((x): x is number => typeof x === 'number') : [])
  return {
    decisionsId: r.decisions_id,
    tabDefault: r.tab_default === 'changes' ? 'changes' : 'plan',
    rungs: { brain: str(rungs.brain, 'recipes'), contentBrain: typeof rungs.content_brain === 'string' ? rungs.content_brain : null,
             line: str(rungs.line) },
    summary: summary(r.summary),
    decisions,
    deferred,
    whys: Array.isArray(r.whys) ? r.whys.map((w) => (typeof w === 'string' && w ? cleanText(w) : null)) : [],
    grouped: ints(r.grouped),
    unexplained: ints(r.unexplained),
  }
}
