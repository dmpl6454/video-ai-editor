/**
 * The phone's tool catalogue — internal consistency only.
 *
 * Whether these tool names, gate keys and argument names still exist on the
 * Mac is checked by `tests/test_mobile_catalog_drift.py` in the pytest suite,
 * because the backend is the half that moves. What THIS file pins is the
 * catalogue's own invariants: the split against `MAC_ONLY` is total and
 * disjoint, search finds what a person would type, and a gate never greys out
 * a tool it has no evidence against.
 */

import { ASYNC_DISPATCH_TOOLS } from "../../lib/jobs";
import {
  gateFor,
  groupEntries,
  GROUP_ORDER,
  MAC_ONLY,
  MOBILE_CATALOG,
  runsAsJob,
  visibleEntries,
  type CatalogEntry,
} from "../../lib/catalog";
import type { FeatureEntry, FeatureReport } from "../../lib/types";

const entry = (tool: string): CatalogEntry => {
  const found = MOBILE_CATALOG.find((e) => e.tool === tool);
  if (found === undefined) throw new Error(`no catalogue entry for ${tool}`);
  return found;
};

function report(unavailable: FeatureEntry[]): FeatureReport {
  return {
    packaged_app: false,
    python: "3.12.0",
    anthropic_key_set: true,
    available: [],
    unavailable,
    summary: `${unavailable.length} optional features missing`,
  };
}

const allNames = new Set(MOBILE_CATALOG.map((e) => e.tool));

describe("the catalogue's shape", () => {
  test("has exactly 26 entries", () => {
    expect(MOBILE_CATALOG.length).toBe(26);
  });

  test("every tool name is a plain, whitespace-free identifier", () => {
    for (const e of MOBILE_CATALOG) {
      expect(typeof e.tool).toBe("string");
      expect(e.tool).toMatch(/^[a-z0-9_]+$/);
    }
  });

  test("no tool is listed twice", () => {
    expect(allNames.size).toBe(MOBILE_CATALOG.length);
  });

  test("no tool is both offered here and declared Mac-only", () => {
    const overlap = MAC_ONLY.filter((m) => allNames.has(m.tool)).map((m) => m.tool);
    expect(overlap).toEqual([]);
  });

  test("every Mac-only entry explains itself in a sentence", () => {
    for (const m of MAC_ONLY) {
      expect(m.tool).toMatch(/^[a-z0-9_]+$/);
      expect(m.label.length).toBeGreaterThan(0);
      // Long enough to be a reason rather than a label repeated back.
      expect(m.why.length).toBeGreaterThan(30);
    }
  });

  test("every entry has copy a person can read", () => {
    for (const e of MOBILE_CATALOG) {
      expect(e.label.length).toBeGreaterThan(0);
      expect(e.description.length).toBeGreaterThan(20);
      expect(GROUP_ORDER).toContain(e.group);
    }
  });

  test("every group in GROUP_ORDER is actually used", () => {
    const used = new Set(MOBILE_CATALOG.map((e) => e.group));
    for (const g of GROUP_ORDER) expect(used.has(g)).toBe(true);
  });

  test("grouping preserves every entry and drops empty groups", () => {
    const grouped = groupEntries(MOBILE_CATALOG);
    const total = grouped.reduce((n, g) => n + g.entries.length, 0);
    expect(total).toBe(MOBILE_CATALOG.length);
    expect(grouped.every((g) => g.entries.length > 0)).toBe(true);
  });
});

describe("advanced warnings", () => {
  test("every entry marked advanced carries a why-style sentence, not a flag", () => {
    for (const e of MOBILE_CATALOG) {
      if (e.advanced === undefined) continue;
      expect(typeof e.advanced).toBe("string");
      expect(e.advanced.length).toBeGreaterThan(30);
      expect(e.advanced).toMatch(/\.$/);
    }
  });

  /**
   * The specific hazard the field exists for: a tool that walks the whole
   * recording but is NOT on the async list runs as a synchronous dispatch
   * under a 90-second ceiling, and hitting that ceiling looks like a failure
   * when it is not. Each of these must say so before the tap.
   */
  test("the long-running tools that are not background jobs all warn first", () => {
    const slowAndSynchronous = [
      "remove_silences",
      "remove_fillers",
      "auto_cut_to_beats",
      "auto_reframe",
      "make_shorts",
      "translate_captions",
      "assign_caption_speakers",
      "find_moments",
    ];
    for (const tool of slowAndSynchronous) {
      expect(ASYNC_DISPATCH_TOOLS.has(tool)).toBe(false);
      expect(entry(tool).advanced).toBeDefined();
    }
  });

  test("runsAsJob agrees with the shared async list", () => {
    for (const e of MOBILE_CATALOG) {
      expect(runsAsJob(e)).toBe(ASYNC_DISPATCH_TOOLS.has(e.tool));
    }
  });
});

