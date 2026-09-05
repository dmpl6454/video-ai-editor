/**
 * The transport. What is asserted here is what the rest of the app relies on
 * never having to think about: the bearer goes on, the anti-CSRF client header
 * goes on, every failure arrives typed, and media requests take a different
 * route from everything else.
 */

// The REAL class expo/fetch rejects with — see the note on ABORT_SHAPES below.
// Imported rather than re-typed so an abort fixture cannot drift away from the
// transport that actually ships.
import { FetchError } from "expo/src/winter/fetch/FetchErrors";

import { ApiClient, DEFAULT_TIMEOUT_MS } from "../../lib/api";
import { isConnectionFailure } from "../../lib/connection";
import { ApiError, isCancellation } from "../../lib/errors";

interface Call {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: string | undefined;
}

const calls: Call[] = [];

function respond(
  status: number,
  body: unknown,
  headers: Record<string, string> = {},
): Response {
  const text = typeof body === "string" ? body : JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (k: string) => headers[k.toLowerCase()] ?? null },
    text: async () => text,
    json: async () => JSON.parse(text),
  } as unknown as Response;
}

function stub(queue: (() => Response | Promise<Response>)[]): void {
  let i = 0;
  global.fetch = jest.fn(async (url: unknown, init: unknown) => {
    const opts = init as { method: string; headers: Record<string, string>; body?: string };
    calls.push({ url: String(url), method: opts.method, headers: opts.headers, body: opts.body });
    const next = queue[Math.min(i, queue.length - 1)];
    i += 1;
    return next?.() ?? respond(200, {});
  }) as unknown as typeof fetch;
}

function client(token: string | null = "tok-123"): ApiClient {
  return new ApiClient({ baseUrl: "http://10.0.0.5:8765", token });
}

beforeEach(() => {
  calls.length = 0;
});

describe("headers", () => {
  it("sends the bearer and the anti-CSRF client header on every request", async () => {
    stub([() => respond(200, { ok: true })]);
    await client().health();
    expect(calls[0]?.headers.authorization).toBe("Bearer tok-123");
    /**
     * `X-VAE-Client` is a security control, not telemetry: it is a header no
     * plain form or image tag can set, so it forces a CORS preflight that the
     * Mac's origin allowlist denies. That is what stops a web page the user is
     * visiting from driving their editor over the same LAN endpoint.
     */
    expect(calls[0]?.headers["X-VAE-Client"]).toBe("1");
  });

  it("omits the bearer when there is none rather than sending an empty one", async () => {
    stub([() => respond(200, { ok: true })]);
    await client(null).health();
    expect(calls[0]?.headers.authorization).toBeUndefined();
    expect(calls[0]?.headers["X-VAE-Client"]).toBe("1");
  });

  it("never sets a Host header by hand", async () => {
    // `Host` is a forbidden header name for fetch; iOS derives it from the URL
    // authority, which is exactly what the Mac's Host allowlist expects.
    stub([() => respond(200, { ok: true })]);
    await client().health();
    const keys = Object.keys(calls[0]?.headers ?? {}).map((k) => k.toLowerCase());
    expect(keys).not.toContain("host");
  });

  it("sets a JSON content type only when there is a body", async () => {
    stub([() => respond(200, {})]);
    const c = client();
    await c.health();
    expect(calls[0]?.headers["content-type"]).toBeUndefined();
    await c.createSession("Cut 1");
    expect(calls[1]?.headers["content-type"]).toBe("application/json");
    expect(calls[1]?.body).toBe(JSON.stringify({ name: "Cut 1" }));
  });
});

describe("error mapping", () => {
  it("turns a 422 envelope into a typed error carrying the Mac's sentence", async () => {
    stub([() => respond(422, { error: { message: "request failed", details: { message: "no clip on v1" } } })]);
    await expect(client().getEdl("s_1")).rejects.toMatchObject({
      kind: "rejected",
      status: 422,
      message: "no clip on v1",
    });
  });

  it("distinguishes a lockout from an ordinary 401", async () => {
    stub([() => respond(401, { error: { code: "auth_lockout" } })]);
    await expect(client().whoami()).rejects.toMatchObject({ kind: "auth_lockout" });
  });

  it("keeps the request id so a report can be matched to a Mac-side log", async () => {
    stub([() => respond(500, "boom", { "x-request-id": "req-42" })]);
    await expect(client().health()).rejects.toMatchObject({ kind: "server", requestId: "req-42" });
  });

  it("reads Retry-After off a 429", async () => {
    stub([() => respond(429, "", { "retry-after": "5" })]);
    await expect(client().health()).rejects.toMatchObject({ kind: "rate_limited", retryAfterMs: 5_000 });
  });

  it("reports an unreachable Mac as an ambiguous network failure", async () => {
    stub([
      () => {
        throw new TypeError("Network request failed");
      },
    ]);
    await expect(client().health()).rejects.toMatchObject({ kind: "network" });
  });

  it("never lets a raw fetch rejection escape untyped", async () => {
    stub([
      () => {
        throw new Error("something odd");
      },
    ]);
    await expect(client().health()).rejects.toBeInstanceOf(ApiError);
  });
});

