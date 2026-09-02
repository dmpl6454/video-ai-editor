// /api/tools schema → form fields → dispatch args. Pure, so the derivation
// rules and the arg coercion are unit-tested against literal schema fixtures
// (schemaForm.test.ts) instead of discovered one 422 at a time.
//
// The backend validates types but does NOT enforce `required` (CLAUDE.md), so
// the two things buildArgs must never do are send "" for an omitted optional
// (it would be stored as the value) and send NaN for a blank number.

import type { JsonSchemaProp, ToolSchema } from '../api'
import type { CatalogEntry, FieldOverride, Widget } from './aiCatalog'

export interface Field {
  name: string; label: string; widget: Widget; required: boolean; default: unknown
  options?: (string | number)[]; integer?: boolean; min?: number; max?: number
  description?: string; defaultFrom?: FieldOverride['defaultFrom']
  nullable?: { label: string }; clipFilter?: 'overlay' | 'video'; accept?: string
}

export interface FormContext {
  playhead: number; inMark: number | null; outMark: number | null
  captionTargetPref: string | null; captionSpeedPref: string | null
}

// Schema names that are filesystem paths, whatever their declared type says.
const PATH_NAMES = new Set(['path', 'reference', 'bin', 'end_card'])

// ["string","null"] / ["number","null"] → the base type; nullability is a
// deliberate UI decision (FieldOverride.nullable), not something inferred.
function baseType(t: JsonSchemaProp['type']): string | undefined {
  return Array.isArray(t) ? t.find((x) => x !== 'null') : t
}

function humanize(name: string): string {
  const s = name.replace(/_/g, ' ')
  return s.charAt(0).toUpperCase() + s.slice(1)
}

function derivedWidget(name: string, prop: JsonSchemaProp, type: string | undefined): Widget {
  if (prop.enum) return 'select'
  if (type === 'array') return name === 'bbox' && prop.items?.type === 'number' ? 'bbox' : 'list'
  if (type === 'object') return 'mapping'
  if (PATH_NAMES.has(name)) return 'path'
  if (type === 'integer' || type === 'number') return 'number'
  if (type === 'boolean') return 'checkbox'
  return 'text'
}

function toField(name: string, prop: JsonSchemaProp | undefined, required: boolean, ov?: FieldOverride): Field {
  const type = baseType(prop?.type)
  const widget = ov?.widget ?? (prop ? derivedWidget(name, prop, type) : 'text')
  const options = ov?.options ?? (prop?.enum as (string | number)[] | undefined)
  return {
    name, widget, required,
    label: ov?.label ?? humanize(name),
    default: ov?.default !== undefined ? ov.default : prop?.default,
    ...(options ? { options } : {}),
    ...(type === 'integer' ? { integer: true } : {}),
    ...(prop?.minimum !== undefined ? { min: prop.minimum } : {}),
    ...(prop?.maximum !== undefined ? { max: prop.maximum } : {}),
    ...(prop?.description ? { description: prop.description } : {}),
    ...(ov?.defaultFrom ? { defaultFrom: ov.defaultFrom } : {}),
    ...(ov?.nullable ? { nullable: ov.nullable } : {}),
    ...(ov?.clipFilter ? { clipFilter: ov.clipFilter } : {}),
    ...(ov?.accept ? { accept: ov.accept } : {}),
  }
}

export function fieldsFor(schema: ToolSchema, entry: CatalogEntry): Field[] {
  const props = schema.input_schema.properties ?? {}
  const required = new Set(schema.input_schema.required ?? [])
  const hidden = new Set(entry.hide ?? [])
  const names = Object.keys(props)
  const ordered = entry.order
    ? [...entry.order.filter((n) => names.includes(n)), ...names.filter((n) => !entry.order!.includes(n))]
    : names
  const fields: Field[] = []
  for (const name of ordered) {
    const ov = entry.fields?.[name]
    if (hidden.has(name) || ov?.hidden) continue
    fields.push(toField(name, props[name], required.has(name), ov))
  }
  // Handler-only args the schema doesn't advertise (auto_reframe.subject_track).
  for (const [name, ov] of Object.entries(entry.fields ?? {})) {
    if (name in props || ov.hidden || hidden.has(name)) continue
    fields.push(toField(name, undefined, false, ov))
  }
  return fields
}

const round3 = (n: number) => Math.round(n * 1000) / 1000

function contextDefault(from: NonNullable<FieldOverride['defaultFrom']>, ctx: FormContext): unknown {
  switch (from) {
    case 'playhead': return round3(ctx.playhead)
    case 'playheadPlus3': return round3(ctx.playhead + 3)
    case 'inMark': return ctx.inMark ?? undefined
    case 'outMark': return ctx.outMark ?? undefined
    case 'captionTargetPref': return ctx.captionTargetPref ?? undefined
    case 'captionSpeedPref': return ctx.captionSpeedPref ?? undefined
  }
}

function blankFor(widget: Widget): unknown {
  if (widget === 'checkbox') return false
  if (widget === 'bbox') return ['', '', '', '']
  return ''
}

// A <select> can't show "nothing": with no schema default the browser paints
// the first option, so a required select's STATE must start there too
// (apply_template.name, apply_export_preset.name have an enum and no default).
// Seeding '' instead made Run fail with "Required" under a control that
// visibly showed a choice, and the first option could only be submitted by
// picking another one and switching back. Optional selects keep '' — the form
// renders a blank "—" option for those.
function selectFallback(f: Field): unknown {
  if (f.default !== undefined && f.default !== '') return f.default
  return f.required && f.options?.length ? f.options[0] : ''
}

