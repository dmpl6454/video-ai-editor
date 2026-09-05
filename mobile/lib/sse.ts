/**
 * Reading the Mac's chat stream.
 *
 * WHAT THE MAC ACTUALLY SENDS. `POST /api/sessions/{sid}/chat` is a plain
 * `StreamingResponse` whose generator yields `f"data: {json.dumps(evt)}\n\n"`
 * and nothing else (main.py). There is no `event:` line, no `id:`, no retry
 * directive and no comment keep-alive — `sse-starlette` is used on other
 * routes but deliberately not on this one. So this parser is written to THAT
 * wire format rather than to the EventSource spec, which is why it is thirty
 * lines instead of three hundred.
 *
 * WHY SPLITTING ON A BLANK LINE IS SAFE. A frame boundary is `\n\n`, and the
 * payload between the boundaries is JSON produced by `json.dumps`, which
 * escapes every control character — a newline inside an assistant's reply
 * crosses the wire as the two characters `\` and `n`, never as a byte 0x0A.
 * A raw `\n\n` therefore cannot appear inside a frame, and the split cannot
 * cut one in half. If the backend ever stopped using `json.dumps` this
 * assumption would break silently, which is why `sse.test.ts` pins it.
 *
 * WHY THE DECODER IS STATEFUL. Chunks arrive at whatever size the socket
 * hands over, and a multi-byte character — every emoji Claude writes, every
 * Devanagari caption it quotes back — can be cut across two of them. Decoding
 * each chunk independently turns that into a replacement character in the
 * middle of a word. `TextDecoder` with `{ stream: true }` holds the trailing
 * bytes until the rest arrives; that is the entire reason this class exists
 * rather than a `String.fromCharCode` loop.
 *
 * WHY ONE BAD FRAME MUST NOT KILL THE STREAM. The desktop learned this the
 * hard way: an uncaught `JSON.parse` throw escaped the read loop and chat
 * stopped mid-sentence with nothing on screen to say it had. Here a frame that
 * will not parse is counted and dropped, and the caller can tell the user that
 * part of the reply was unreadable — which is a much better thing to see than
 * a sentence that simply stops.
 */

import type { Op } from "./types";

// ---------------------------------------------------------------------------
// The event union — agent/loop.py's module docstring, ported literally
// ---------------------------------------------------------------------------

export type ChatEvent =
  /** A slice of assistant prose. Concatenate them in arrival order. */
  | { type: "text_delta"; text: string }
  /** A tool the agent is about to run. `id` pairs it with its result. */
  | { type: "tool_use"; name: string; args: Record<string, unknown>; id: string }
  /** That tool's return value. `is_error` marks a handler that raised. */
  | { type: "tool_result"; name: string; result: unknown; id: string; is_error?: boolean }
  /** The tool call changed the EDL and this is the op log entry it produced. */
  | { type: "op"; op: Op }
  /** End of turn. Nothing follows it. */
  | { type: "done" }
  /** The turn failed. `message` is written to be shown verbatim. */
  | { type: "error"; message: string };

export type ChatEventType = ChatEvent["type"];

const KNOWN_TYPES: ReadonlySet<string> = new Set<ChatEventType>([
  "text_delta",
  "tool_use",
  "tool_result",
  "op",
  "done",
  "error",
]);

/** The frame delimiter. See "WHY SPLITTING ON A BLANK LINE IS SAFE" above. */
const FRAME_SEPARATOR = "\n\n";

/**
 * The three things a complete frame can be.
 *
 * "empty" and "unreadable" are kept apart on purpose: a frame carrying no
 * `data:` line at all is a comment or a keep-alive and is perfectly normal,
 * while a `data:` line that will not parse is a real symptom the screen should
 * be able to mention. Folding them together would have the malformed counter
 * tick on every keep-alive the Mac might one day start sending.
 */
type FrameResult =
  | { kind: "event"; event: ChatEvent }
  | { kind: "empty" }
  | { kind: "unreadable" };

/**
 * Parse one frame's worth of text.
 *
 * Every `data:` line in the frame is concatenated with a newline before
 * parsing, which is what the SSE spec says to do with a multi-line payload.
 * The Mac only ever sends one, but honouring the rule costs a single `join`.
 */
