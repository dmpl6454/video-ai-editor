/**
 * The chat frame parser.
 *
 * These tests are the only place the phone's assumptions about the Mac's wire
 * format are written down and checked. There is no simulator on the machine
 * this ships from, so a chunk-boundary bug found here costs a minute and one
 * found on the device costs a full EAS build.
 */

import { ChatStreamParser, readChatStream, type ChatEvent, type ChunkReader } from "../../lib/sse";

const enc = new TextEncoder();

/** A frame exactly as `main.py` writes it. */
const frame = (obj: unknown) => `data: ${JSON.stringify(obj)}\n\n`;

/** A reader over a fixed list of chunks — the shape `body.getReader()` has. */
function readerOver(chunks: (Uint8Array | string)[]): ChunkReader & { cancelled: boolean } {
  let i = 0;
  const state = {
    cancelled: false,
    read: async () => {
      const next = chunks[i++];
      if (next === undefined) return { done: true, value: undefined };
      return {
        done: false,
        value: typeof next === "string" ? enc.encode(next) : next,
      };
    },
    cancel: async () => {
      state.cancelled = true;
    },
  };
  return state;
}

async function collect(reader: ChunkReader): Promise<ChatEvent[]> {
  const out: ChatEvent[] = [];
  for await (const evt of readChatStream(reader)) out.push(evt);
  return out;
}

describe("ChatStreamParser", () => {
  test("one complete frame produces exactly one event", () => {
    const p = new ChatStreamParser();
    const events = p.push(frame({ type: "text_delta", text: "hello" }));
    expect(events).toEqual([{ type: "text_delta", text: "hello" }]);
  });

  test("a frame split across three chunks produces one event once complete", () => {
    const whole = frame({ type: "text_delta", text: "split me" });
    // Cut mid-JSON, mid-`data:` of the next frame's prefix, and mid-`\n\n`.
    const a = whole.slice(0, 12); // inside "data: {"type"
    const b = whole.slice(12, whole.length - 1); // everything up to the last \n
    const c = whole.slice(whole.length - 1); // the final newline of "\n\n"

    const p = new ChatStreamParser();
    expect(p.push(a)).toEqual([]);
    expect(p.push(b)).toEqual([]);
    expect(p.push(c)).toEqual([{ type: "text_delta", text: "split me" }]);
  });

  test("a `data:` prefix cut in half still parses once the rest lands", () => {
    const p = new ChatStreamParser();
    expect(p.push("da")).toEqual([]);
    expect(p.push('ta: {"type":"done"}\n\n')).toEqual([{ type: "done" }]);
  });

  test("two frames in one chunk produce two events, in order", () => {
    const p = new ChatStreamParser();
    const events = p.push(
      frame({ type: "text_delta", text: "one" }) + frame({ type: "text_delta", text: "two" }),
    );
    expect(events).toEqual([
      { type: "text_delta", text: "one" },
      { type: "text_delta", text: "two" },
    ]);
  });

  test("a malformed frame yields no event, does not throw, and is counted", () => {
    const p = new ChatStreamParser();
    expect(() => p.push("data: {not json\n\n")).not.toThrow();
    expect(p.push("data: {also not json\n\n")).toEqual([]);
    expect(p.malformedFrames).toBe(2);
  });

  test("the stream survives a bad frame and keeps delivering good ones", () => {
    const p = new ChatStreamParser();
    const events = p.push(
      "data: {broken\n\n" + frame({ type: "text_delta", text: "still here" }),
    );
    expect(events).toEqual([{ type: "text_delta", text: "still here" }]);
    expect(p.malformedFrames).toBe(1);
  });

  test("an event whose type the phone does not know is dropped, not crashed on", () => {
    const p = new ChatStreamParser();
    expect(p.push(frame({ type: "thinking_delta", text: "…" }))).toEqual([]);
  });

  test("flush after a truncated trailing frame yields nothing and says so", () => {
    const p = new ChatStreamParser();
    expect(p.push('data: {"type":"text_de')).toEqual([]);
    expect(p.flush()).toEqual([]);
    expect(p.endedMidFrame).toBe(true);
  });

  test("flush after a clean boundary reports no truncation", () => {
    const p = new ChatStreamParser();
    p.push(frame({ type: "done" }));
    expect(p.flush()).toEqual([]);
    expect(p.endedMidFrame).toBe(false);
  });

  test("a multi-byte character split across a chunk boundary reassembles", () => {
    // "नमस्ते 🎬" — Devanagari plus an astral-plane emoji, which is exactly what
    // a caption-translation turn streams back.
    const whole = frame({ type: "text_delta", text: "नमस्ते 🎬" });
    const bytes = enc.encode(whole);
    // Cut inside the emoji's four-byte sequence: the frame terminator is the
    // last two bytes, so three from the end is mid-character.
    const cut = bytes.length - 6;
    const p = new ChatStreamParser();
    expect(p.push(bytes.slice(0, cut))).toEqual([]);
    expect(p.push(bytes.slice(cut))).toEqual([{ type: "text_delta", text: "नमस्ते 🎬" }]);
  });

  test("a literal blank line inside the JSON string does not split the frame", () => {
    // `json.dumps` escapes a newline as the two characters \ and n, so the two
    // real newlines below never appear as bytes on the wire. This test is what
    // pins that assumption: if the backend ever stopped using json.dumps, the
    // frame WOULD split here and this would go red.
    const text = "first\n\nsecond";
    const p = new ChatStreamParser();
    const events = p.push(frame({ type: "text_delta", text }));
    expect(events).toEqual([{ type: "text_delta", text }]);
    expect(JSON.stringify(text)).toContain("\\n\\n");
  });

  test("`data:` with no space after the colon is accepted", () => {
    const p = new ChatStreamParser();
    expect(p.push('data:{"type":"done"}\n\n')).toEqual([{ type: "done" }]);
  });

  test("a comment keep-alive line is ignored rather than parsed", () => {
    const p = new ChatStreamParser();
    expect(p.push(": ping\n\n")).toEqual([]);
    expect(p.malformedFrames).toBe(0);
  });

  test("a tool_use and its result carry the id that pairs them", () => {
    const p = new ChatStreamParser();
    const events = p.push(
      frame({ type: "tool_use", name: "upscale", args: { clip_id: "c1" }, id: "tu_1" }) +
        frame({ type: "tool_result", name: "upscale", result: { summary: "ok" }, id: "tu_1" }),
    );
    expect(events).toHaveLength(2);
    expect(events[0]).toMatchObject({ type: "tool_use", id: "tu_1" });
    expect(events[1]).toMatchObject({ type: "tool_result", id: "tu_1" });
  });
});

