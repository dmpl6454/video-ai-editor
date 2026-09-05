/**
 * Turning a failed request into something a person can act on.
 *
 * Two things had to be ported carefully from the desktop:
 *
 * 1. `api/hardening.py::_envelope` hardcodes `message = "request failed"` for
 *    EVERY HTTPException whose detail is a dict, and hides the real sentence
 *    under `error.details.message`. Reading `error.message` first — which the
 *    desktop store did until it was fixed — means a carefully written
 *    server-side explanation can never reach the user. The ordering in
 *    `messageFromBody` is therefore load-bearing: details.message, THEN
 *    error.message, THEN FastAPI's `detail`.
 *
 * 2. `api/jobs.py` records a failure as `f"{type(e).__name__}: {e}"`, so an
 *    async upscale that refuses a text clip arrives as "RuntimeError: upscale
 *    only supports media clips". The class name is noise to an editor.
 *
 * On the phone there is a third concern the desktop never had: the request can
 * fail before it reaches the Mac at all, and "TypeError: Network request
 * failed" is the same string whether the Mac is asleep, the phone is on a
 * different Wi-Fi, or iOS silently blocked the local-network access. `kind`
 * exists so the connection reducer can tell those apart instead of guessing;
 * `lib/net.ts` does the actual telling.
 */

export type ApiErrorKind =
  /** The request never got an HTTP response. Cause is genuinely ambiguous. */
  | "network"
  /** The bearer token is missing, wrong, or revoked. */
  | "unauthorized"
  /**
   * 401 because the Mac has temporarily stopped listening to this device after
   * too many bad tokens. Distinct from `unauthorized` because re-sending the
   * same good token will keep failing until the window expires — a client that
   * treats it as a normal 401 will sit in a retry loop behind a green banner.
   */
  | "auth_lockout"
  /** 403 — the Mac understood us and said no (an out-of-session media path). */
  | "forbidden"
  /** 404 — including a job id the Mac has already pruned. */
  | "not_found"
  /** 413 — the upload is bigger than the Mac will accept. */
  | "too_large"
  /** 421 — the Mac refused the Host header we sent. */
  | "host_rejected"
  /** 422 — the Mac understood the request and rejected its contents. */
  | "rejected"
  /** 429 — too many requests too fast. */
  | "rate_limited"
  /** 5xx. */
  | "server"
  /** The caller aborted, or a paused download resolved to nothing. */
  | "cancelled"
  /** We gave up waiting. */
  | "timeout";

export interface ApiErrorInit {
  kind: ApiErrorKind;
  message: string;
  status?: number | null;
  /** `X-Request-ID` from api/hardening.py, so a report can be matched to a log. */
  requestId?: string | null;
  retryAfterMs?: number | null;
  cause?: unknown;
}

/** Every failure that leaves `lib/api.ts` is one of these. */
export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status: number | null;
  readonly requestId: string | null;
  readonly retryAfterMs: number | null;

  constructor(init: ApiErrorInit) {
    super(init.message, init.cause === undefined ? undefined : { cause: init.cause });
    this.name = "ApiError";
    this.kind = init.kind;
    this.status = init.status ?? null;
    this.requestId = init.requestId ?? null;
    this.retryAfterMs = init.retryAfterMs ?? null;
  }
}

export function isApiError(e: unknown): e is ApiError {
  return e instanceof ApiError;
}

/** api/jobs.py prefixes a job failure with the exception class. Drop it. */
export function stripExceptionPrefix(msg: string): string {
  return msg.replace(/^\w+(Error|Exception): /, "");
}

/**
 * Pull the human sentence out of a backend error body. See note 1 in the file
 * header: `details.message` MUST be read before `error.message`.
 */
export function messageFromBody(body: unknown): string | null {
  if (typeof body !== "object" || body === null) return null;
  const b = body as {
    error?: { message?: unknown; details?: unknown };
    detail?: unknown;
  };

  const details = b.error?.details;
  if (details && typeof details === "object" && !Array.isArray(details)) {
    const dm = (details as { message?: unknown; error?: unknown }).message
      ?? (details as { error?: unknown }).error;
    if (typeof dm === "string" && dm.trim()) return stripExceptionPrefix(dm);
  }

  if (typeof b.error?.message === "string" && b.error.message.trim()) {
    return stripExceptionPrefix(b.error.message);
  }

  if (typeof b.detail === "string" && b.detail.trim()) return stripExceptionPrefix(b.detail);
  if (b.detail && typeof b.detail === "object") {
    const d = b.detail as { message?: unknown; error?: unknown };
    const dm = typeof d.message === "string" ? d.message : typeof d.error === "string" ? d.error : null;
    if (dm && dm.trim()) return stripExceptionPrefix(dm);
  }

  return null;
}

