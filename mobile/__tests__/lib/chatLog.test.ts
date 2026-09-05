/**
 * The transcript reducer.
 *
 * The interesting cases are all about ORDER: deltas that must join the bubble
 * being written and not the one before the last tool call, results that must
 * find their own call rather than the most recent one with the same name, and
 * a persisted history that has to rebuild the same shape the stream produced.
 */

import {
  applyChatEvent,
  historyToMessages,
  noticeMessage,
  toolCallLine,
  userMessage,
  type ChatMessage,
} from "../../lib/chatLog";
import type { ChatEvent } from "../../lib/sse";

const fold = (events: ChatEvent[], from: ChatMessage[] = []): ChatMessage[] =>
  events.reduce<ChatMessage[]>(applyChatEvent, from);

const delta = (text: string): ChatEvent => ({ type: "text_delta", text });
const use = (id: string, name: string, args: Record<string, unknown> = {}): ChatEvent => ({
  type: "tool_use",
  id,
  name,
  args,
});
const result = (id: string, r: unknown, isError = false): ChatEvent => ({
  type: "tool_result",
  id,
  name: "ignored",
  result: r,
  ...(isError ? { is_error: true } : {}),
});

describe("applyChatEvent", () => {
  test("consecutive deltas join one assistant bubble", () => {
    const out = fold([delta("Hel"), delta("lo "), delta("there")]);
    expect(out).toEqual([{ kind: "assistant", seq: 0, text: "Hello there" }]);
  });

  test("a tool call ends the bubble, so the next narration is its own", () => {
    const out = fold([delta("Cutting."), use("t1", "remove_silences"), delta("Done.")]);
    expect(out.map((m) => m.kind)).toEqual(["assistant", "tool", "assistant"]);
    expect(out[0]).toMatchObject({ text: "Cutting." });
    expect(out[2]).toMatchObject({ text: "Done." });
  });

  test("a result attaches to its own call, not to the most recent one by name", () => {
    const out = fold([
      use("t1", "apply_export_preset", { name: "reels" }),
      use("t2", "apply_export_preset", { name: "shorts" }),
      result("t1", { summary: "reels" }),
    ]);
    expect(out[0]).toMatchObject({ callId: "t1", result: { summary: "reels" }, ok: true });
    expect(out[1]).toMatchObject({ callId: "t2" });
    expect((out[1] as { result?: unknown }).result).toBeUndefined();
  });

  test("a failed tool is marked so the row can say failed rather than done", () => {
    const out = fold([use("t1", "upscale"), result("t1", "RuntimeError: nope", true)]);
    expect(out[0]).toMatchObject({ ok: false });
  });

  test("a result with no matching call is still shown rather than dropped", () => {
    const out = fold([result("orphan", { summary: "ran anyway" })]);
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({ kind: "tool", callId: "orphan", tool: "(unknown)" });
  });

  test("an error event becomes a notice the user can read", () => {
    const out = fold([
      { type: "error", message: "ANTHROPIC_API_KEY is not set. Add it to ~/video-ai-editor/.env and restart." },
    ]);
    expect(out[0]).toMatchObject({ kind: "notice", tone: "danger" });
    expect((out[0] as { text: string }).text).toContain("ANTHROPIC_API_KEY");
  });

  test("op and done add nothing to the transcript", () => {
    const before = fold([delta("hi")]);
    const after = fold(
      [
        { type: "op", op: { seq: 1, ts: 0, tool: "x", args: {}, summary: "s", edl_hash_before: "a", edl_hash_after: "b", by: "claude" } },
        { type: "done" },
      ],
      before,
    );
    expect(after).toEqual(before);
  });

  test("the reducer never mutates the list it was given", () => {
    const before = fold([delta("hi")]);
    const snapshot = JSON.stringify(before);
    applyChatEvent(before, delta(" there"));
    expect(JSON.stringify(before)).toBe(snapshot);
  });

  test("seq is unique and monotonic across every message kind", () => {
    const out = fold(
      [delta("a"), use("t1", "x"), delta("b"), { type: "error", message: "bad" }],
      userMessage([], "go"),
    );
    const seqs = out.map((m) => m.seq);
    expect(seqs).toEqual([0, 1, 2, 3, 4]);
  });

  test("a notice can be appended without disturbing the numbering", () => {
    const out = noticeMessage(userMessage([], "go"), "warn", "stopped");
    expect(out.map((m) => m.seq)).toEqual([0, 1]);
  });
});

