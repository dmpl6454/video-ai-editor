// The Prompt run log's verified checks, as sentences a person reads (QA-017).
//
// The verify table printed `measured` and `expected` through a formatter whose
// fallback was JSON.stringify, so a reframe check read
// `{"src_changed":true,"skipped":false,"all_cover":true}` and the safe-zone
// check `expected {"y_min":0.1,"y_max":0.78,…}`. Those strings were
// `white-space: nowrap` in two `auto` grid columns, which took the row's whole
// width and crushed the label column to one character: every label ran down
// the log one letter per line (10 px wide, up to 504 px tall), and the log
// squeezed the 9:16 preview to 128×228.
//
// Here every value becomes words — objects as "key: value" pairs with the
// snake_case keys spelled out, booleans as yes/no, numbers rounded — and a
// check becomes ONE line of prose under its label ("measured 9:16 · as
// expected"), which wraps like any other sentence.

import type { VerifyCheck } from './promptEvents'

const words = (key: string) => key.replace(/_/g, ' ').trim()

function num(n: number): string {
  if (Number.isInteger(n)) return String(n)
  return n.toFixed(Math.abs(n) < 10 ? 2 : 1)
}

/** A range the verifier sends as `{min, max, unit}` (max null = open). */
function rangeText(v: Record<string, unknown>): string | null {
  const keys = Object.keys(v)
  if (!keys.length || !keys.every((k) => k === 'min' || k === 'max' || k === 'unit')) return null
  const lo = typeof v.min === 'number' ? v.min : null
  const hi = typeof v.max === 'number' ? v.max : null
  if (lo === null && hi === null) return null
  const u = typeof v.unit === 'string' && v.unit ? ` ${v.unit}` : ''
  if (lo !== null && hi !== null) return `${num(lo)}–${num(hi)}${u}`
  return lo !== null ? `at least ${num(lo)}${u}` : `at most ${num(hi as number)}${u}`
}

/** One measured/expected value as plain words. Never JSON. */
export function humanValue(v: unknown, unit?: string): string {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'number') return unit ? `${num(v)} ${unit}` : num(v)
  if (typeof v === 'boolean') return v ? 'yes' : 'no'
  if (typeof v === 'string') return unit && /^-?\d+(\.\d+)?$/.test(v) ? `${v} ${unit}` : v
  if (Array.isArray(v)) {
    if (v.length === 0) return 'none'
    const parts = v.map((x) => humanValue(x))
    return unit && v.every((x) => typeof x === 'number') ? `${parts.join(', ')} ${unit}` : parts.join(', ')
  }
  if (typeof v === 'object') {
    const range = rangeText(v as Record<string, unknown>)
    if (range !== null) return range
    const entries = Object.entries(v as Record<string, unknown>)
    if (entries.length === 0) return '—'
    return entries.map(([k, x]) => {
      const inner = humanValue(x)
      const nested = typeof x === 'object' && x !== null && !Array.isArray(x)
        && rangeText(x as Record<string, unknown>) === null
      return nested ? `${words(k)} (${inner})` : `${words(k)}: ${inner}`
    }).join(', ')
  }
  return String(v)
}

const has = (v: unknown) => v !== null && v !== undefined

/**
 * The line under a check's label: what was measured against what was wanted,
 * or null when the check carries neither. A check that could not be measured
 * says so rather than printing a dash beside a JSON target.
 */
export function checkValues(c: VerifyCheck): string | null {
  const m = has(c.measured) ? humanValue(c.measured, c.unit) : null
  const e = has(c.expected) ? humanValue(c.expected, c.unit) : null
  if (m === null && e === null) return null
  if (m === null) return c.pass === null ? `not measured · target ${e}` : `expected ${e}`
  if (e === null) return `measured ${m}`
  if (m === e) return `${m} · as expected`
  return `measured ${m} · expected ${e}`
}

/** The one-line headline of a finished run, for the collapsed log. */
export function checksHeadline(verify: { passed: number; total: number; checks: VerifyCheck[] } | null): string | null {
  if (!verify) return null
  const failed = verify.checks.filter((c) => c.pass === false && c.headline !== false).length
  const base = `${verify.passed} of ${verify.total} check${verify.total === 1 ? '' : 's'} passed`
  return failed ? `${base} · ${failed} failed` : base
}

/** The checks a run log lists: headline ones first, then the info ones, each
 *  identical check (same id, words and result) once (review RD3: Auto edit
 *  sent "the video got shorter" twice, and the repeated React key threw). */
export function shownChecks(checks: readonly VerifyCheck[]): VerifyCheck[] {
  const seen = new Set<string>()
  const out: VerifyCheck[] = []
  for (const c of [...checks.filter((x) => x.headline !== false), ...checks.filter((x) => x.headline === false)]) {
    const k = `${c.check}\u0000${c.human}\u0000${String(c.pass)}`
    if (seen.has(k)) continue
    seen.add(k)
    out.push(c)
  }
  return out
}