/** Same idea, but starting from a raw response body that may not be JSON. */
export function messageFromText(text: string): string | null {
  const trimmed = text.trim();
  if (!trimmed) return null;
  const start = trimmed.indexOf("{");
  if (start !== -1) {
    try {
      const parsed: unknown = JSON.parse(trimmed.slice(start));
      const msg = messageFromBody(parsed);
      if (msg) return msg;
    } catch {
      // Not JSON after all — fall through and use the text as written.
    }
  }
  // A raw HTML error page would be a wall of markup in a toast; refuse it.
  if (trimmed.startsWith("<")) return null;
  return stripExceptionPrefix(trimmed.slice(0, 300));
}

/**
 * The fallback sentence for a status code, used only when the Mac sent no
 * message of its own. Each one names the Mac, because on the phone "failed"
 * without a subject reads as "the app is broken".
 */
const STATUS_FALLBACK: Record<ApiErrorKind, string> = {
  network: "Could not reach your Mac.",
  unauthorized: "Your Mac no longer recognises this phone. Pair it again.",
  auth_lockout: "Your Mac has paused sign-in attempts for a minute. It will accept this phone again shortly.",
  forbidden: "Your Mac refused that — the file is outside this project.",
  not_found: "Your Mac does not have that any more.",
  too_large: "That file is larger than your Mac is set to accept.",
  host_rejected: "Your Mac refused the address this phone used to reach it.",
  rejected: "Your Mac could not carry that out.",
  rate_limited: "Too many requests at once. Slowing down.",
  server: "Something went wrong on your Mac.",
  cancelled: "Cancelled.",
  timeout: "Your Mac did not answer in time.",
};

/** 401 bodies from the pairing middleware mark a lockout with this code. */
const LOCKOUT_MARKERS = ["auth_lockout", "too many failed"];

export function kindForStatus(status: number, body: string): ApiErrorKind {
  if (status === 401) {
    const haystack = body.toLowerCase();
    return LOCKOUT_MARKERS.some((m) => haystack.includes(m)) ? "auth_lockout" : "unauthorized";
  }
  if (status === 403) return "forbidden";
  if (status === 404) return "not_found";
  if (status === 413) return "too_large";
  if (status === 421) return "host_rejected";
  if (status === 422) return "rejected";
  if (status === 429) return "rate_limited";
  if (status >= 500) return "server";
  // 400, 405, 409 … the Mac understood us and said no. "rejected" is the
  // honest bucket; the body almost always carries the real sentence.
  return "rejected";
}

/** `Retry-After` is seconds (an integer) per RFC 9110; the limiter sends that. */
export function retryAfterMs(header: string | null): number | null {
  if (!header) return null;
  const secs = Number(header.trim());
  if (!Number.isFinite(secs) || secs < 0) return null;
  return Math.min(secs, 300) * 1000;
}

/** Build the ApiError for a non-OK HTTP response. */
export function apiErrorFromResponse(
  status: number,
  body: string,
  opts: { requestId?: string | null; retryAfter?: string | null } = {},
): ApiError {
  const kind = kindForStatus(status, body);
  return new ApiError({
    kind,
    message: messageFromText(body) ?? STATUS_FALLBACK[kind],
    status,
    requestId: opts.requestId ?? null,
    retryAfterMs: retryAfterMs(opts.retryAfter ?? null),
  });
}

/**
 * The messages an abort actually arrives with, per transport.
 *
 * WHY A MESSAGE LIST AND NOT A NAME OR A CLASS CHECK — this is the whole bug:
 *
 * This guard used to be `e.name === "AbortError" || e.message === "Aborted"`,
 * which is the shape React Native's whatwg-fetch polyfill produces. But expo
 * REPLACES the global fetch: `expo/src/winter/runtime.native.ts` runs
 * `install('fetch', …)` unconditionally unless `EXPO_PUBLIC_USE_RN_FETCH` is
 * set, and this app does not set it. expo/fetch rejects with a `FetchError`
 * (expo/src/winter/fetch/FetchErrors.ts) whose constructor never assigns
 * `this.name`, so `e.name` is the INHERITED "Error" — and it rewrites the
 * message to `fetch failed: <original>`. Running that real class gives:
 *
 *   name "Error", message "fetch failed: Fetch request has been canceled"
 *   name "Error", message "fetch failed: The operation was aborted."
 *
 * Neither old guard matched either one, so every abort fell through to
 * `kind: "network"` — which IS in `connection.ts::CONNECTION_KINDS`. A user
 * tapping Stop therefore knocked the connection to retrying/unreachable,
 * cleared the media token (killing the player and every thumbnail), and got a
 * red "Could not reach your Mac." banner for something they chose to do.
 *
 * `e.constructor.name === "FetchError"` is deliberately NOT used: Metro's
 * release minifier mangles class names, so it would pass in Jest and fail in
 * the production binary — precisely the class of defect this file already
 * shipped once. Checked, not assumed: `expo export --platform ios` produces a
 * Hermes bundle that contains the literals "fetch failed: " and "The operation
 * was aborted" but NOT the bare identifier `FetchError`. Message text survives
 * minification; the class identity does not.
 *
 * One more thing that check turned up, and which explains the split below:
 * "Fetch request has been canceled" is absent from the JS bundle entirely,
 * because it is not a JS string. It is the `reason` on
 * `FetchRequestCanceledException` in expo/ios/Fetch/FetchExceptions.swift (and
 * its Android twin), crosses the bridge at runtime, and only then gets the
 * "fetch failed: " prefix from FetchError. So the full message is assembled
 * from a native half and a JS half and can only be matched as text.
 */
