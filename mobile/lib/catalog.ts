/**
 * Which of the Mac's tools get a card on the phone, and how each one reads.
 *
 * This is a hand-curated subset of `frontend/src/lib/aiCatalog.ts`, not a copy
 * of it. The desktop offers 42 tools; this offers 26, and the 16 it leaves out
 * are named in `MAC_ONLY` with the reason, so the AI screen can show the user
 * what is missing instead of letting them wonder. The editorial rule behind
 * the split is one line long:
 *
 *   THE PHONE OFFERS THE TOOLS WHOSE INPUT IS A DECISION, NOT A DOCUMENT.
 *
 * A tool that wants an absolute path on the Mac's disk, a box drawn on a
 * frame, a colour picked off the picture, or timeline surgery against a
 * scrubber is a tool that belongs to the Mac. A tool that wants a language, a
 * ratio, a sentence or a yes/no is a tool that is genuinely better on a phone,
 * because the phone is what you have in your hand while the Mac renders.
 *
 * THE COPY IS PORTED, NOT REWRITTEN. Every description below came from the
 * desktop catalogue, because those sentences encode real behaviour that took
 * shipping to learn — that translation downloads about 3 GB the first time,
 * that upscaling costs roughly a second a frame, that "cut to the beat" needs
 * music on the timeline before it can do anything. Rewriting them for tone
 * would have thrown that away.
 *
 * TWO DELIBERATE DIFFERENCES FROM THE DESKTOP:
 *
 * 1. `clip_id` is SHOWN, not hidden. The desktop hides it because the timeline
 *    selection supplies it; the phone's shared store has no selection concept
 *    at all, so a picker built from the EDL is the honest control. Hiding it
 *    here would have produced a Run button that 422s.
 *
 * 2. `advanced` is a sentence, not a flag. On the desktop it meant "this takes
 *    typed paths, meant for a source install", and none of those tools ship
 *    here. What is left that a user deserves a warning about is TIME: the Mac
 *    runs two job workers shared with whoever is sitting at it, and anything
 *    not on the async list is dispatched synchronously with a 90-second ceiling
 *    (`lib/api.ts::DISPATCH_TIMEOUT_MS`). When that ceiling is hit the tool has
 *    NOT failed — the Mac is still working and the edit will land. Every entry
 *    that can outlast the wait says so in its own words, and `app/ai.tsx` shows
 *    the sentence before the tap and again if the wait runs out.
 *
 * Drift against the live backend is checked on the Python side by
 * `tests/test_mobile_catalog_drift.py`, which greps the `tool:` / `gate:` /
 * `hide:` / `order:` literals below. Keep them as plain quoted strings.
 */

import { ASYNC_DISPATCH_TOOLS } from "./jobs";
import type { FeatureReport } from "./types";

// ---------------------------------------------------------------------------
// Shape
// ---------------------------------------------------------------------------

export type AiGroup =
  | "Auto edit"
  | "Captions & speech"
  | "Audio"
  | "Enhance"
  | "Cutout & effects"
  | "Text & brand"
  | "Find & search"
  | "Export";

export const GROUP_ORDER: readonly AiGroup[] = [
  "Auto edit",
  "Captions & speech",
  "Audio",
  "Enhance",
  "Cutout & effects",
  "Text & brand",
  "Find & search",
  "Export",
];

/**
 * `ai/features.py` keys, minus `gpu_transcribe` — that one is a speed tier
 * rather than a capability and must never grey out captions — and minus
 * `visual_search` and `object_erase`, whose tools are all Mac-only here.
 */
export const GATE_KEYS = [
  "captions",
  "noise_reduce",
  "stems",
  "bg_remove",
  "diarize",
  "tracking",
  "beats",
  "tts",
  "translate",
  "stabilize",
  "upscale",
  "interpolate",
] as const;

export type GateKey = (typeof GATE_KEYS)[number];

/** The controls a generated form can render. `lib/schemaForm.ts` owns the
 *  derivation; an override here only overrules it. */
