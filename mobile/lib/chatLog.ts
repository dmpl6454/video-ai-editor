/**
 * The chat transcript, as a value.
 *
 * Everything in this file is pure: a list of messages in, a new list out.
 * That is deliberate. The Mac streams six kinds of event at us, three of them
 * mutate a message that is already on screen (text deltas accumulate, a tool
 * result attaches to the call it belongs to), and the reply arrives while the
 * user is scrolling. Working that out inside a React component is how you get
 * a transcript that loses the last sentence of every answer. Here it is a
 * reducer with tests.
 *
 * THE TOOL PAIRING IS BY ID, NOT BY NAME. `agent/loop.py` emits `tool_use` and
 * `tool_result` carrying the same Anthropic block id, and the desktop pairs
 * them by searching backwards for the most recent unresolved call with a
 * matching NAME — which mismatches the moment a turn runs the same tool twice,
 * something `apply_export_preset` then `set_loudness_target` then
 * `apply_export_preset` does routinely. Matching on the id is both simpler and
 * correct.
 *
 * A NEW ASSISTANT BUBBLE AFTER EVERY TOOL CALL. Claude narrates, calls a tool,
 * then narrates again; appending the second narration to the first would
 * produce one paragraph that reads as though the tool ran in the middle of a
 * sentence. Each run of text deltas between tool calls is its own bubble.
 */

import type { ChatEvent } from "./sse";

export type ChatMessage =
  | { kind: "user"; seq: number; text: string }
  | { kind: "assistant"; seq: number; text: string }
  | {
      kind: "tool";
      seq: number;
      callId: string;
      tool: string;
      args: Record<string, unknown>;
      /** Absent until the `tool_result` for this call arrives. */
      result?: unknown;
      ok?: boolean;
    }
  /** Something the app is telling the user, not something Claude said: a
   *  missing API key, a dropped connection, an unreadable frame. */
  | { kind: "notice"; seq: number; tone: "warn" | "danger"; text: string };

export type ChatRole = ChatMessage["kind"];

function nextSeq(messages: readonly ChatMessage[]): number {
  const last = messages[messages.length - 1];
  return last === undefined ? 0 : last.seq + 1;
}

export function userMessage(messages: readonly ChatMessage[], text: string): ChatMessage[] {
  return [...messages, { kind: "user", seq: nextSeq(messages), text }];
}

export function noticeMessage(
  messages: readonly ChatMessage[],
  tone: "warn" | "danger",
  text: string,
): ChatMessage[] {
  return [...messages, { kind: "notice", seq: nextSeq(messages), tone, text }];
}

/**
 * Append a text delta to the assistant bubble that is currently being written,
 * or open a new one.
 *
 * "Currently being written" means the LAST message is an assistant bubble —
 * not merely that one exists somewhere above. A tool call in between ends the
 * bubble, which is what gives the transcript its shape.
 */
function appendDelta(messages: readonly ChatMessage[], text: string): ChatMessage[] {
  const last = messages[messages.length - 1];
  if (last !== undefined && last.kind === "assistant") {
    return [...messages.slice(0, -1), { ...last, text: last.text + text }];
  }
  return [...messages, { kind: "assistant", seq: nextSeq(messages), text }];
}

function attachResult(
  messages: readonly ChatMessage[],
  callId: string,
  result: unknown,
  ok: boolean,
): ChatMessage[] {
  const idx = messages.findIndex((m) => m.kind === "tool" && m.callId === callId);
  if (idx === -1) {
    // A result with no call. The Mac does not do this, but a dropped frame
    // could produce it, and silently discarding the only evidence that a tool
    // ran would be worse than showing a row with no arguments.
    return [
      ...messages,
      { kind: "tool", seq: nextSeq(messages), callId, tool: "(unknown)", args: {}, result, ok },
    ];
  }
  const target = messages[idx];
  if (target === undefined || target.kind !== "tool") return [...messages];
  return [...messages.slice(0, idx), { ...target, result, ok }, ...messages.slice(idx + 1)];
}