const ABORT_MESSAGES: readonly RegExp[] = [
  // React Native's whatwg-fetch polyfill, and the DOMException an
  // AbortController raises directly. Kept because both are still reachable:
  // EXPO_PUBLIC_USE_RN_FETCH=1 restores RN's fetch, and expo's own
  // AbortSignal patch throws DOMExceptions.
  /^aborted$/i,
  /^fetch is aborted$/i,
  // expo/fetch, signal-already-aborted path (fetch.ts) and DOMException's
  // standard reason. Arrives as "fetch failed: The operation was aborted."
  /\bthe operation was aborted\b/i,
  // expo/fetch, native cancel path. ios/Fetch/FetchExceptions.swift and its
  // Android twin both say "Fetch request has been canceled" (one 'l'); the
  // second spelling is defensive, not observed.
  /\bfetch request has been cancell?ed\b/i,
];

/**
 * Last-resort abort detection, used only when the caller could not tell us.
 * `lib/chat.ts` and `lib/upload.ts` drive their own fetches and call
 * `apiErrorFromThrow` with no context, so this path still has to work.
 */
function looksLikeAbort(e: unknown): boolean {
  if (!(e instanceof Error)) return false;
  if (e.name === "AbortError") return true;
  return ABORT_MESSAGES.some((re) => re.test(e.message));
}

function timeoutError(cause: unknown, timeoutMs: number | undefined): ApiError {
  const secs = timeoutMs === undefined ? null : Math.round(timeoutMs / 1000);
  return new ApiError({
    kind: "timeout",
    message:
      secs === null
        ? STATUS_FALLBACK.timeout
        : `Your Mac did not answer within ${secs} second${secs === 1 ? "" : "s"}.`,
    cause,
  });
}

function cancelledError(cause: unknown): ApiError {
  return new ApiError({ kind: "cancelled", message: STATUS_FALLBACK.cancelled, cause });
}

/** What the caller knows that the error object cannot say for itself. */
export interface ThrowContext {
  /**
   * The transport's OWN deadline fired. Only `ApiClient.request` can know
   * this, because both a timeout and a cancel abort the same controller.
   */
  timedOut?: boolean;
  /** How long that deadline was, so the sentence can name it. */
  timeoutMs?: number;
  /**
   * The CALLER's `AbortSignal` is aborted — i.e. a person tapped Stop. This is
   * the failure the user asked for and must stay out of the connection kinds.
   */
  cancelled?: boolean;
}

/**
 * Build the ApiError for a request that never produced a response.
 *
 * INTENT IS CHECKED BEFORE SHAPE, and that ordering is the fix. Whether an
 * abort was a timeout or a cancel is a fact about the CALLER, not about the
 * error — the two are indistinguishable once fetch has rejected, and they mean
 * opposite things: a cancel is shown neutrally and leaves the connection
 * alone, while a timeout is a Mac that is not answering and MUST move the
 * connection state. `ApiClient.request` already tracks both, so it is asked
 * first; `looksLikeAbort` is only the fallback for callers that cannot say.
 */
export function apiErrorFromThrow(e: unknown, opts: ThrowContext = {}): ApiError {
  if (isApiError(e)) return e;
  if (opts.timedOut) return timeoutError(e, opts.timeoutMs);
  if (opts.cancelled) return cancelledError(e);
  if (looksLikeAbort(e)) return cancelledError(e);
  return new ApiError({ kind: "network", message: STATUS_FALLBACK.network, cause: e });
}

/** The sentence to show a user for anything at all. Never returns "". */
export function errorMessage(e: unknown): string {
  if (isApiError(e)) return e.message;
  if (e instanceof Error) return stripExceptionPrefix(e.message) || STATUS_FALLBACK.server;
  const s = String(e).trim();
  return s ? stripExceptionPrefix(s) : STATUS_FALLBACK.server;
}

/** A cancel is the one failure the user asked for; it is shown neutrally. */
export function isCancellation(e: unknown): boolean {
  if (isApiError(e)) return e.kind === "cancelled";
  return e instanceof Error && / was cancelled$/.test(e.message);
}
