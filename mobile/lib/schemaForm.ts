/**
 * `/api/tools` schema → form fields → dispatch args.
 *
 * Ported from `frontend/src/lib/schemaForm.ts`, minus everything the phone
 * cannot do (no file pickers, no bounding boxes drawn on a preview, no
 * playhead or In/Out marks to seed a time field from — the phone's shared
 * store has none of those). What is left is the part that matters: the
 * derivation is pure, so the rules and the argument coercion are unit-tested
 * against literal schema fixtures instead of discovered one 422 at a time on
 * a device with no console.
 *
 * THE TWO RULES `buildArgs` MUST NEVER BREAK. The backend validates types but
 * does not enforce `required`, so:
 *
 *   - an omitted optional must be OMITTED, never sent as "". A `""` is stored
 *     as the value and a blank caption language becomes a caption language
 *     literally named "";
 *   - a blank number must be omitted, never sent as NaN. `JSON.stringify`
 *     turns NaN into `null`, and `null` for `min_dur` is a 500 on the Mac.
 *
 * WHY A SEPARATE `stepper` WIDGET. A bounded integer — `upscale.factor`,
 * `make_shorts.target_count` — is four taps on a phone and a keyboard dance in
 * a text field. Unbounded numbers stay text, because a stepper with no ceiling
 * is a control you cannot reach the end of.
 */

import type { CatalogEntry, FieldOverride, Widget } from "./catalog";
import type { JsonSchemaProp, ToolSchema } from "./types";

/**
 * A bounded integer only becomes a stepper while the range is small enough to
 * tap through. Beyond this it is a number field: twenty taps is the limit of
 * anyone's patience, and `threshold_db` at -60..0 would be sixty.
 */
export const STEPPER_MAX_RANGE = 20;

export interface Field {
  name: string;
  label: string;
  widget: Widget;
  required: boolean;
  default: unknown;
  options?: (string | number)[];
  integer?: boolean;
  min?: number;
  max?: number;
  /** The schema's own `description`, verbatim. */
  description?: string;
  /** The catalogue's addition — what the schema could not say. */
  help?: string;
  nullable?: { label: string };
  clipFilter?: "video" | "media";
}

// ---------------------------------------------------------------------------
// Derivation
// ---------------------------------------------------------------------------

/** `["string","null"]` → `"string"`. Nullability is a deliberate UI decision
 *  (`FieldOverride.nullable`), never inferred from the union. */
function baseType(t: JsonSchemaProp["type"]): string | undefined {
  return Array.isArray(t) ? t.find((x) => x !== "null") : t;
}