describe("routes", () => {
  it("uses the wait flag the desktop uses for each dispatch mode", async () => {
    stub([() => respond(200, {})]);
    const c = client();
    await c.dispatch("s_1", "add_text", { text: "hi" });
    expect(calls[0]?.url).toBe("http://10.0.0.5:8765/api/sessions/s_1/dispatch?wait=1");
    await c.dispatchAsync("s_1", "upscale", {});
    expect(calls[1]?.url).toBe("http://10.0.0.5:8765/api/sessions/s_1/dispatch?wait=0");
  });

  it("asks for ops since the last seq it saw", async () => {
    stub([() => respond(200, { ops: [] })]);
    await client().getOps("s_1", 12);
    expect(calls[0]?.url).toContain("/ops?since=12");
  });

  it("strips a trailing slash from the base so paths never double up", async () => {
    stub([() => respond(200, {})]);
    await new ApiClient({ baseUrl: "http://10.0.0.5:8765/", token: null }).health();
    expect(calls[0]?.url).toBe("http://10.0.0.5:8765/api/health");
  });
});

describe("claim", () => {
  it("sends the code and this device's name, with no bearer to send", async () => {
    stub([() => respond(200, { id: "d1", name: "Sam's iPhone", token: "bearer", created_at: 0, server: {} })]);
    const result = await new ApiClient({ baseUrl: "http://10.0.0.5:8765", token: null }).claim(
      "0123456789abcdef0123456789abcdef",
      "Sam's iPhone",
    );
    expect(calls[0]?.url).toBe("http://10.0.0.5:8765/api/pair/claim");
    expect(calls[0]?.body).toBe(
      JSON.stringify({ code: "0123456789abcdef0123456789abcdef", device_name: "Sam's iPhone" }),
    );
    expect(calls[0]?.headers.authorization).toBeUndefined();
    expect(result.token).toBe("bearer");
  });

  it("surfaces the Mac's one deliberate message for a stale or used code", async () => {
    /**
     * The Mac answers "wrong", "already used" and "expired" identically on
     * purpose — telling an attacker which guesses were close would be worse
     * than the small loss of precision. The phone must show that sentence
     * rather than inventing three of its own.
     */
    stub([
      () =>
        respond(401, {
          detail: {
            error: "bad_code",
            message: "That code is not valid any more. Codes work once and expire after ten minutes — show a new one on the Mac.",
          },
        }),
    ]);
    await expect(
      new ApiClient({ baseUrl: "http://10.0.0.5:8765", token: null }).claim("0".repeat(32), "iPhone"),
    ).rejects.toMatchObject({
      kind: "unauthorized",
      message: expect.stringContaining("Codes work once"),
    });
  });
});

describe("queueDepthFor", () => {
  const now = 1_000;
  const jobs = [
    { id: "old-run", status: "running", created_at: now - 30 },
    { id: "old-queued", status: "queued", created_at: now - 20 },
    { id: "done", status: "completed", created_at: now - 10 },
    { id: "mine", status: "queued", created_at: now },
    { id: "after", status: "queued", created_at: now + 10 },
  ];

  it("counts only unfinished jobs created before ours", async () => {
    stub([() => respond(200, { jobs })]);
    expect(await client().queueDepthFor("s_1", "mine")).toBe(2);
  });

  it("returns null when the Mac has no record of our job", async () => {
    stub([() => respond(200, { jobs })]);
    expect(await client().queueDepthFor("s_1", "nope")).toBeNull();
  });

  it("asks the session-scoped route the Mac actually serves", async () => {
    // `GET /api/jobs` is not a route on the Mac — only `/api/jobs/{id}`,
    // `/api/jobs/{id}/cancel` and `/api/sessions/{sid}/jobs`. Asking for the
    // collection 404'd on every poll, so queue depth was permanently null and
    // every queued job showed the "we do not know" fallback line.
    const seen: string[] = [];
    global.fetch = jest.fn((url: unknown) => {
      seen.push(String(url));
      return Promise.resolve(respond(200, { jobs }));
    }) as unknown as typeof fetch;
    await client().queueDepthFor("s_abc", "mine");
    expect(seen).toEqual([expect.stringContaining("/api/sessions/s_abc/jobs")]);
    expect(seen[0]).not.toMatch(/\/api\/jobs(\?|$)/);
  });
});

