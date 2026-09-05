/**
 * A tool's return value → something a card can render.
 *
 * Keyed by tool name because the handlers return genuinely different shapes
 * (`dispatch.py`: `find_moments` → `{matches}`, `make_shorts` → `{shorts}`,
 * `generate_hook` → `{candidates}`, `audit_aesthetic` → `{score, issues}`).
 * A generic "here is the JSON" view would lose the one thing the user wants
 * from a search on a phone: a readable timestamp they can act on when they
 * get back to the Mac.
 *
 * ONLY THE FOUR READ-ONLY TOOLS THE PHONE OFFERS HAVE A VIEW. The desktop has
 * eight; the other four (`search_media`, `find_broll`, `diarize`,
 * `match_style`) are Mac-only here, so porting their readers would have been
 * dead code pretending to be coverage.
 *
 * EVERY READER IS DEFENSIVE. A handler's shape can drift between the Mac's
 * version and this build, and a result view that throws would take the AI
 * screen down with it. `resultView` returns something renderable for any
 * input at all, including `null` and a bare string.
 */

export type ResultRow =
  /** A moment or a range in the recording. */
  | { kind: "range"; start: number; end: number; text: string; score?: number }
  /** A line of text the user might use as-is — a hook candidate. */
  | { kind: "text"; text: string }
  /** One finding from the style audit. */
  | { kind: "issue"; level: "error" | "warn" | "info"; text: string };

export interface ResultView {
  headline: string;
  rows: ResultRow[];
  note?: string;
  /** The untouched payload, for the card's "Raw result" disclosure. When a
   *  reader misses something, this is what lets the user see it anyway. */
  raw: unknown;
}

type Rec = Record<string, unknown>;

const isRec = (v: unknown): v is Rec => !!v && typeof v === "object" && !Array.isArray(v);
const recs = (v: unknown): Rec[] => (Array.isArray(v) ? v.filter(isRec) : []);
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const str = (v: unknown): string => (typeof v === "string" ? v : "");
const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

function rangeRow(r: Rec, text: string): ResultRow | null {
  const start = num(r.start);
  const end = num(r.end);
  if (start === null || end === null) return null;
  const score = num(r.score);
  return { kind: "range", start, end, text, ...(score !== null ? { score } : {}) };
}

function findMoments(r: Rec): ResultView {
  const rows = recs(r.matches).flatMap((m) => {
    // ai/vision.py emits two shapes: `{description}` for vision-ranked hits
    // and `{transcript, shot_description}` for transcript-ranked ones.
    const text = [str(m.transcript), str(m.shot_description) || str(m.description)]
      .filter(Boolean)
      .join(" — ");
    const row = rangeRow(m, text);
    return row ? [row] : [];
  });
  const query = str(r.query);
  const headline = rows.length
    ? `${plural(rows.length, "moment")}${query ? ` for “${query}”` : ""}`
    : str(r.summary) || "No moments found";
  return { headline, rows, raw: r };
}

function makeShorts(r: Rec): ResultView {
  const rows = recs(r.shorts).flatMap((s) => {
    const row = rangeRow(s, str(s.why));
    return row ? [row] : [];
  });
  const sessions = Array.isArray(r.new_sessions) ? r.new_sessions.length : 0;
  return {
    headline: str(r.summary) || plural(rows.length, "short"),
    rows,
    ...(sessions
      ? { note: `Saved ${plural(sessions, "project")} on your Mac — they are in your project list.` }
      : {}),
    raw: r,
  };
}

function generateHook(r: Rec): ResultView {
  const rows: ResultRow[] = (Array.isArray(r.candidates) ? r.candidates : [])
    .filter((c): c is string => typeof c === "string" && c.trim() !== "")
    .map((text) => ({ kind: "text", text }));
  return {
    headline: plural(rows.length, "hook idea"),
    rows,
    ...(str(r.source) ? { note: `Source: ${str(r.source)}` } : {}),
    raw: r,
  };
}

function auditAesthetic(r: Rec): ResultView {
  const score = num(r.score);
  const rows: ResultRow[] = recs(r.issues).map((i) => {
    const level = i.level === "error" ? "error" : i.level === "warn" ? "warn" : "info";
    return { kind: "issue", level, text: str(i.message) || str(i.key) || "issue" };
  });
  const hook = isRec(r.hook) ? num(r.hook.hook_score) : null;
  return {
    headline: score !== null ? `Score ${score}/100` : "Style audit",
    rows,
    ...(hook !== null ? { note: `Hook stack ${hook}/3` } : {}),
    raw: r,
  };
}

const VIEWS: Record<string, (r: Rec) => ResultView> = {
  find_moments: findMoments,
  make_shorts: makeShorts,
  generate_hook: generateHook,
  audit_aesthetic: auditAesthetic,
};

export function resultView(tool: string, result: unknown, label = tool): ResultView {
  const fallback = `${label} done`;
  if (!isRec(result)) return { headline: fallback, rows: [], raw: result };
  const view = VIEWS[tool];
  if (view) return view(result);
  return { headline: str(result.summary) || fallback, rows: [], raw: result };
}

/**
 * The one line a toast or a status strip shows after a mutating tool lands.
 * Falls back to the tool's own label rather than to "OK", because "Remove
 * silences done" tells you which of the two things you started has finished.
 */
export function resultSummary(tool: string, result: unknown, label: string): string {
  return resultView(tool, result, label).headline;
}
