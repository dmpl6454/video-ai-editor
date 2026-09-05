/**
 * Turning a handler's return value into rows.
 *
 * The point of these tests is that a DRIFTED shape degrades instead of
 * crashing: the AI screen is not inside an error boundary, and a reader that
 * threw on an unexpected field would take every card down with it, on a device
 * with no console to explain why.
 */

import { resultSummary, resultView } from "../../lib/aiResults";

describe("find_moments", () => {
  test("reads both of the shapes ai/vision.py emits", () => {
    const view = resultView("find_moments", {
      query: "the punchline",
      matches: [
        { start: 12, end: 18, transcript: "and then", shot_description: "wide shot", score: 0.8 },
        { start: 40, end: 44, description: "close up" },
      ],
    });
    expect(view.headline).toBe("2 moments for “the punchline”");
    expect(view.rows[0]).toEqual({
      kind: "range",
      start: 12,
      end: 18,
      text: "and then — wide shot",
      score: 0.8,
    });
    expect(view.rows[1]).toEqual({ kind: "range", start: 40, end: 44, text: "close up" });
  });

  test("a match with no times is skipped rather than rendered at zero", () => {
    const view = resultView("find_moments", { matches: [{ description: "no timing" }] });
    expect(view.rows).toEqual([]);
    expect(view.headline).toBe("No moments found");
  });

  test("one hit is singular", () => {
    const view = resultView("find_moments", { matches: [{ start: 1, end: 2 }] });
    expect(view.headline).toBe("1 moment");
  });
});

describe("make_shorts", () => {
  test("uses the handler's own summary and names the saved projects", () => {
    const view = resultView("make_shorts", {
      summary: "3 shorts found",
      shorts: [{ start: 0, end: 30, why: "strong open" }],
      new_sessions: ["s_a", "s_b"],
    });
    expect(view.headline).toBe("3 shorts found");
    expect(view.note).toBe("Saved 2 projects on your Mac — they are in your project list.");
  });

  test("no saved sessions means no note about them", () => {
    const view = resultView("make_shorts", { shorts: [{ start: 0, end: 5, why: "" }] });
    expect(view.note).toBeUndefined();
  });
});

describe("generate_hook", () => {
  test("each candidate becomes a text row", () => {
    const view = resultView("generate_hook", {
      candidates: ["Stop scrolling.", "You are doing this wrong.", "  "],
      source: "transcript",
    });
    expect(view.rows).toEqual([
      { kind: "text", text: "Stop scrolling." },
      { kind: "text", text: "You are doing this wrong." },
    ]);
    expect(view.note).toBe("Source: transcript");
  });

  test("a non-string candidate is discarded, not rendered as [object Object]", () => {
    const view = resultView("generate_hook", { candidates: [{ text: "nope" }, "yes"] });
    expect(view.rows).toEqual([{ kind: "text", text: "yes" }]);
  });
});

describe("audit_aesthetic", () => {
  test("leads with the score and maps every issue level", () => {
    const view = resultView("audit_aesthetic", {
      score: 72,
      hook: { hook_score: 2 },
      issues: [
        { level: "error", message: "no hook" },
        { level: "warn", message: "long cuts" },
        { level: "info", message: "captions ok" },
        { message: "no level at all" },
      ],
    });
    expect(view.headline).toBe("Score 72/100");
    expect(view.note).toBe("Hook stack 2/3");
    expect(view.rows.map((r) => (r.kind === "issue" ? r.level : null))).toEqual([
      "error",
      "warn",
      "info",
      "info",
    ]);
  });

  test("an issue with no message falls back to its key rather than an empty row", () => {
    const view = resultView("audit_aesthetic", { issues: [{ key: "hook_missing" }] });
    expect(view.rows[0]).toMatchObject({ text: "hook_missing" });
    expect(view.headline).toBe("Style audit");
  });
});

describe("the fallback", () => {
  test("an uncatalogued tool shows its summary", () => {
    expect(resultView("remove_silences", { summary: "cut 12 silences" }).headline).toBe(
      "cut 12 silences",
    );
  });

  test("a result with no summary names the tool by its label", () => {
    expect(resultView("stabilize", { ok: true }, "Stabilize").headline).toBe("Stabilize done");
  });

  test("null, a bare string and an array are all renderable", () => {
    for (const value of [null, "ok", [1, 2, 3]]) {
      const view = resultView("whatever", value, "Whatever");
      expect(view.headline).toBe("Whatever done");
      expect(view.rows).toEqual([]);
      expect(view.raw).toBe(value);
    }
  });

  test("a reader never throws on a shape it did not expect", () => {
    for (const tool of ["find_moments", "make_shorts", "generate_hook", "audit_aesthetic"]) {
      expect(() => resultView(tool, { matches: "not a list", shorts: 7, issues: null })).not.toThrow();
    }
  });

  test("resultSummary is the headline, so a toast and a card agree", () => {
    const payload = { summary: "cut 12 silences" };
    expect(resultSummary("remove_silences", payload, "Remove silences")).toBe(
      resultView("remove_silences", payload, "Remove silences").headline,
    );
  });
});
