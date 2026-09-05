/**
 * The error envelope. The ordering assertion in "prefers details.message" is
 * the one that matters most: the Mac hardcodes `message: "request failed"` for
 * every HTTPException whose detail is a dict, so reading `error.message` first
 * means a real server-side explanation can never reach a user.
 */

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

  it("reads an abort as a cancellation, not a network failure", () => {
    const abort = new Error("Aborted");
    abort.name = "AbortError";
    expect(apiErrorFromThrow(abort).kind).toBe("cancelled");
  });

  it("treats anything else as an ambiguous network failure", () => {
    // This is the "TypeError: Network request failed" case; lib/net.ts is what
    // turns the ambiguity into a diagnosis.
    expect(apiErrorFromThrow(new TypeError("Network request failed")).kind).toBe("network");
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