export type Widget =
  | "text"
  /** A multi-line box. Not derivable from the schema — `text` is a string
   *  whether it is a hook line or a two-minute voiceover script — so it is
   *  always a catalogue decision. */
  | "paragraph"
  | "number"
  | "stepper"
  | "select"
  | "switch"
  | "time"
  | "list"
  | "mapping"
  | "clip";

export interface FieldOverride {
  widget?: Widget;
  label?: string;
  /** Beats the schema's own `default`. */
  default?: unknown;
  options?: (string | number)[];
  /** Shown under the control. Use it for the thing the schema cannot say. */
  help?: string;
  hidden?: boolean;
  /** Adds a switch that sends an explicit `null` — for `["string","null"]`
   *  arguments where null means something specific rather than "omitted". */
  nullable?: { label: string };
  /** Which tracks the clip picker offers. */
  clipFilter?: "video" | "media";
}

export interface CatalogEntry {
  tool: string;
  group: AiGroup;
  label: string;
  description: string;
  gate?: GateKey;
  /** An escape hatch: the gate does not apply while this field holds this
   *  value, because that path of the handler never imports the missing
   *  dependency. See `auto_reframe` below. */
  gateUnless?: { field: string; equals: unknown };
  /** Informational only. Never disables anything — the tool still runs, it
   *  just runs its non-Claude path. */
  keyHint?: string;
  /** No timeline mutation; the card renders the result instead. */
  readOnly?: boolean;
  /** The warning a user deserves before tapping. See difference 2 above. */
  advanced?: string;
  fields?: Record<string, FieldOverride>;
  hide?: string[];
  order?: string[];
}

/** A tool the phone deliberately does not offer, and why. Rendered verbatim,
 *  so the user finds out from the app rather than from its absence. */
export interface MacOnlyEntry {
  tool: string;
  label: string;
  why: string;
}

// ---------------------------------------------------------------------------
// Shared copy
// ---------------------------------------------------------------------------

const NO_KEY_HINT_VISION = "vision verify needs ANTHROPIC_API_KEY — transcript-only until then";
const NO_KEY_HINT_HOOK = "uses a transcript heuristic until ANTHROPIC_API_KEY is set";

/** The two lanes `remove_silences` and friends operate on. The schema types
 *  `track` as a bare string, so the choice has to come from here. */
const TRACK_CHOICES = ["v1", "v2"];

const SLOW_SCAN = "Reads the whole track before it cuts. On a long recording this can outlast the phone's wait — the cut still lands on your Mac.";

// ---------------------------------------------------------------------------
// The catalogue
// ---------------------------------------------------------------------------