describe("visibleEntries", () => {
  test("drops entries this Mac does not advertise", () => {
    const advertised = new Set(MOBILE_CATALOG.map((e) => e.tool));
    advertised.delete("upscale");
    const shown = visibleEntries(MOBILE_CATALOG, advertised);
    expect(shown.map((e) => e.tool)).not.toContain("upscale");
    expect(shown.length).toBe(MOBILE_CATALOG.length - 1);
  });

  test("a null tool list means 'not asked yet' and shows everything", () => {
    expect(visibleEntries(MOBILE_CATALOG, null).length).toBe(MOBILE_CATALOG.length);
  });

  test("an empty tool list shows nothing", () => {
    expect(visibleEntries(MOBILE_CATALOG, new Set())).toEqual([]);
  });

  test("search matches the label, case-insensitively", () => {
    const hits = visibleEntries(MOBILE_CATALOG, null, "AUTO CAPTIONS");
    expect(hits.map((e) => e.tool)).toContain("auto_caption");
  });

  test("search matches the tool name", () => {
    const hits = visibleEntries(MOBILE_CATALOG, null, "smooth_slow");
    expect(hits.map((e) => e.tool)).toEqual(["smooth_slow_motion"]);
  });

  test("search matches the description", () => {
    const hits = visibleEntries(MOBILE_CATALOG, null, "Demucs");
    expect(hits.map((e) => e.tool).sort()).toEqual(["instrumental_isolate", "vocal_isolate"]);
  });

  test("search matches the group name", () => {
    const hits = visibleEntries(MOBILE_CATALOG, null, "enhance");
    expect(hits.every((e) => e.group === "Enhance")).toBe(true);
    expect(hits.length).toBe(3);
  });

  test("a blank or whitespace query filters nothing", () => {
    expect(visibleEntries(MOBILE_CATALOG, null, "   ").length).toBe(MOBILE_CATALOG.length);
  });

  test("the two filters compose", () => {
    const advertised = new Set(["upscale"]);
    expect(visibleEntries(MOBILE_CATALOG, advertised, "stabilize")).toEqual([]);
    expect(visibleEntries(MOBILE_CATALOG, advertised, "upscale").map((e) => e.tool)).toEqual([
      "upscale",
    ]);
  });
});

describe("gateFor", () => {
  test("an ungated tool is always runnable and never 'checking'", () => {
    expect(gateFor(entry("add_caption_track"), null)).toEqual({ ok: true, checking: false });
  });

  test("an unknown feature report leaves a gated tool runnable, marked checking", () => {
    expect(gateFor(entry("upscale"), null)).toEqual({ ok: true, checking: true });
  });

  test("a gated tool is runnable when the report does not list it as missing", () => {
    expect(gateFor(entry("upscale"), report([]))).toEqual({ ok: true, checking: false });
  });

  test("a missing feature blocks the tool and carries the Mac's own fix", () => {
    const result = gateFor(
      entry("upscale"),
      report([
        {
          key: "upscale",
          feature: "AI upscaling (Real-ESRGAN)",
          tools: ["upscale"],
          fix: "`uv sync --extra upscale`",
        },
      ]),
    );
    expect(result).toEqual({
      ok: false,
      feature: "AI upscaling (Real-ESRGAN)",
      fix: "`uv sync --extra upscale`",
      packagedExcluded: false,
    });
  });

  test("a feature the packaged app excludes on purpose is reported as such", () => {
    const result = gateFor(
      entry("stabilize"),
      report([
        {
          key: "stabilize",
          feature: "Stabilisation",
          tools: ["stabilize"],
          packaged_app_excluded: true,
        },
      ]),
    );
    expect(result).toMatchObject({ ok: false, fix: "", packagedExcluded: true });
  });

  /**
   * The `aiCatalog.ts` rule the desktop learned the hard way: `auto_reframe`
   * only imports the tracker when `subject_track` is on, so a centre-crop run
   * must stay available on a Mac with no tracker installed.
   */
  test("gateUnless keeps auto-reframe runnable without the tracker when tracking is off", () => {
    const missingTracker = report([
      { key: "tracking", feature: "Subject tracking", tools: ["auto_reframe"], fix: "uv sync" },
    ]);
    expect(gateFor(entry("auto_reframe"), missingTracker, { subject_track: false })).toEqual({
      ok: true,
      checking: false,
    });
  });

  test("gateUnless does NOT rescue the tracked path", () => {
    const missingTracker = report([
      { key: "tracking", feature: "Subject tracking", tools: ["auto_reframe"], fix: "uv sync" },
    ]);
    expect(gateFor(entry("auto_reframe"), missingTracker, { subject_track: true })).toMatchObject({
      ok: false,
      feature: "Subject tracking",
    });
  });

  test("gateUnless is ignored when no form values exist yet", () => {
    const missingTracker = report([
      { key: "tracking", feature: "Subject tracking", tools: ["auto_reframe"], fix: "uv sync" },
    ]);
    expect(gateFor(entry("auto_reframe"), missingTracker)).toMatchObject({ ok: false });
  });
});