function seedValue(f: Field, ctx: FormContext): unknown {
  const fromCtx = f.defaultFrom ? contextDefault(f.defaultFrom, ctx) : undefined
  if (f.widget === 'select') {
    const v = fromCtx ?? f.default
    // A stale preference that is no longer one of the choices must not become
    // a <select> value the user can't see — fall back to the schema default.
    const valid = v !== undefined && v !== '' && (!f.options || f.options.includes(v as string | number))
    return valid ? v : selectFallback(f)
  }
  return fromCtx ?? f.default ?? blankFor(f.widget)
}

export function initialValues(fields: Field[], ctx: FormContext): Record<string, unknown> {
  return Object.fromEntries(fields.map((f) => [f.name, seedValue(f, ctx)]))
}

// The context sources that move while a form sits open. A card seeds its
// values once (on first expand), so "Set In/Out marks first" used to stay on
// screen after the user did exactly that: the blank start/end were read only
// inside the seed. Re-applying the seed to the fields the user has NOT edited
// keeps them following the marks / playhead until they are touched. Returns
// the SAME object when nothing changed so React can skip the re-render.
const LIVE_SOURCES = new Set<FieldOverride['defaultFrom']>(['playhead', 'playheadPlus3', 'inMark', 'outMark'])

export function reseedContextValues(
  fields: Field[], values: Record<string, unknown>, touched: ReadonlySet<string>, ctx: FormContext,
): Record<string, unknown> {
  let out = values
  for (const f of fields) {
    if (!f.defaultFrom || !LIVE_SOURCES.has(f.defaultFrom) || touched.has(f.name)) continue
    const v = seedValue(f, ctx)
    if (!Object.is(out[f.name], v)) out = { ...out, [f.name]: v }
  }
  return out
}

type Conv = { value: unknown } | { error: string }
const OMIT: Conv = { value: undefined }

function isBlankScalar(v: unknown): boolean {
  return v === undefined || v === null || (typeof v === 'string' && v.trim() === '')
}

function isBlank(v: unknown): boolean {
  if (Array.isArray(v)) return v.every(isBlankScalar)
  return isBlankScalar(v)
}

function convertNumber(f: Field, v: unknown): Conv {
  const n = typeof v === 'number' ? v : Number(String(v).trim())
  if (!Number.isFinite(n)) return { error: 'Must be a number' }
  const r = f.integer ? Math.round(n) : n
  if (f.min !== undefined && r < f.min) return { error: `Minimum ${f.min}` }
  if (f.max !== undefined && r > f.max) return { error: `Maximum ${f.max}` }
  return { value: r }
}

// A <select> hands back strings; a numeric enum (upscale.factor: [2, 4]) must
// go back over the wire as the number the schema declares.
function convertOption(f: Field, v: unknown): Conv {
  const numeric = !!f.options?.length && f.options.every((o) => typeof o === 'number')
  return { value: numeric ? Number(v) : String(v) }
}

function convertList(f: Field, v: unknown): Conv {
  const raw = Array.isArray(v) ? v.map(String) : String(v).split(/[,\n]/)
  const items = raw.map((s) => s.trim()).filter(Boolean)
  if (!items.length) return f.required ? { error: 'Required' } : OMIT
  return { value: items }
}

// Mirrors dispatch.py::_norm_bbox, which hard-400s anything outside 0..1: a
// pixel box once OOM-killed the whole app, so the form refuses it first.
function convertBbox(v: unknown): Conv {
  const raw = Array.isArray(v) ? v : String(v).split(/[,\s]+/)
  if (raw.length !== 4) return { error: 'Needs four numbers: x, y, w, h' }
  const nums = raw.map((x) => (typeof x === 'number' ? x : Number(String(x).trim())))
  if (!nums.every(Number.isFinite)) return { error: 'Needs four numbers: x, y, w, h' }
  if (nums.some((n) => n < 0 || n > 1)) return { error: 'Values are fractions of the frame (0..1)' }
  const [x, y, w, h] = nums
  if (x + w > 1 + 1e-9 || y + h > 1 + 1e-9) return { error: 'Box runs off the frame (x+w and y+h must be ≤ 1)' }
  return { value: nums }
}

function convertMapping(v: unknown): Conv {
  if (v && typeof v === 'object' && !Array.isArray(v)) return { value: v }
  const out: Record<string, string> = {}
  for (const line of String(v).split('\n').map((s) => s.trim()).filter(Boolean)) {
    const eq = line.indexOf('=')
    if (eq < 1) return { error: `“${line}” needs the form KEY=VALUE` }
    out[line.slice(0, eq).trim()] = line.slice(eq + 1).trim()
  }
  return { value: out }
}

function convert(f: Field, v: unknown): Conv {
  if (f.nullable && v === null) return { value: null }
  if (isBlank(v)) return f.required ? { error: 'Required' } : OMIT
  switch (f.widget) {
    case 'number': case 'time': return convertNumber(f, v)
    case 'checkbox': return { value: !!v }
    case 'select': return convertOption(f, v)
    case 'list': return convertList(f, v)
    case 'bbox': return convertBbox(v)
    case 'mapping': return convertMapping(v)
    case 'file': return { value: v }   // a File until the panel swaps in the uploaded path
    default: return { value: String(v).trim() }
  }
}

export function buildArgs(
  fields: Field[], values: Record<string, unknown>,
): { args: Record<string, unknown>; errors: Record<string, string> } {
  const args: Record<string, unknown> = {}
  const errors: Record<string, string> = {}
  for (const f of fields) {
    const r = convert(f, values[f.name])
    if ('error' in r) errors[f.name] = r.error
    else if (r.value !== undefined) args[f.name] = r.value
  }
  return { args, errors }
}