export const MOBILE_CATALOG: readonly CatalogEntry[] = [
  // ---- Auto edit --------------------------------------------------------
  {
    tool: "remove_silences",
    group: "Auto edit",
    label: "Remove silences",
    description:
      "Detect silences in a track and ripple-cut them out. Defaults suit talking-head speech.",
    advanced: SLOW_SCAN,
    fields: {
      track: { widget: "select", options: TRACK_CHOICES, default: "v1" },
      threshold_db: { label: "Silence below (dB)" },
      min_dur: { label: "Shortest silence to cut (s)" },
      keep_pad: { label: "Breathing room to leave (s)" },
    },
  },
  {
    tool: "remove_fillers",
    group: "Auto edit",
    label: "Remove filler words",
    description:
      "Cut “um”, “uh”, “like”, “you know” out of the transcript and ripple-close the gaps.",
    advanced: SLOW_SCAN,
    fields: {
      words: { label: "Words (blank = the built-in list)" },
      track: { widget: "select", options: TRACK_CHOICES, default: "v1" },
    },
  },
  {
    tool: "auto_cut_to_beats",
    group: "Auto edit",
    label: "Cut to the beat",
    description:
      "Split v1 on every Nth beat of the music track. Needs music on the timeline first.",
    gate: "beats",
    advanced:
      "Analyses the music track for beats before it cuts, which can outlast the phone's wait. The cuts still land on your Mac.",
  },
  {
    tool: "auto_reframe",
    group: "Auto edit",
    label: "Auto-reframe",
    description:
      "Switch the canvas aspect and reframe every clip — subject-tracked when the tracker is installed.",
    // The handler only imports the tracker when subject_track is on, so the
    // centre-crop path stays runnable on a packaged Mac with no OpenCV.
    gate: "tracking",
    gateUnless: { field: "subject_track", equals: false },
    advanced:
      "Walks every clip in the project. Tracked runs are much slower than a centre crop and can outlast the phone's wait.",
    fields: {
      // Handler-only: read by dispatch.py but not advertised in input_schema,
      // which is why the form has to add it rather than derive it.
      subject_track: {
        widget: "switch",
        default: true,
        label: "Track the subject",
        help: "Off = centre-crop, which needs no tracker and is much faster.",
      },
    },
  },
  {
    tool: "make_shorts",
    group: "Auto edit",
    label: "Find shorts",
    description:
      "Pick highlight ranges from the v1 footage (transcript + audio energy). Optionally save each as a new session.",
    readOnly: true,
    // Read-only, so a wait that runs out costs the ANSWER and nothing else —
    // which is a different and much less alarming thing than a half-applied
    // edit, and the copy has to say which of the two it is.
    advanced:
      "Scores the whole recording before it answers, which can outlast the phone's wait. Nothing is changed either way, so if the wait runs out just run it again.",
    fields: {
      target_count: { label: "How many" },
      save_as_sessions: { label: "Save each as its own project" },
    },
  },

  // ---- Captions & speech -----------------------------------------------
  {
    tool: "auto_caption",
    group: "Captions & speech",
    label: "Auto captions",
    description:
      "Re-transcribe with Whisper large-v3 and lay broadcast-grade cues on the captions track.",
    gate: "captions",
    fields: {
      target: {
        label: "Caption language",
        help: "Blank captions in whatever was spoken.",
      },
      language: { label: "Spoken language", help: "Blank auto-detects." },
      model: {
        widget: "select",
        options: ["large-v3-turbo"],
        label: "Model",
        help: "Blank uses large-v3, the most accurate and the slowest.",
      },
    },
  },
  // Ungated on purpose: the handler only reads a transcript that already
  // exists, so a Mac with no ASR stack installed can still run it.
  {
    tool: "add_caption_track",
    group: "Captions & speech",
    label: "Captions from transcript",
    description:
      "Lay the existing transcript on the captions track — no re-transcription, so it is instant.",
  },
  {
    tool: "translate_captions",
    group: "Captions & speech",
    label: "Translate captions",
    description: "Translate the captions track in place, locally, on your Mac.",
    gate: "translate",
    advanced:
      "The first run downloads the MADLAD model, about 3 GB, onto your Mac. Start it while you are on Wi-Fi and expect to wait.",
    fields: {
      target_lang: {
        widget: "select",
        options: ["hi", "en", "es", "fr", "de", "pt", "ja", "ko", "zh"],
        default: "hi",
        label: "Translate to",
      },
      source_lang: { label: "Source language", help: "Blank uses the detected one." },
    },
  },
  {
    tool: "assign_caption_speakers",
    group: "Captions & speech",
    label: "Colour captions by speaker",
    description: "Run diarization and colour each speaker’s captions from the brand palette.",
    gate: "diarize",
    hide: ["turns"],
    advanced:
      "Diarization listens to the whole recording. It can outlast the phone's wait; the colours still land on your Mac.",
    fields: { num_speakers: { label: "How many speakers" } },
  },
  {
    tool: "name_speakers",
    group: "Captions & speech",
    label: "Name speakers",
    description: "Map diarized labels to display names for lower-thirds.",
    fields: {
      mapping: {
        label: "Names",
        help: "One SPEAKER_00=Host per line.",
      },
    },
  },

  // ---- Audio ------------------------------------------------------------
  {
    tool: "noise_reduce",
    group: "Audio",
    label: "Reduce noise",
    description: "Spectrally denoise a clip’s audio — hiss, fans, room tone.",
    gate: "noise_reduce",
    fields: {
      clip_id: { widget: "clip", clipFilter: "media", label: "Clip" },
      strength: { label: "Strength" },
    },
  },
  {
    tool: "vocal_isolate",
    group: "Audio",
    label: "Isolate vocals",
    description: "Demucs: pull the vocal stem onto the vo track and mute the clip’s own audio.",
    gate: "stems",
    fields: { clip_id: { widget: "clip", clipFilter: "media", label: "Clip" } },
  },
  {
    tool: "instrumental_isolate",
    group: "Audio",
    label: "Isolate instrumental",
    description:
      "Demucs: everything except vocals onto the music track; the clip’s own audio is muted.",
    gate: "stems",
    fields: { clip_id: { widget: "clip", clipFilter: "media", label: "Clip" } },
  },
  {
    tool: "tts_voiceover",
    group: "Audio",
    label: "AI voiceover",
    description: "Piper text-to-speech onto the vo track.",
    gate: "tts",
    advanced: "The voice downloads on first use, about 60 MB, onto your Mac.",
    fields: {
      text: { widget: "paragraph", label: "What to say" },
      start: { widget: "time", label: "Start" },
      volume_db: { label: "Level (dB)" },
    },
  },

  // ---- Enhance ----------------------------------------------------------
  {
    tool: "upscale",
    group: "Enhance",
    label: "AI upscale",
    description: "Real-ESRGAN 2× or 4× on one clip.",
    gate: "upscale",
    advanced:
      "About a second per frame on your Mac — a 30-second clip is roughly a 15-minute job. It runs in the background; you can leave this screen.",
    fields: { clip_id: { widget: "clip", clipFilter: "video", label: "Clip" } },
  },
  {
    tool: "stabilize",
    group: "Enhance",
    label: "Stabilize",
    description: "Two-pass vidstab on one clip.",
    gate: "stabilize",
    advanced:
      "Two full passes over the footage. Slow, and it runs in the background — you can leave this screen.",
    fields: { clip_id: { widget: "clip", clipFilter: "video", label: "Clip" } },
  },
  {
    tool: "smooth_slow_motion",
    group: "Enhance",
    label: "Smooth slow-mo",
    description:
      "RIFE frame interpolation: the clip becomes factor× longer with generated in-between frames.",
    gate: "interpolate",
    advanced:
      "Generates every in-between frame. Slow, and it runs in the background — you can leave this screen.",
    fields: {
      clip_id: { widget: "clip", clipFilter: "video", label: "Clip" },
      factor: { widget: "select", options: [2, 4], default: 2, label: "Slow down by" },
    },
  },

  // ---- Cutout & effects -------------------------------------------------
  {
    tool: "remove_background",
    group: "Cutout & effects",
    label: "Remove background",
    description:
      "rembg cutout of one clip. Flattens onto a colour so a chroma key can composite it, or keeps true alpha.",
    gate: "bg_remove",
    advanced: "Walks every frame of the clip. It runs in the background — you can leave this screen.",
    fields: {
      clip_id: { widget: "clip", clipFilter: "video", label: "Clip" },
      bg_color: {
        label: "Fill colour",
        nullable: { label: "True alpha (no fill colour)" },
      },
    },
  },

  // ---- Text & brand -----------------------------------------------------
  {
    tool: "add_hook_overlay",
    group: "Text & brand",
    label: "Hook overlay",
    description: "A bold hook line over the first seconds.",
    fields: {
      text: { widget: "paragraph", label: "Hook line" },
      duration: { widget: "time", label: "On screen for" },
    },
  },
  {
    tool: "add_super_text",
    group: "Text & brand",
    label: "Super text",
    description:
      "Bold on-screen text between two times. Replaces an overlapping overlay of the same role.",
    fields: {
      text: { widget: "paragraph" },
      start: { widget: "time" },
      end: { widget: "time" },
      upper: { label: "ALL CAPS" },
      allow_stack: { label: "Keep overlapping overlays" },
    },
  },
  {
    tool: "add_lower_third",
    group: "Text & brand",
    label: "Lower third",
    description: "Guest name and handle graphic.",
    fields: {
      start: { widget: "time" },
      end: { widget: "time", label: "End", help: "Blank uses the default length." },
    },
  },
  {
    tool: "generate_hook",
    group: "Text & brand",
    label: "Suggest hooks",
    description: "Draft three hook lines from the transcript. Add one from the result.",
    readOnly: true,
    keyHint: NO_KEY_HINT_HOOK,
  },

  // ---- Find & search ----------------------------------------------------
  {
    tool: "find_moments",
    group: "Find & search",
    label: "Find moments",
    description:
      "Natural-language search over the footage: transcript first, vision-verified on top.",
    readOnly: true,
    keyHint: NO_KEY_HINT_VISION,
    advanced:
      "Searches the whole recording and can outlast the phone's wait. Nothing is changed either way, so if the wait runs out just run it again.",
    fields: { query: { label: "What are you looking for" }, top_k: { label: "How many hits" } },
  },
  {
    tool: "audit_aesthetic",
    group: "Find & search",
    label: "Style audit",
    description: "House-style check: hook stack, pacing, captions — a 0–100 score with fixes.",
    readOnly: true,
  },

  // ---- Export -----------------------------------------------------------
  {
    tool: "apply_export_preset",
    group: "Export",
    label: "Platform preset",
    description: "Canvas, fps, bitrate and loudness for one platform.",
    fields: { name: { label: "Platform" } },
  },
  {
    tool: "set_loudness_target",
    group: "Export",
    label: "Export loudness",
    description: "LUFS target for the export loudness pass. Reels and TikTok −16, YouTube −14.",
    fields: {
      lufs: { label: "Target LUFS", nullable: { label: "Off (skip the loudness pass)" } },
    },
  },
];