describe("readChatStream", () => {
  test("yields every event in a stream and stops at done", async () => {
    const events = await collect(
      readerOver([
        frame({ type: "text_delta", text: "a" }),
        frame({ type: "text_delta", text: "b" }),
        frame({ type: "done" }),
        // The Mac sends nothing after `done`; if it did, we must not read it.
        frame({ type: "text_delta", text: "never" }),
      ]),
    );
    expect(events).toEqual([
      { type: "text_delta", text: "a" },
      { type: "text_delta", text: "b" },
      { type: "done" },
    ]);
  });

  test("cancels the reader once the turn ends", async () => {
    const reader = readerOver([frame({ type: "done" })]);
    await collect(reader);
    expect(reader.cancelled).toBe(true);
  });

  test("cancels the reader when the consumer breaks out early", async () => {
    const reader = readerOver([
      frame({ type: "text_delta", text: "a" }),
      frame({ type: "text_delta", text: "b" }),
      frame({ type: "done" }),
    ]);
    for await (const evt of readChatStream(reader)) {
      expect(evt.type).toBe("text_delta");
      break;
    }
    expect(reader.cancelled).toBe(true);
  });

  test("a stream that ends without `done` still delivers what arrived", async () => {
    const events = await collect(
      readerOver([frame({ type: "text_delta", text: "half a senten" })]),
    );
    expect(events).toEqual([{ type: "text_delta", text: "half a senten" }]);
  });

  test("an empty chunk is skipped rather than treated as end of stream", async () => {
    const events = await collect(
      readerOver([new Uint8Array(0), frame({ type: "done" })]),
    );
    expect(events).toEqual([{ type: "done" }]);
  });

  test("a reader that rejects lets the error out for the screen to diagnose", async () => {
    const reader: ChunkReader = {
      read: () => Promise.reject(new Error("Network request failed")),
    };
    await expect(collect(reader)).rejects.toThrow("Network request failed");
  });
});
