/**
 * The backend's shapes, mirrored for the phone.
 *
 * Every interface here is a port of something the Mac already sends, and the
 * port is deliberately literal — field names stay snake_case because that is
 * what comes over the wire, and nothing is "cleaned up" on the way in. A
 * rename here would be a silent divergence from `frontend/src/types.ts`, which
 * is the shape the desktop has been running against since M1.
 *
 * This file declares TYPES and the two narrowing predicates that are
 * inseparable from the union. Timeline arithmetic (clipDuration, clipEnd,
 * videoExtent) lives in `lib/edl.ts`; nothing in this file computes.
 */

// ---------------------------------------------------------------------------
// EDL — ported from frontend/src/types.ts
// ---------------------------------------------------------------------------

export interface Canvas {
  w: number;
  h: number;
  fps: number;
  bg: string;
}

/** A media clip. `in`/`out` are SOURCE seconds; `start` is TIMELINE seconds. */
export interface Clip {
  id: string;
  src: string;
  in: number;
  out: number;
  start: number;
  /**
   * Playback rate. Absent on most clips, and a curve dict rather than a number
   * on speed-ramped ones — which is exactly why `lib/edl.ts` reads it through
   * a guard instead of trusting the declared type. Drawing a 2× clip at its
   * source length is how the desktop once overlapped every neighbour the
   * backend had already rippled left.
   */
  speed?: number | Record<string, unknown> | null;
}

/** A text clip. Has `end` (an absolute timeline second), never `out`. */
export interface TextClip {
  id: string;
  text: string;
  start: number;
  end: number;
  role?: string;
  style?: {
    font?: string;
    size?: number;
    color?: string;
    stroke?: string;
    stroke_w?: number;
    /** TRI-STATE. null/absent means "use the role's own default", so an
     *  existing project keeps its capitals and only an explicit false
     *  lowercases one. Do not collapse this to a boolean. */
    upper?: boolean | null;
  };
  anim_in?: string | null;
  anim_out?: string | null;
  speaker?: string | null;
}

export type AnyClip = Clip | TextClip;

export interface Track {
  id: string;
  type: string;
  z: number;
  label?: string;
  clips: AnyClip[];
  muted?: boolean;
}

export interface Marker {
  id: string;
  time: number;
  label: string;
  color?: string;
}

export interface EDL {
  version: number;
  duration: number;
  canvas: Canvas;
  tracks: Track[];
  markers?: Marker[];
}

/** A media clip carries `src` and `out`; a text clip carries neither. */
export function isMediaClip(c: AnyClip): c is Clip {
  return "src" in c && "out" in c;
}

export function isTextClip(c: AnyClip): c is TextClip {
  return "text" in c && "end" in c;
}

// ---------------------------------------------------------------------------
// Ops and sessions
// ---------------------------------------------------------------------------

/** One entry in the undo log. `by` names who made the edit — and because the
 *  Mac and the phone share a single store and a single undo stack, that field
 *  is the only way a phone Undo can tell the user whose edit it is about to
 *  take back. */
export interface Op {
  seq: number;
  ts: number;
  tool: string;
  args: Record<string, unknown>;
  summary: string;
  edl_hash_before: string;
  edl_hash_after: string;
  by: string;
}

/** A row from GET /api/sessions (storage.py::list_sessions). `created_at` is a
 *  UNIX float from the directory's st_mtime, so it is really "last edited". */
export interface SessionSummaryRow {
  id: string;
  name: string;
  source?: string | null;
  created_at: number;
}

/** GET /api/sessions/{sid}. */
export interface SessionInfo {
  id: string;
  name: string;
  summary: {
    duration: number;
    canvas: Canvas;
    tracks: { id: string; type: string; label?: string; clips: number }[];
    edl_hash: string;
    ops: number;
  };
  ops: Op[];
  /** Mirrors the on-disk redo_stack.json. Optional so an older backend still
   *  typechecks; callers coerce it to a boolean. */
  redo_available?: boolean;
}

/** POST /api/sessions/{sid}/dispatch. `result` is the handler's own return
 *  dict and differs per tool, so callers narrow it themselves. */
export interface DispatchResponse {
  result: unknown;
  edl_hash: string;
  op: Op | null;
}

// ---------------------------------------------------------------------------
// Jobs — api/jobs.py::Job.to_dict
// ---------------------------------------------------------------------------

export type JobStatus = "queued" | "running" | "completed" | "failed" | "cancelled";

export interface Job {
  id: string;
  kind: string;
  status: JobStatus;
  /** 0..1, best-effort. Stays at 0 for any handler that takes no
   *  `set_progress` — read ToolSchema.reports_progress before drawing a bar. */
  progress: number;
  result: Record<string, unknown> | null;
  error: string | null;
  created_at: number;
  started_at: number | null;
  completed_at: number | null;
  session_id: string | null;
}

// ---------------------------------------------------------------------------
// Tool catalogue and feature gates
// ---------------------------------------------------------------------------

export interface JsonSchemaProp {
  type?: string | string[];
  description?: string;
  default?: unknown;
  enum?: unknown[];
  items?: { type?: string };
  minimum?: number;
  maximum?: number;
}

/**
 * GET /api/tools. `cancellable` and `reports_progress` are derived server-side
 * from the handler's SIGNATURE (main.py::_handler_hook_flags) — a tool with no
 * `cancel_event` parameter keeps running after a cancel and commits anyway.
 * Never hardcode either flag; a UI that promises a Cancel the backend cannot
 * honour is worse than one that offers none.
 */
export interface ToolSchema {
  name: string;
  description: string;
  category?: string;
  cancellable: boolean;
  reports_progress: boolean;
  input_schema: {
    type: "object";
    properties: Record<string, JsonSchemaProp>;
    required: string[];
  };
}

/** GET /api/features. `fix` appears only on `unavailable` entries and is
 *  written to be shown to the user verbatim. */
export interface FeatureEntry {
  key: string;
  feature: string;
  tools: string[];
  note?: string;
  fix?: string;
  packaged_app_excluded?: boolean;
}

export interface FeatureReport {
  packaged_app: boolean;
  python: string;
  anthropic_key_set: boolean;
  available: FeatureEntry[];
  unavailable: FeatureEntry[];
  summary: string;
}