describe("media", () => {
  it("offers the bearer to native loaders that honour headers", () => {
    expect(client().mediaHeaders()).toEqual({ authorization: "Bearer tok-123" });
    expect(client(null).mediaHeaders()).toEqual({});
  });

  it("appends a short-lived query token for loaders that do not", async () => {
    stub([() => respond(200, { token: "dev1.1700000060.abc", expires_at: 1_700_000_060, ttl_s: 60 })]);
    const url = await client().mediaUrl("/api/sessions/s_1/thumb?src=/a.mov&t=1");
    expect(url).toBe("http://10.0.0.5:8765/api/sessions/s_1/thumb?src=/a.mov&t=1&k=dev1.1700000060.abc");
  });

  it("uses ? when the path has no query of its own", async () => {
    stub([() => respond(200, { token: "dev1.1700000060.abc", expires_at: 1_700_000_060, ttl_s: 60 })]);
    expect(await client().mediaUrl("/api/sessions/s_1/preview.mp4")).toContain("preview.mp4?k=dev1.1700000060.abc");
  });

  it("mints the token once for a burst of callers", async () => {
    /**
     * A filmstrip asks for two dozen URLs at once. Minting a token per request
     * would spend the whole rate-limit budget on tokens and blank the strip.
     */
    stub([() => respond(200, { token: "dev1.1700000060.abc", expires_at: 1_700_000_060, ttl_s: 60 })]);
    const c = client();
    await Promise.all(Array.from({ length: 24 }, () => c.mediaUrl("/api/x")));
    const mints = calls.filter((call) => call.url.endsWith("/api/pair/media_token"));
    expect(mints).toHaveLength(1);
  });

  it("still returns a usable URL when the Mac will not mint a token", async () => {
    // Header auth is the other half of the story; a URL with no `k` is not a
    // failure, it just leans on `mediaHeaders()`.
    stub([() => respond(500, "no")]);
    expect(await client().mediaUrl("/api/x")).toBe("http://10.0.0.5:8765/api/x");
  });

  it("re-mints after the cached token is cleared", async () => {
    stub([() => respond(200, { token: "dev1.1700000060.abc", expires_at: 1_700_000_060, ttl_s: 60 })]);
    const c = client();
    await c.mediaUrl("/api/x");
    c.clearMediaToken();
    await c.mediaUrl("/api/x");
    expect(calls.filter((call) => call.url.endsWith("/api/pair/media_token"))).toHaveLength(2);
  });

  it("probes the query-token path without sending the bearer", async () => {
    stub([() => respond(200, { token: "dev1.1700000060.abc", expires_at: 1_700_000_060, ttl_s: 60 }), () => respond(200, "ok")]);
    const result = await client().probeMedia("/api/sessions/s_1/thumb?src=/a.mov");
    expect(result.ok).toBe(true);
    const probe = calls[1];
    expect(probe?.url).toContain("k=dev1.1700000060.abc");
    // The whole point of the probe is that it authenticates the way a native
    // loader will — by query string alone.
    expect(probe?.headers.authorization).toBeUndefined();
    expect(probe?.headers["X-VAE-Client"]).toBeUndefined();
  });

  it("reports a refused media request in words rather than silently", async () => {
    stub([() => respond(200, { token: "dev1.1700000060.abc", expires_at: 1_700_000_060, ttl_s: 60 }), () => respond(403, "nope")]);
    const result = await client().probeMedia("/api/sessions/s_1/thumb?src=/a.mov");
    expect(result.ok).toBe(false);
    expect(result.detail).toMatch(/forbidden/);
  });
});

describe("withToken", () => {
  it("returns a new client rather than mutating one in flight", () => {
    const a = client("one");
    const b = a.withToken("two");
    expect(a.token).toBe("one");
    expect(b.token).toBe("two");
    expect(b.baseUrl).toBe(a.baseUrl);
  });
});