/**
 * The 16 the phone leaves out. Shown in the AI screen so the absence is a
 * stated decision rather than something the user has to notice.
 */
export const MAC_ONLY: readonly MacOnlyEntry[] = [
  {
    tool: "cut_range",
    label: "Cut range",
    why: "Ripple-cutting a range is timeline surgery. It wants a scrubber and In/Out marks, not two typed timecodes.",
  },
  {
    tool: "multicam",
    label: "Multicam switch",
    why: "Takes a list of absolute paths to the angle files on the Mac's disk.",
  },
  {
    tool: "import_srt",
    label: "Import subtitles",
    why: "Reads a subtitle file off the Mac's disk.",
  },
  {
    tool: "export_srt",
    label: "Export .srt",
    why: "Writes a file onto the Mac, where this phone cannot open it.",
  },
  {
    tool: "export_vtt",
    label: "Export .vtt",
    why: "Writes a file onto the Mac, where this phone cannot open it.",
  },
  {
    tool: "export_ass",
    label: "Export .ass",
    why: "Writes a file onto the Mac, where this phone cannot open it.",
  },
  {
    tool: "diarize",
    label: "Detect speakers",
    why: "Returns a list of speaker turns to read against the timeline. “Colour captions by speaker” is the version of this that does something, and it is here.",
  },
  {
    tool: "chroma_key",
    label: "Chroma key",
    why: "Keying wants a colour picked off the frame and a preview to judge the result in.",
  },
  {
    tool: "object_erase",
    label: "Erase object",
    why: "You have to draw the box on the picture, and the phone has no frame to draw it on.",
  },
  {
    tool: "motion_track",
    label: "Motion-track an overlay",
    why: "Needs a box drawn on the video and an overlay picked off the timeline.",
  },
  {
    tool: "apply_brand_kit",
    label: "Brand kit",
    why: "Palette, font and end-card image are set up once, on the Mac, from files that live there.",
  },
  {
    tool: "apply_template",
    label: "Show template",
    why: "Takes a table of inputs that belongs on a keyboard.",
  },
  {
    tool: "apply_show_template",
    label: "Saved show template",
    why: "Applies a template you saved on the Mac, pointing at the Mac's own brand kit and music.",
  },
  {
    tool: "search_media",
    label: "Search footage",
    why: "Frame search hands back clip ids and file paths you can only act on at the timeline.",
  },
  {
    tool: "find_broll",
    label: "Find b-roll",
    why: "Searches a folder on the Mac and returns paths this phone cannot put on a timeline.",
  },
  {
    tool: "match_style",
    label: "Match a reference",
    why: "Fingerprints a reference video by absolute path on the Mac's disk.",
  },
];

