// Tool result → something a card can render. Keyed by tool name because the
// handlers return different shapes (dispatch.py: find_moments → {matches},
// make_shorts → {shorts}, diarize → {turns}, audit → {issues}…) and a generic
// "here's the JSON" view loses the one thing the user wants from a search:
// a button that takes them to the moment.
//
// Every reader is defensive: a handler's shape can drift, and a result view
// that throws would take the whole panel down with it (AiPanel is not inside
// an error boundary). resultView must return a raw-JSON view for ANY input.

import { baseName } from './paths'

export type ResultRow =
  | { kind: 'range'; start: number; end: number; text: string; score?: number }
  | { kind: 'text'; text: string }
  | { kind: 'path'; path: string; text: string }
  | { kind: 'issue'; level: 'error' | 'warn' | 'info'; text: string }
  | { kind: 'speaker'; speaker: string; start: number; end: number }

export interface ResultView { headline: string; rows: ResultRow[]; note?: string; raw?: unknown }

type Rec = Record<string, unknown>

const isRec = (v: unknown): v is Rec => !!v && typeof v === 'object' && !Array.isArray(v)
const recs = (v: unknown): Rec[] => (Array.isArray(v) ? v.filter(isRec) : [])
const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)
const str = (v: unknown): string => (typeof v === 'string' ? v : '')
const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`

function rangeRow(r: Rec, text: string): ResultRow | null {
  const start = num(r.start)
  const end = num(r.end)
  if (start === null || end === null) return null
  const score = num(r.score)
  return { kind: 'range', start, end, text, ...(score !== null ? { score } : {}) }
}

function findMoments(r: Rec): ResultView {
  const rows = recs(r.matches).flatMap((m) => {
    // ai/vision.py emits two shapes: {description} for vision-ranked hits and
    // {transcript, shot_description} for transcript-ranked ones.
    const text = [str(m.transcript), str(m.shot_description) || str(m.description)]
      .filter(Boolean).join(' — ')
    const row = rangeRow(m, text)
    return row ? [row] : []
  })
  const query = str(r.query)
  const headline = rows.length
    ? `${plural(rows.length, 'moment')}${query ? ` for “${query}”` : ''}`
    : str(r.summary) || 'No moments found'
  return { headline, rows, raw: r }
}

function makeShorts(r: Rec): ResultView {
  const rows = recs(r.shorts).flatMap((s) => {
    const row = rangeRow(s, str(s.why))
    return row ? [row] : []
  })
  const sessions = Array.isArray(r.new_sessions) ? r.new_sessions.length : 0
  return {
    headline: str(r.summary) || plural(rows.length, 'short'),
    rows,
    ...(sessions ? { note: `Saved ${plural(sessions, 'session')} — open them from the session picker` } : {}),
    raw: r,
  }
}

function searchMedia(r: Rec): ResultView {
  const rows: ResultRow[] = []
  const notes: string[] = []
  const spoken = isRec(r.spoken) ? r.spoken : null
  const visual = isRec(r.visual) ? r.visual : null
  for (const hit of recs(spoken?.results)) {
    const row = rangeRow(hit, str(hit.text))
    if (row) rows.push(row)
  }
  for (const hit of recs(visual?.results)) {
    // ai/clip_search.search: {clip_id, score, time, src_name}; older/other
    // producers may carry start/end or a bare path — take what is there.
    const name = str(hit.src_name) || str(hit.id) || str(hit.clip_id)
    const ranged = rangeRow(hit, name)
    const t = num(hit.time)
    if (ranged) rows.push(ranged)
    else if (t !== null) rows.push({ kind: 'range', start: t, end: t, text: `${name} @ ${t}s`,
                                     ...(num(hit.score) !== null ? { score: num(hit.score)! } : {}) })
    else if (str(hit.path)) rows.push({ kind: 'path', path: str(hit.path), text: baseName(str(hit.path)) })
  }
  if (visual?.status === 'unavailable') notes.push(str(visual.message) || 'Visual search is unavailable')
  const query = str(r.query)
  return {
    headline: `${plural(rows.length, 'result')}${query ? ` for “${query}”` : ''}`,
    rows,
    ...(notes.length ? { note: notes.join(' · ') } : {}),
    raw: r,
  }
}

function generateHook(r: Rec): ResultView {
  const rows: ResultRow[] = (Array.isArray(r.candidates) ? r.candidates : [])
    .filter((c): c is string => typeof c === 'string' && c.trim() !== '')
    .map((text) => ({ kind: 'text', text }))
  return {
    headline: plural(rows.length, 'hook idea'),
    rows,
    ...(str(r.source) ? { note: `Source: ${str(r.source)}` } : {}),
    raw: r,
  }
}

function findBroll(r: Rec): ResultView {
  const rows: ResultRow[] = recs(r.candidates).flatMap((c) => {
    const path = str(c.path)
    if (!path) return []
    const dur = num(c.duration)
    return [{ kind: 'path' as const, path, text: `${baseName(path)}${dur !== null ? ` · ${dur.toFixed(1)}s` : ''}` }]
  })
  return { headline: str(r.summary) || plural(rows.length, 'candidate'), rows, raw: r }
}

function diarize(r: Rec): ResultView {
  const rows: ResultRow[] = recs(r.turns).flatMap((t) => {
    const start = num(t.start)
    const end = num(t.end)
    if (start === null || end === null) return []
    return [{ kind: 'speaker' as const, speaker: str(t.speaker) || '?', start, end }]
  })
  const speakers = Array.isArray(r.speakers) ? r.speakers.length : 0
  return { headline: str(r.summary) || `${plural(rows.length, 'turn')}, ${plural(speakers, 'speaker')}`, rows, raw: r }
}

function auditAesthetic(r: Rec): ResultView {
  const score = num(r.score)
  const rows: ResultRow[] = recs(r.issues).map((i) => {
    const level = i.level === 'error' ? 'error' : i.level === 'warn' ? 'warn' : 'info'
    return { kind: 'issue', level, text: str(i.message) || str(i.key) || 'issue' }
  })
  const hook = isRec(r.hook) ? num(r.hook.hook_score) : null
  return {
    headline: score !== null ? `Score ${score}/100` : 'Style audit',
    rows,
    ...(hook !== null ? { note: `Hook stack ${hook}/3` } : {}),
    raw: r,
  }
}

function matchStyle(r: Rec): ResultView {
  const ref = str(r.reference)
  return { headline: ref ? `Style fingerprint of ${baseName(ref)}` : 'Style fingerprint', rows: [], raw: r }
}

const VIEWS: Record<string, (r: Rec) => ResultView> = {
  find_moments: findMoments,
  make_shorts: makeShorts,
  search_media: searchMedia,
  generate_hook: generateHook,
  find_broll: findBroll,
  diarize,
  audit_aesthetic: auditAesthetic,
  match_style: matchStyle,
}

export function resultView(tool: string, result: unknown, label = tool): ResultView {
  const fallback = `${label} done`
  if (!isRec(result)) return { headline: fallback, rows: [], raw: result }
  const view = VIEWS[tool]
  if (view) return view(result)
  return { headline: str(result.summary) || fallback, rows: [], raw: result }
}