/**
 * Fold one streamed event into the transcript.
 *
 * `done` produces no message — it is the caller's signal to stop, not
 * something the user needs to read — and an unrecognised event is returned
 * unchanged rather than dropped noisily, because `lib/sse.ts` has already
 * refused anything that is not a known type.
 */
export function applyChatEvent(messages: readonly ChatMessage[], evt: ChatEvent): ChatMessage[] {
  switch (evt.type) {
    case "text_delta":
      return appendDelta(messages, evt.text);
    case "tool_use":
      return [
        ...messages,
        { kind: "tool", seq: nextSeq(messages), callId: evt.id, tool: evt.name, args: evt.args },
      ];
    case "tool_result":
      return attachResult(messages, evt.id, evt.result, evt.is_error !== true);
    case "error":
      return noticeMessage(messages, "danger", evt.message);
    // An `op` changes the project rather than the transcript; the screen
    // refreshes the EDL when it sees one. `done` ends the turn.
    case "op":
    case "done":
      return [...messages];
  }
}

/** One short line describing a tool call, for the collapsed row. */
export function toolCallLine(tool: string, args: Record<string, unknown>): string {
  const parts = Object.entries(args).map(([k, v]) => `${k}=${summariseValue(v)}`);
  return parts.length === 0 ? tool : `${tool}(${parts.join(", ")})`;
}

function summariseValue(v: unknown): string {
  if (typeof v === "string") return v.length > 24 ? `“${v.slice(0, 24)}…”` : `“${v}”`;
  if (v === null) return "null";
  if (Array.isArray(v)) return `[${v.length}]`;
  if (typeof v === "object") return "{…}";
  return String(v);
}

// ---------------------------------------------------------------------------
// History
// ---------------------------------------------------------------------------

/**
 * `GET /api/sessions/{sid}/history` returns the raw Anthropic message list the
 * Mac persists in `chat.json`. Its `content` is a string on the messages the
 * backend wrote by hand and a block list on the ones the SDK produced, so both
 * shapes have to be handled.
 *
 * This matters more than it looks: `main.py` saves the history in a `finally`,
 * so a turn whose connection dropped mid-answer IS on disk. Reloading history
 * is the recovery path for every stream failure, which is why the screen calls
 * it on mount and after a drop rather than asking the user to send again.
 */
export function historyToMessages(history: unknown): ChatMessage[] {
  if (!Array.isArray(history)) return [];
  let messages: ChatMessage[] = [];
  for (const raw of history) {
    if (typeof raw !== "object" || raw === null) continue;
    const entry = raw as { role?: unknown; content?: unknown };
    const role = entry.role === "assistant" ? "assistant" : entry.role === "user" ? "user" : null;
    if (role === null) continue;
    messages = appendHistoryEntry(messages, role, entry.content);
  }
  return messages;
}

function appendHistoryEntry(
  messages: ChatMessage[],
  role: "user" | "assistant",
  content: unknown,
): ChatMessage[] {
  if (typeof content === "string") {
    const text = content.trim();
    if (text === "") return messages;
    return [...messages, { kind: role, seq: nextSeq(messages), text }];
  }
  if (!Array.isArray(content)) return messages;

  let out = messages;
  for (const block of content) {
    if (typeof block !== "object" || block === null) continue;
    const b = block as {
      type?: unknown;
      text?: unknown;
      id?: unknown;
      name?: unknown;
      input?: unknown;
      tool_use_id?: unknown;
      content?: unknown;
      is_error?: unknown;
    };
    if (b.type === "text" && typeof b.text === "string" && b.text.trim() !== "") {
      out = [...out, { kind: role, seq: nextSeq(out), text: b.text }];
    } else if (b.type === "tool_use" && typeof b.id === "string" && typeof b.name === "string") {
      const args = typeof b.input === "object" && b.input !== null && !Array.isArray(b.input)
        ? (b.input as Record<string, unknown>)
        : {};
      out = [...out, { kind: "tool", seq: nextSeq(out), callId: b.id, tool: b.name, args }];
    } else if (b.type === "tool_result" && typeof b.tool_use_id === "string") {
      out = attachResult(out, b.tool_use_id, b.content, b.is_error !== true);
    }
  }
  return out;
}