describe("timeouts and cancellation", () => {
  /**
   * The two abort shapes this transport can actually produce.
   *
   * `expo` is the one that runs in this app: `expo/src/winter/runtime.native.ts`
   * installs expo's own fetch over React Native's unless EXPO_PUBLIC_USE_RN_FETCH
   * is set, and it is not set anywhere in this project. Built through the REAL
   * `FetchError`, so `name` is the inherited "Error" and the message is
   * "fetch failed: Fetch request has been canceled" — neither of which the old
   * `e.name === "AbortError" || e.message === "Aborted"` guard matched.
   *
   * `rn` is the legacy whatwg-fetch shape. The suite drives BOTH because
   * pinning only `rn` — which is what it used to do — is precisely how the
   * timeout and cancel branches came to be dead code in a shipping build while
   * every test stayed green.
   */
  const ABORT_SHAPES = {
    expo: () => FetchError.createFromError(new Error("Fetch request has been canceled")),
    rn: () => {
      const e = new Error("Aborted");
      e.name = "AbortError";
      return e;
    },
  } as const;

  /** A fetch that never settles until its signal aborts, then rejects as `shape`. */
  function hangingFetch(shape: keyof typeof ABORT_SHAPES) {
    global.fetch = jest.fn(
      (_url: unknown, init: unknown) =>
        new Promise((_resolve, reject) => {
          const signal = (init as { signal: AbortSignal }).signal;
          signal.addEventListener("abort", () => reject(ABORT_SHAPES[shape]()));
        }),
    ) as unknown as typeof fetch;
  }

  const shapes = Object.keys(ABORT_SHAPES) as (keyof typeof ABORT_SHAPES)[];

  describe.each(shapes)("with a %s abort", (shape) => {
    it("reports a request that never answers as a TIMEOUT, not a cancellation", async () => {
      // This assertion used to read `kind: "cancelled"`, and it was pinning the
      // bug rather than the behaviour. `"cancelled"` is not in
      // `connection.ts::CONNECTION_KINDS`, so the reducer ignored it and a
      // sleeping Mac left the bar reading "Connected". `edit.tsx` and
      // `import.tsx` both suppress cancellations, so the failure was silent on
      // screen as well as in the state. Under expo/fetch it was worse still:
      // the abort was not recognised at all and came out as `network`.
      jest.useFakeTimers();
      hangingFetch(shape);

      const promise = client().health();
      const assertion = expect(promise).rejects.toMatchObject({
        kind: "timeout",
        // The sentence the retry path and the banner were written around.
        message: "Your Mac did not answer within 12 seconds.",
      });
      jest.advanceTimersByTime(DEFAULT_TIMEOUT_MS + 1);
      await assertion;
      jest.useRealTimers();
    });

    it("still reports a CALLER's abort as a cancellation", async () => {
      // The other half: a cancel is the one failure the user asked for, and it
      // must stay neutral — never "your Mac did not answer".
      jest.useFakeTimers();
      hangingFetch(shape);

      const controller = new AbortController();
      const promise = client().request("GET", "/api/health", { signal: controller.signal });
      const assertion = expect(promise).rejects.toMatchObject({ kind: "cancelled" });
      controller.abort();
      await assertion;
      jest.useRealTimers();
    });

    it("keeps a user's cancel OUT of the connection kinds", async () => {
      // chat.tsx, export.tsx and import.tsx all pass real AbortSignals. When
      // this came back as `network` — a CONNECTION_KIND — a tap on Stop ran
      // store.noteFailure, dropped the connection to retrying/unreachable and
      // called client.clearMediaToken(), so the player and every thumbnail died
      // and a red banner appeared for something the user chose to do.
      jest.useFakeTimers();
      hangingFetch(shape);

      const controller = new AbortController();
      const promise = client().request("GET", "/api/health", { signal: controller.signal });
      controller.abort();

      const error = await promise.then(
        () => { throw new Error("expected the request to reject"); },
        (e: unknown) => e,
      );
      expect(isConnectionFailure(error)).toBe(false);
      expect(isCancellation(error)).toBe(true);
      jest.useRealTimers();
    });

    it("counts a timeout AS a connection failure so the reducer moves", async () => {
      jest.useFakeTimers();
      hangingFetch(shape);

      const promise = client().health();
      const settled = promise.then(
        () => { throw new Error("expected the request to reject"); },
        (e: unknown) => e,
      );
      jest.advanceTimersByTime(DEFAULT_TIMEOUT_MS + 1);
      const error = await settled;
      expect(isConnectionFailure(error)).toBe(true);
      expect(isCancellation(error)).toBe(false);
      jest.useRealTimers();
    });
  });

  it("lets the caller's cancel win when the timeout fires in the same tick", async () => {
    // Both aborts race on one controller. The user's intent is the one that
    // must survive: being told "your Mac did not answer" for a tap on Stop is
    // a lie, and a timeout would move the connection state as a side effect.
    jest.useFakeTimers();
    hangingFetch("expo");

    const controller = new AbortController();
    const promise = client().request("GET", "/api/health", { signal: controller.signal });
    const assertion = expect(promise).rejects.toMatchObject({ kind: "cancelled" });
    controller.abort();
    jest.advanceTimersByTime(DEFAULT_TIMEOUT_MS + 1);
    await assertion;
    jest.useRealTimers();
  });

  it("still reports a genuine transport failure as a network failure", async () => {
    // The guard against over-widening: an unreachable Mac must NOT be quietly
    // reclassified as a cancellation, or the failure disappears from the UI.
    global.fetch = jest.fn(async () => {
      throw new TypeError("Network request failed");
    }) as unknown as typeof fetch;

    await expect(client().health()).rejects.toMatchObject({ kind: "network" });
  });
});
