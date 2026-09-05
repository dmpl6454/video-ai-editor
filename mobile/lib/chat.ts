/**
 * Talking to the Mac's agent loop.
 *
 * WHY THIS DOES NOT GO THROUGH `ApiClient.request`. That method is built for
 * JSON round trips: it buffers the whole body, it applies a timeout, and it
 * returns a parsed object. A chat turn is none of those things — it streams
 * for as long as Claude keeps writing, which for a turn that runs three tools
 * is comfortably past any sensible timeout, and the whole point is to show the
 * words as they arrive. So this module makes the request itself and borrows
 * the two things from `ApiClient` that must not be reimplemented: the origin,
 * and the credential.
 *
 * THE HEADERS ARE NOT OPTIONAL. `X-VAE-Client: 1` is a security control, not a
 * label — it is a header no plain form or image tag can set, so its presence
 * forces a CORS preflight the Mac's origin allowlist then denies, which is
 * what stops a web page the user happens to be visiting from driving their
 * editor. The bearer is what proves this phone is paired. Both must be here
 * for the same reasons `lib/api.ts` explains at length.
 *
 * THE NON-STREAMING FALLBACK IS REAL, NOT DEFENSIVE PADDING. `expo/fetch`
 * gives us a `ReadableStream` body on a dev or release build; there is no
 * simulator on the machine this ships from, so "it definitely streams
 * everywhere" is not something anyone here can claim. When `body` is null the
 * turn is read as one buffered string and pushed through the SAME parser, so
 * the user gets the whole answer at once instead of nothing at all. It is a
 * worse experience and an honest one.
 *
 * RECOVERY IS ALWAYS `history`, NEVER A REPLAY. `main.py` persists the
 * transcript in a `finally`, so a turn whose connection dropped mid-answer is
 * already on the Mac's disk — including any edits its tools applied. Sending
 * the same message again would run those tools a second time. The screen
 * refetches history instead; `lib/chatLog.ts::historyToMessages` turns it back
 * into a transcript.
 */

import { fetch as streamingFetch } from "expo/fetch";

import type { ApiClient } from "./api";
import { apiErrorFromResponse, apiErrorFromThrow } from "./errors";
import { ChatStreamParser, readChatStream, type ChatEvent } from "./sse";

export interface ChatTurnRequest {
  client: ApiClient;
  sessionId: string;
  message: string;
  /** Aborts the turn. The Mac keeps working — see the Stop copy in
   *  `app/chat.tsx` — this only stops us listening. */
  signal?: AbortSignal;
}

function chatHeaders(client: ApiClient): Record<string, string> {
  const headers: Record<string, string> = {
    "content-type": "application/json",
    accept: "text/event-stream",
    "X-VAE-Client": "1",
  };
  if (client.token) headers.authorization = `Bearer ${client.token}`;
  return headers;
}

/**
 * Run one chat turn, yielding events as the Mac produces them.
 *
 * Throws an `ApiError` for anything that goes wrong at the transport level —
 * a refused request, a revoked token, a Mac that stopped answering mid-stream
 * — so the screen can tell "the connection died" apart from "the agent said
 * it could not do that", which arrives as an `error` EVENT and is a completely
 * different message to show.
 */
export async function* streamChatTurn(req: ChatTurnRequest): AsyncGenerator<ChatEvent> {
  const { client, sessionId, message, signal } = req;

  let res: Awaited<ReturnType<typeof streamingFetch>>;
  try {
    res = await streamingFetch(`${client.baseUrl}/api/sessions/${sessionId}/chat`, {
      method: "POST",
      headers: chatHeaders(client),
      // `selection`, `multi_selection` and `playhead` are deliberately not
      // sent. They let Claude bind "this clip" and "here" to real ids, and the
      // phone's shared store holds none of them — sending zeros would tell it
      // the playhead is at the start of the project when it is not.
      body: JSON.stringify({ message }),
      ...(signal ? { signal } : {}),
    });
  } catch (e) {
    throw apiErrorFromThrow(e);
  }

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw apiErrorFromResponse(res.status, text, {
      requestId: res.headers.get("x-request-id"),
      retryAfter: res.headers.get("retry-after"),
    });
  }

  const parser = new ChatStreamParser();
  const body = res.body;

  if (body === null) {
    let text: string;
    try {
      text = await res.text();
    } catch (e) {
      throw apiErrorFromThrow(e);
    }
    for (const evt of parser.push(text)) {
      yield evt;
      if (evt.type === "done") return;
    }
    for (const evt of parser.flush()) {
      yield evt;
      if (evt.type === "done") return;
    }
    return;
  }

  try {
    yield* readChatStream(body.getReader(), parser);
  } catch (e) {
    throw apiErrorFromThrow(e);
  }
}

/** The persisted transcript for a session, as the Mac stores it. */
export async function fetchChatHistory(client: ApiClient, sessionId: string): Promise<unknown> {
  const res = await client.request<{ history: unknown }>(
    "GET",
    `/api/sessions/${sessionId}/history`,
  );
  return res.history;
}