// ---------------------------------------------------------------------------
// Gating
// ---------------------------------------------------------------------------

export type GateResult =
  | { ok: true; checking: boolean }
  | { ok: false; feature: string; fix: string; packagedExcluded: boolean };

/**
 * Whether the Mac can run this tool.
 *
 * `report === null` means the feature probe has not answered yet (or failed).
 * UNKNOWN IS NOT UNAVAILABLE: the tool stays runnable with a quiet "checking"
 * badge, because a 422 after the tap is a better outcome than a greyed-out
 * control that would in fact have worked.
 */
export function gateFor(
  entry: CatalogEntry,
  report: FeatureReport | null,
  values?: Record<string, unknown>,
): GateResult {
  if (!entry.gate) return { ok: true, checking: false };
  if (entry.gateUnless && values && values[entry.gateUnless.field] === entry.gateUnless.equals) {
    return { ok: true, checking: false };
  }
  if (report === null) return { ok: true, checking: true };
  const missing = report.unavailable.find((f) => f.key === entry.gate);
  if (!missing) return { ok: true, checking: false };
  return {
    ok: false,
    feature: missing.feature,
    fix: missing.fix ?? "",
    packagedExcluded: missing.packaged_app_excluded === true,
  };
}

// ---------------------------------------------------------------------------
// Filtering and grouping
// ---------------------------------------------------------------------------

