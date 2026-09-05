/**
 * The error envelope. The ordering assertion in "prefers details.message" is
 * the one that matters most: the Mac hardcodes `message: "request failed"` for
 * every HTTPException whose detail is a dict, so reading `error.message` first
 * means a real server-side explanation can never reach a user.
 */

// The REAL class expo/fetch rejects with. Imported rather than re-typed so the
// abort fixtures below are derived from the shipping transport, not from a
// belief about it — believing the RN shape is what put this defect in a build.
import { FetchError } from "expo/src/winter/fetch/FetchErrors";

import { isConnectionFailure } from "../../lib/connection";
import {
  ApiError,
  apiErrorFromResponse,
  apiErrorFromThrow,
  errorMessage,
  isApiError,
  isCancellation,
  kindForStatus,
  messageFromBody,
  messageFromText,
  retryAfterMs,
  stripExceptionPrefix,
} from "../../lib/errors";

describe("stripExceptionPrefix", () => {
  it("drops the class name api/jobs.py records", () => {
    expect(stripExceptionPrefix("RuntimeError: upscale only supports media clips")).toBe(
      "upscale only supports media clips",
    );
    expect(stripExceptionPrefix("ValueError: bad")).toBe("bad");
  });

  it("leaves a plain sentence alone", () => {
    expect(stripExceptionPrefix("no clip on v1 to caption")).toBe("no clip on v1 to caption");
    // A colon that is not an exception prefix must survive.
    expect(stripExceptionPrefix("clip v1: nothing there")).toBe("clip v1: nothing there");
  });
});

describe("messageFromBody", () => {
  it("prefers details.message over the envelope's sentinel", () => {
    const body = {
      error: {
        message: "request failed",
        details: { message: "a cached overlay image was corrupted; your media is fine" },
      },
    };
    expect(messageFromBody(body)).toBe("a cached overlay image was corrupted; your media is fine");
  });

  it("falls back to error.message when there are no details", () => {
    expect(messageFromBody({ error: { message: "rate limited" } })).toBe("rate limited");
  });

  it("reads FastAPI's string detail", () => {
    expect(messageFromBody({ detail: "not a video file" })).toBe("not a video file");
  });

  it("reads FastAPI's dict detail", () => {
    expect(messageFromBody({ detail: { error: "unsupported container" } })).toBe("unsupported container");
  });

  it("strips an exception prefix wherever the sentence came from", () => {
    expect(messageFromBody({ error: { details: { message: "RuntimeError: nope" } } })).toBe("nope");
  });

  it("returns null for anything with no sentence in it", () => {
    expect(messageFromBody(null)).toBeNull();
    expect(messageFromBody("a string")).toBeNull();
    expect(messageFromBody({})).toBeNull();
    expect(messageFromBody({ error: { message: "   " } })).toBeNull();
  });
});

describe("messageFromText", () => {
  it("finds the envelope inside a wrapped error string", () => {
    const raw = '422 Unprocessable Entity: {"error":{"message":"request failed","details":{"message":"no clip on v1"}}}';
    expect(messageFromText(raw)).toBe("no clip on v1");
  });

  it("uses plain text when the body is not JSON", () => {
    expect(messageFromText("Internal Server Error")).toBe("Internal Server Error");
  });

  it("refuses an HTML error page rather than putting markup in a toast", () => {
    expect(messageFromText("<html><body>502 Bad Gateway</body></html>")).toBeNull();
  });

  it("returns null for an empty body", () => {
    expect(messageFromText("")).toBeNull();
    expect(messageFromText("   ")).toBeNull();
  });

  it("truncates a runaway body", () => {
    const msg = messageFromText("x".repeat(5_000));
    expect(msg).not.toBeNull();
    expect((msg as string).length).toBeLessThanOrEqual(300);
  });
});