function humanize(name: string): string {
  const s = name.replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function numericWidget(prop: JsonSchemaProp): Widget {
  const { minimum, maximum } = prop;
  if (minimum === undefined || maximum === undefined) return "number";
  return maximum - minimum <= STEPPER_MAX_RANGE ? "stepper" : "number";
}

/**
 * The widget for a schema property, before any catalogue override.
 *
 * An unrecognised or missing `type` falls through to `text` rather than
 * throwing. A new argument type on the Mac should degrade to a text box the
 * user can still type into, not take the whole screen down.
 */
function derivedWidget(prop: JsonSchemaProp): Widget {
  const type = baseType(prop.type);
  if (prop.enum) return "select";
  if (type === "array") return "list";
  if (type === "object") return "mapping";
  if (type === "boolean") return "switch";
  if (type === "integer" || type === "number") return numericWidget(prop);
  return "text";
}

function toField(
  name: string,
  prop: JsonSchemaProp | undefined,
  required: boolean,
  ov?: FieldOverride,
): Field {
  const type = baseType(prop?.type);
  const widget = ov?.widget ?? (prop ? derivedWidget(prop) : "text");
  const options = ov?.options ?? (prop?.enum as (string | number)[] | undefined);
  return {
    name,
    widget,
    required,
    label: ov?.label ?? humanize(name),
    default: ov?.default !== undefined ? ov.default : prop?.default,
    ...(options ? { options } : {}),
    ...(type === "integer" ? { integer: true } : {}),
    ...(prop?.minimum !== undefined ? { min: prop.minimum } : {}),
    ...(prop?.maximum !== undefined ? { max: prop.maximum } : {}),
    ...(prop?.description ? { description: prop.description } : {}),
    ...(ov?.help ? { help: ov.help } : {}),
    ...(ov?.nullable ? { nullable: ov.nullable } : {}),
    ...(ov?.clipFilter ? { clipFilter: ov.clipFilter } : {}),
  };
}

/**
 * The fields to render, in the order to render them.
 *
 * Two things happen here that a naive `Object.entries(properties)` would miss:
 * `entry.order` pulls named fields to the front (the rest keep schema order),
 * and any catalogue field the schema does NOT advertise is appended. That
 * second path is not hypothetical — `auto_reframe` reads `subject_track` in
 * its handler and does not declare it, and without this the phone would have
 * no way to turn tracking off and no way to escape the tracker's feature gate.
 */
export function fieldsFor(schema: ToolSchema, entry: CatalogEntry): Field[] {
  const props = schema.input_schema.properties ?? {};
  const required = new Set(schema.input_schema.required ?? []);
  const hidden = new Set(entry.hide ?? []);
  const names = Object.keys(props);
  const order = entry.order;
  const ordered = order
    ? [...order.filter((n) => names.includes(n)), ...names.filter((n) => !order.includes(n))]
    : names;

  const fields: Field[] = [];
  for (const name of ordered) {
    const ov = entry.fields?.[name];
    if (hidden.has(name) || ov?.hidden) continue;
    fields.push(toField(name, props[name], required.has(name), ov));
  }
  for (const [name, ov] of Object.entries(entry.fields ?? {})) {
    if (name in props || ov.hidden || hidden.has(name)) continue;
    fields.push(toField(name, undefined, false, ov));
  }
  return fields;
}

// ---------------------------------------------------------------------------
// Seeding
// ---------------------------------------------------------------------------

function blankFor(widget: Widget): unknown {
  return widget === "switch" ? false : "";
}

/**
 * A picker cannot show "nothing", and it cannot show a choice it does not
 * offer either.
 *
 * TWO FAILURES THIS ONE FUNCTION AVOIDS:
 *
 *   - with no default at all, the control paints its first option, so a
 *     REQUIRED select's STATE has to start there too. `apply_export_preset`
 *     has an enum and no default; seeding "" made Run fail with "Required"
 *     under a control that visibly showed a choice, and the first option could
 *     only be submitted by picking another one and switching back;
 *   - a default that is no longer one of the options — a Mac whose enum
 *     changed under a phone build that did not — must NOT become the value.
 *     It would be invisible in the row of chips and would go over the wire on
 *     a Run the user never made a choice for.
 *
 * Optional selects keep "" and render an explicit "Default" chip, which is the
 * honest way to say "leave it to the Mac".
 */
function seedValue(f: Field): unknown {
  if (f.widget !== "select") return f.default ?? blankFor(f.widget);
  const d = f.default;
  const offered =
    d !== undefined && d !== "" && (!f.options || f.options.includes(d as string | number));
  if (offered) return d;
  return f.required && f.options && f.options.length > 0 ? f.options[0] : "";
}

export function initialValues(fields: readonly Field[]): Record<string, unknown> {
  return Object.fromEntries(fields.map((f) => [f.name, seedValue(f)]));
}

/** Keep a stepper inside the schema's own bounds. The UI calls this so the
 *  control cannot produce a value `buildArgs` would then reject. */
export function clampToField(f: Field, n: number): number {
  const lo = f.min ?? Number.NEGATIVE_INFINITY;
  const hi = f.max ?? Number.POSITIVE_INFINITY;
  return Math.min(hi, Math.max(lo, n));
}

// ---------------------------------------------------------------------------
// Coercion
// ---------------------------------------------------------------------------

type Conv = { value: unknown } | { error: string };
const OMIT: Conv = { value: undefined };

function isBlankScalar(v: unknown): boolean {
  return v === undefined || v === null || (typeof v === "string" && v.trim() === "");
}

function isBlank(v: unknown): boolean {
  if (Array.isArray(v)) return v.every(isBlankScalar);
  return isBlankScalar(v);
}

function convertNumber(f: Field, v: unknown): Conv {
  const n = typeof v === "number" ? v : Number(String(v).trim());
  if (!Number.isFinite(n)) return { error: "Needs to be a number" };
  const r = f.integer ? Math.round(n) : n;
  if (f.min !== undefined && r < f.min) return { error: `Lowest is ${f.min}` };
  if (f.max !== undefined && r > f.max) return { error: `Highest is ${f.max}` };
  return { value: r };
}

/** A picker hands back the option it was given; a numeric enum
 *  (`upscale.factor: [2, 4]`) has to go back over the wire as a number. */
function convertOption(f: Field, v: unknown): Conv {
  const numeric = !!f.options?.length && f.options.every((o) => typeof o === "number");
  return { value: numeric ? Number(v) : String(v) };
}

function convertList(f: Field, v: unknown): Conv {
  const raw = Array.isArray(v) ? v.map(String) : String(v).split(/[,\n]/);
  const items = raw.map((s) => s.trim()).filter(Boolean);
  if (!items.length) return f.required ? { error: "Required" } : OMIT;
  return { value: items };
}

function convertMapping(v: unknown): Conv {
  if (v && typeof v === "object" && !Array.isArray(v)) return { value: v };
  const out: Record<string, string> = {};
  for (const line of String(v)
    .split("\n")
    .map((s) => s.trim())
    .filter(Boolean)) {
    const eq = line.indexOf("=");
    if (eq < 1) return { error: `“${line}” needs the form KEY=VALUE` };
    out[line.slice(0, eq).trim()] = line.slice(eq + 1).trim();
  }
  return { value: out };
}

function convert(f: Field, v: unknown): Conv {
  // An explicit null is a VALUE for a nullable field — "true alpha", "no
  // loudness pass" — and must survive the blank check below, which would
  // otherwise omit it and leave the Mac on its default.
  if (f.nullable && v === null) return { value: null };
  if (isBlank(v)) return f.required ? { error: "Required" } : OMIT;
  switch (f.widget) {
    case "number":
    case "stepper":
    case "time":
      return convertNumber(f, v);
    case "switch":
      return { value: v === true };
    case "select":
      return convertOption(f, v);
    case "list":
      return convertList(f, v);
    case "mapping":
      return convertMapping(v);
    default:
      return { value: String(v).trim() };
  }
}

export interface BuiltArgs {
  args: Record<string, unknown>;
  errors: Record<string, string>;
}

export function buildArgs(fields: readonly Field[], values: Record<string, unknown>): BuiltArgs {
  const args: Record<string, unknown> = {};
  const errors: Record<string, string> = {};
  for (const f of fields) {
    const r = convert(f, values[f.name]);
    if ("error" in r) errors[f.name] = r.error;
    else if (r.value !== undefined) args[f.name] = r.value;
  }
  return { args, errors };
}