function matches(entry: CatalogEntry, needle: string): boolean {
  return (
    entry.label.toLowerCase().includes(needle) ||
    entry.tool.toLowerCase().includes(needle) ||
    entry.description.toLowerCase().includes(needle) ||
    entry.group.toLowerCase().includes(needle)
  );
}

/**
 * The entries this Mac can actually show: catalogued, advertised by
 * `/api/tools`, and matching the search box.
 *
 * The `advertised` check is not defensive padding. A Mac running an older
 * build, or one where a tool was removed, still answers every other request
 * perfectly — so a card for a tool it does not have would look completely
 * normal right up until the tap. Passing `null` means "we have not asked yet"
 * and shows everything; an empty set means the Mac advertised nothing.
 */
export function visibleEntries(
  entries: readonly CatalogEntry[],
  advertised: ReadonlySet<string> | null,
  query = "",
): CatalogEntry[] {
  const needle = query.trim().toLowerCase();
  return entries.filter(
    (e) => (advertised === null || advertised.has(e.tool)) && (needle === "" || matches(e, needle)),
  );
}

export interface CatalogGroup {
  group: AiGroup;
  entries: CatalogEntry[];
}

export function groupEntries(entries: readonly CatalogEntry[]): CatalogGroup[] {
  return GROUP_ORDER.map((group) => ({
    group,
    entries: entries.filter((e) => e.group === group),
  })).filter((g) => g.entries.length > 0);
}

/**
 * True when this tool goes through the Mac's job queue rather than blocking a
 * request worker. Job runs report progress and can be cancelled where the
 * handler supports it; everything else is a synchronous dispatch under
 * `DISPATCH_TIMEOUT_MS`, which is exactly what `entry.advanced` warns about.
 */
export function runsAsJob(entry: CatalogEntry): boolean {
  return ASYNC_DISPATCH_TOOLS.has(entry.tool);
}