describe("kindForStatus", () => {
  it("maps each status the Mac actually returns", () => {
    expect(kindForStatus(401, "")).toBe("unauthorized");
    expect(kindForStatus(403, "")).toBe("forbidden");
    expect(kindForStatus(404, "")).toBe("not_found");
    expect(kindForStatus(413, "")).toBe("too_large");
    expect(kindForStatus(421, "")).toBe("host_rejected");
    expect(kindForStatus(422, "")).toBe("rejected");
    expect(kindForStatus(429, "")).toBe("rate_limited");
    expect(kindForStatus(500, "")).toBe("server");
    expect(kindForStatus(503, "")).toBe("server");
    expect(kindForStatus(400, "")).toBe("rejected");
  });

  it("separates a lockout from an ordinary rejected token", () => {
    // A client that retries through a lockout only extends it, so this is not
    // cosmetic: the two states take different actions.
    expect(kindForStatus(401, '{"error":{"code":"auth_lockout"}}')).toBe("auth_lockout");
    expect(kindForStatus(401, "Too many failed attempts")).toBe("auth_lockout");
    expect(kindForStatus(401, '{"error":{"message":"bad token"}}')).toBe("unauthorized");
  });
});

describe("retryAfterMs", () => {
  it("reads the header as seconds", () => {
    expect(retryAfterMs("30")).toBe(30_000);
    expect(retryAfterMs(" 2 ")).toBe(2_000);
  });

  it("caps an absurd value and refuses a non-number", () => {
    expect(retryAfterMs("100000")).toBe(300_000);
    expect(retryAfterMs("soon")).toBeNull();
    expect(retryAfterMs("-5")).toBeNull();
    expect(retryAfterMs(null)).toBeNull();
  });
});

describe("apiErrorFromResponse", () => {
  it("carries the Mac's own sentence when there is one", () => {
    const e = apiErrorFromResponse(422, '{"error":{"details":{"message":"no clip on v1 to caption"}}}');
    expect(e.kind).toBe("rejected");
    expect(e.message).toBe("no clip on v1 to caption");
    expect(e.status).toBe(422);
  });

  it("falls back to a sentence that names the Mac", () => {
    const e = apiErrorFromResponse(500, "");
    expect(e.message).toMatch(/your Mac/i);
  });

  it("keeps the request id and retry-after for a bug report", () => {
    const e = apiErrorFromResponse(429, "", { requestId: "req-7", retryAfter: "5" });
    expect(e.requestId).toBe("req-7");
    expect(e.retryAfterMs).toBe(5_000);
  });
});