describe("toolCallLine", () => {
  test("names the tool alone when it took no arguments", () => {
    expect(toolCallLine("generate_hook", {})).toBe("generate_hook");
  });

  test("shows scalar arguments, quoting strings", () => {
    expect(toolCallLine("upscale", { clip_id: "c1", factor: 4 })).toBe(
      'upscale(clip_id=“c1”, factor=4)',
    );
  });

  test("truncates a long string rather than filling the row with it", () => {
    const line = toolCallLine("add_hook_overlay", { text: "x".repeat(80) });
    expect(line.length).toBeLessThan(60);
    expect(line).toContain("…");
  });

  test("summarises arrays, objects and null instead of dumping them", () => {
    expect(toolCallLine("t", { a: [1, 2, 3], b: { x: 1 }, c: null })).toBe("t(a=[3], b={…}, c=null)");
  });
});

describe("historyToMessages", () => {
  test("a plain string message becomes a bubble", () => {
    const out = historyToMessages([{ role: "user", content: "cut the silences" }]);
    expect(out).toEqual([{ kind: "user", seq: 0, text: "cut the silences" }]);
  });

  test("block content rebuilds text, tool calls and their results", () => {
    const out = historyToMessages([
      { role: "user", content: "do it" },
      {
        role: "assistant",
        content: [
          { type: "text", text: "On it." },
          { type: "tool_use", id: "t1", name: "remove_silences", input: { track: "v1" } },
        ],
      },
      {
        role: "user",
        content: [{ type: "tool_result", tool_use_id: "t1", content: "cut 12 silences" }],
      },
    ]);
    expect(out.map((m) => m.kind)).toEqual(["user", "assistant", "tool"]);
    expect(out[2]).toMatchObject({
      tool: "remove_silences",
      args: { track: "v1" },
      result: "cut 12 silences",
      ok: true,
    });
  });

  test("an errored tool result is marked failed", () => {
    const out = historyToMessages([
      { role: "assistant", content: [{ type: "tool_use", id: "t1", name: "upscale", input: {} }] },
      { role: "user", content: [{ type: "tool_result", tool_use_id: "t1", content: "no", is_error: true }] },
    ]);
    expect(out[0]).toMatchObject({ ok: false });
  });

  test("empty and whitespace-only text is dropped rather than shown as a blank bubble", () => {
    expect(historyToMessages([{ role: "user", content: "   " }])).toEqual([]);
    expect(historyToMessages([{ role: "assistant", content: [{ type: "text", text: "" }] }])).toEqual([]);
  });

  test("anything that is not a message list comes back empty rather than throwing", () => {
    expect(historyToMessages(null)).toEqual([]);
    expect(historyToMessages("nope")).toEqual([]);
    expect(historyToMessages([null, 3, { role: "system", content: "x" }])).toEqual([]);
  });

  test("a tool_use with a non-object input still renders with empty arguments", () => {
    const out = historyToMessages([
      { role: "assistant", content: [{ type: "tool_use", id: "t1", name: "x", input: "oops" }] },
    ]);
    expect(out[0]).toMatchObject({ kind: "tool", args: {} });
  });

  test("seq numbering is contiguous across a multi-turn history", () => {
    const out = historyToMessages([
      { role: "user", content: "one" },
      { role: "assistant", content: [{ type: "text", text: "two" }, { type: "text", text: "three" }] },
    ]);
    expect(out.map((m) => m.seq)).toEqual([0, 1, 2]);
  });
});