function parseFrame(frame: string): FrameResult {
  const data: string[] = [];
  for (const line of frame.split("\n")) {
    if (!line.startsWith("data:")) continue;
    // "data: {…}" and "data:{…}" are both legal; only the first space is part
    // of the framing, so slice the prefix and trim exactly one leading space.
    const rest = line.slice(5);
    data.push(rest.startsWith(" ") ? rest.slice(1) : rest);
  }
  if (data.length === 0) return { kind: "empty" };

  let parsed: unknown;
  try {
    parsed = JSON.parse(data.join("\n"));
  } catch {
    return { kind: "unreadable" };
  }

  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return { kind: "unreadable" };
  }
  const type = (parsed as { type?: unknown }).type;
  // An event type this build does not know about is dropped rather than
  // counted as damage: a newer Mac adding one is not a fault of this stream.
  if (typeof type !== "string") return { kind: "unreadable" };
  if (!KNOWN_TYPES.has(type)) return { kind: "empty" };
  return { kind: "event", event: parsed as ChatEvent };
}

/**
 * Byte chunks in, chat events out. Holds the partial tail between calls.
 *
 * One instance per turn. Reusing one across turns would carry a truncated
 * frame from a dropped connection into the next reply.
 */
export class ChatStreamParser {
  private buffer = "";
  private readonly decoder = new TextDecoder();
  private malformed = 0;
  private truncated = false;

  /** Frames that arrived complete but could not be read as an event. */
  get malformedFrames(): number {
    return this.malformed;
  }

  /** True when the stream ended mid-frame — i.e. the connection was cut. */
  get endedMidFrame(): boolean {
    return this.truncated;
  }

  /**
   * Feed one chunk. Accepts a string so tests (and the non-streaming
   * fallback in `lib/chat.ts`) can drive it without constructing bytes.
   */
  push(chunk: Uint8Array | string): ChatEvent[] {
    this.buffer +=
      typeof chunk === "string" ? chunk : this.decoder.decode(chunk, { stream: true });
    return this.drain();
  }

  /**
   * End of stream. Flushes the decoder and DISCARDS any trailing partial
   * frame: the Mac terminates every frame with a blank line, so anything left
   * in the buffer is by definition an incomplete write and parsing it would
   * mean guessing at a truncated JSON object.
   */
  flush(): ChatEvent[] {
    this.buffer += this.decoder.decode();
    const events = this.drain();
    if (this.buffer.trim() !== "") this.truncated = true;
    this.buffer = "";
    return events;
  }

  private drain(): ChatEvent[] {
    const parts = this.buffer.split(FRAME_SEPARATOR);
    // The last part is either "" (the buffer ended on a boundary) or the start
    // of a frame that has not finished arriving. Either way it is not ours yet.
    this.buffer = parts.pop() ?? "";
    const events: ChatEvent[] = [];
    for (const frame of parts) {
      if (frame.trim() === "") continue;
      const parsed = parseFrame(frame);
      if (parsed.kind === "unreadable") this.malformed += 1;
      else if (parsed.kind === "event") events.push(parsed.event);
    }
    return events;
  }
}

/**
 * The minimum a `ReadableStream` reader has to look like for us to read it.
 *
 * Declared structurally rather than as `ReadableStreamDefaultReader` so this
 * module stays free of DOM lib assumptions and so a test can hand it an array
 * of chunks. `lib/chat.ts` passes `response.body.getReader()`, which satisfies
 * it exactly.
 */
export interface ChunkReader {
  read(): Promise<{ done: boolean; value?: Uint8Array | undefined }>;
  cancel?: (reason?: unknown) => Promise<unknown> | unknown;
}

/**
 * Drive a reader to completion, yielding events as they land.
 *
 * Stops at the first `done` event — the Mac sends exactly one and nothing
 * after it, so continuing to read would only hold the socket open — and
 * cancels the reader on the way out, including when the consumer breaks out
 * of the loop early (a `for await` that `break`s runs this generator's
 * `finally`, which is how the Stop button releases the connection).
 */
export async function* readChatStream(
  reader: ChunkReader,
  parser: ChatStreamParser = new ChatStreamParser(),
): AsyncGenerator<ChatEvent> {
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (value === undefined) continue;
      for (const evt of parser.push(value)) {
        yield evt;
        if (evt.type === "done") return;
      }
    }
    for (const evt of parser.flush()) {
      yield evt;
      if (evt.type === "done") return;
    }
  } finally {
    try {
      await reader.cancel?.();
    } catch {
      // A reader that is already closed rejects here on some implementations.
      // There is nothing left to release and nothing useful to tell the user.
    }
  }
}