describe("apiErrorFromThrow", () => {
  it("passes an ApiError straight through", () => {
    const original = new ApiError({ kind: "not_found", message: "gone" });
    expect(apiErrorFromThrow(original)).toBe(original);
  });

  it("reads React Native's whatwg-fetch abort as a cancellation", () => {
    // The shape this file was originally written against. It is NOT what runs
    // in this app (see the expo/fetch cases below) but it is still reachable —
    // EXPO_PUBLIC_USE_RN_FETCH=1 restores RN's fetch — so it must keep working.
    const abort = new Error("Aborted");
    abort.name = "AbortError";
    expect(apiErrorFromThrow(abort).kind).toBe("cancelled");
  });

  describe("expo/fetch, which is the fetch this app actually runs", () => {
    /**
     * These are not hand-written guesses. `expo/src/winter/runtime.native.ts`
     * replaces the global fetch with expo's own unless EXPO_PUBLIC_USE_RN_FETCH
     * is set (this app does not set it), and expo/fetch rejects through the
     * REAL `FetchError` imported below — a class that never assigns
     * `this.name` and rewrites the message as "fetch failed: <original>".
     *
     * Constructing them through the real class rather than typing the strings
     * out is the point: if expo changes either the wrapper or the wording, this
     * suite fails here instead of the app silently regressing to `network` on
     * device. Asserting the derived shape first is what proves the fixture is
     * still the production shape and not a stale copy of it.
     */
    // `name` stays "Error" because FetchError's constructor never sets it —
    // this is exactly why the old `e.name === "AbortError"` guard was dead.
    const nativeCancel = FetchError.createFromError(
      // ios/Fetch/FetchExceptions.swift and its Android twin both raise this.
      new Error("Fetch request has been canceled"),
    );
    // expo/src/winter/fetch/fetch.ts throws this when the signal is already
    // aborted before the request starts.
    const preAborted = new FetchError("The operation was aborted.", { cause: "user" });

    it("has the shape that defeated the old name/message guards", () => {
      expect(nativeCancel.name).toBe("Error");
      expect(nativeCancel.message).toBe("fetch failed: Fetch request has been canceled");
      expect(preAborted.name).toBe("Error");
      expect(preAborted.message).toBe("fetch failed: The operation was aborted.");
      for (const e of [nativeCancel, preAborted]) {
        expect(e.name === "AbortError" || e.message === "Aborted").toBe(false);
      }
    });

    it("still classifies a native cancel as a cancellation", () => {
      expect(apiErrorFromThrow(nativeCancel).kind).toBe("cancelled");
    });

    it("still classifies an already-aborted signal as a cancellation", () => {
      expect(apiErrorFromThrow(preAborted).kind).toBe("cancelled");
    });

    it("does NOT rely on the class name, which Metro mangles in release", () => {
      // A minified release build renames `FetchError` to something like `n`.
      // Reproduce that by keeping the message and losing the identity: the
      // classification must be unchanged.
      class MinifiedFetchError extends Error {}
      Object.defineProperty(MinifiedFetchError, "name", { value: "n" });
      const minified = new MinifiedFetchError("fetch failed: Fetch request has been canceled");
      expect(minified.constructor.name).toBe("n");
      expect(apiErrorFromThrow(minified).kind).toBe("cancelled");
    });
  });

  it("reports the caller's timeout as a timeout whatever the error looks like", () => {
    // The transport's deadline is a fact only ApiClient.request holds, so it
    // is believed over the error object — which under expo/fetch says nothing
    // useful anyway.
    const e = FetchError.createFromError(new Error("Fetch request has been canceled"));
    const err = apiErrorFromThrow(e, { timedOut: true, timeoutMs: 12_000 });
    expect(err.kind).toBe("timeout");
    expect(err.message).toBe("Your Mac did not answer within 12 seconds.");
  });

  it("reports the caller's cancel as a cancellation whatever the error looks like", () => {
    // The mirror case, and the one that matters on screen: a user tapping Stop
    // must never be told their Mac is unreachable.
    const err = apiErrorFromThrow(new TypeError("Network request failed"), { cancelled: true });
    expect(err.kind).toBe("cancelled");
    expect(isCancellation(err)).toBe(true);
  });

  it("keeps a cancellation out of the connection kinds, and a timeout in", () => {
    // WHY THIS ASSERTION EXISTS: `network` IS a connection kind. Every abort
    // used to land there, so tapping Stop ran `store.noteFailure`, which knocked
    // the connection to retrying and called `clearMediaToken()` — killing the
    // player and every thumbnail for a deliberate user action.
    expect(isConnectionFailure(apiErrorFromThrow(new Error("x"), { cancelled: true }))).toBe(false);
    expect(isConnectionFailure(apiErrorFromThrow(new Error("x"), { timedOut: true }))).toBe(true);
  });

  it("names the timeout in seconds, singular included", () => {
    expect(apiErrorFromThrow(new Error("x"), { timedOut: true, timeoutMs: 1_000 }).message).toBe(
      "Your Mac did not answer within 1 second.",
    );
    expect(apiErrorFromThrow(new Error("x"), { timedOut: true }).message).toMatch(/in time/);
  });

  it("treats anything else as an ambiguous network failure", () => {
    // This is the "TypeError: Network request failed" case; lib/net.ts is what
    // turns the ambiguity into a diagnosis. It must NOT be widened by the abort
    // patterns — a real unreachable Mac reported as "cancelled" would be silent.
    expect(apiErrorFromThrow(new TypeError("Network request failed")).kind).toBe("network");
    expect(apiErrorFromThrow(new Error("fetch failed: Software caused connection abort")).kind).toBe(
      "network",
    );
    expect(apiErrorFromThrow("a string").kind).toBe("network");
  });
});

describe("errorMessage", () => {
  it("never returns an empty string", () => {
    for (const input of [new Error(""), "", null, undefined, {}]) {
      expect(errorMessage(input).length).toBeGreaterThan(0);
    }
  });

  it("uses an ApiError's own message verbatim", () => {
    expect(errorMessage(new ApiError({ kind: "rejected", message: "no clip on v1" }))).toBe("no clip on v1");
  });
});

describe("isCancellation", () => {
  it("recognises both the typed and the job-loop forms", () => {
    expect(isCancellation(new ApiError({ kind: "cancelled", message: "x" }))).toBe(true);
    expect(isCancellation(new Error("upscale was cancelled"))).toBe(true);
    expect(isCancellation(new Error("upscale failed"))).toBe(false);
  });
});

describe("isApiError", () => {
  it("narrows", () => {
    expect(isApiError(new ApiError({ kind: "server", message: "x" }))).toBe(true);
    expect(isApiError(new Error("x"))).toBe(false);
  });
});
