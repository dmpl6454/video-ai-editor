/**
 * The connection reducer. Its job is to make sure that when something is
 * wrong, the app says the RIGHT wrong thing — and, just as importantly, that
 * an ordinary per-request failure does not take the whole app offline.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";

import {
  connectionMessage,
  initialConnectionState,
  isConnectionFailure,
  mediaEnabled,
  needsMacNotice,
  reduce,
  retryDelay,
  shouldAutoRetry,
  type ConnectionState,
} from "../../lib/connection";
import { ApiError, type ApiErrorKind } from "../../lib/errors";
import { LOCAL_NETWORK_GRACE_MS } from "../../lib/net";

const LOCAL = "192.168.1.20";

function whoamiFixture(): NonNullable<ConnectionState["whoami"]> {
  return {
    device: { id: "d1", name: "Sam's iPhone", created_at: 0 },
    loopback: false,
    server: {
      version: "0.6.0", lan_enabled: true, auth_required: true,
      job_workers: 2, max_upload_bytes: 1_000, media_token_ttl_s: 60,
    },
  };
}

function selected(overrides: Partial<ConnectionState> = {}): ConnectionState {
  return {
    ...reduce(initialConnectionState, { type: "select", host: LOCAL, port: 8765, connectionId: "c1" }),
    ...overrides,
  };
}

function fail(state: ConnectionState, kind: ApiErrorKind, at: number, retryAfterMs?: number): ConnectionState {
  return reduce(state, {
    type: "failure",
    at,
    error: new ApiError({ kind, message: `${kind} happened`, retryAfterMs: retryAfterMs ?? null }),
  });
}

describe("select and success", () => {
  it("moves to connecting and clears any previous error", () => {
    const s = selected({ lastError: "old" });
    expect(s.status).toBe("connecting");
    expect(s.host).toBe(LOCAL);
    expect(s.connectionId).toBe("c1");
  });

  it("records the durable local-network fact on the first success", () => {
    const s = reduce(selected(), { type: "success", at: 1_000, whoami: whoamiFixture() });
    expect(s.status).toBe("connected");
    expect(s.hasEverConnectedLocally).toBe(true);
    expect(s.whoami?.server.job_workers).toBe(2);
    expect(s.failingSince).toBeNull();
  });

  it("does not drop a live connection to 'connecting' on a routine re-check", () => {
    // Otherwise the banner flickers once a poll and every media loader in the
    // app stops with it.
    const connected = reduce(selected(), { type: "success", at: 1_000 });
    expect(reduce(connected, { type: "attempt" }).status).toBe("connected");
  });

  it("keeps the previous whoami when a later success carries none", () => {
    const first = reduce(selected(), { type: "success", at: 1, whoami: whoamiFixture() });
    const second = reduce(first, { type: "success", at: 2 });
    expect(second.whoami?.server.job_workers).toBe(2);
  });
});

describe("per-request failures do not take the app offline", () => {
  it.each<ApiErrorKind>(["forbidden", "not_found", "rejected", "too_large", "server", "cancelled"])(
    "ignores a %s",
    (kind) => {
      const connected = reduce(selected(), { type: "success", at: 1_000 });
      // One clip with an out-of-session source path must not disconnect the
      // session; one 422 from one tool must not either.
      expect(fail(connected, kind, 2_000)).toEqual(connected);
    },
  );

  it("recognises which kinds are connection-level", () => {
    expect(isConnectionFailure(new ApiError({ kind: "network", message: "" }))).toBe(true);
    expect(isConnectionFailure(new ApiError({ kind: "auth_lockout", message: "" }))).toBe(true);
    expect(isConnectionFailure(new ApiError({ kind: "forbidden", message: "" }))).toBe(false);
    expect(isConnectionFailure(new Error("plain"))).toBe(false);
  });
});

describe("the three local-network branches", () => {
  it("retries quietly on a first run", () => {
    const s = fail(selected(), "network", 0);
    expect(s.status).toBe("retrying");
    expect(s.lastError).toMatch(/Looking for your Mac/);
  });

  it("moves to blocked once the grace window closes", () => {
    const first = fail(selected(), "network", 0);
    const later = fail(first, "network", LOCAL_NETWORK_GRACE_MS + 1);
    expect(later.status).toBe("blocked");
    expect(later.lastError).toMatch(/Settings/);
  });

  it("blames the Mac, never the permission, once we have ever connected", () => {
    const connected = reduce(selected(), { type: "success", at: 0 });
    const s = fail(connected, "network", 10_000);
    expect(s.status).toBe("unreachable");
    expect(s.lastError).toMatch(/awake/);
  });

  it("says an off-LAN address can never work, whatever the elapsed time", () => {
    const s = fail(
      reduce(initialConnectionState, { type: "select", host: "example.com", port: 80, connectionId: "c2" }),
      "network",
      0,
    );
    expect(s.status).toBe("not_local");
  });

  it("measures elapsed time from the first failure of the run, not the latest", () => {
    let s = fail(selected(), "network", 1_000);
    expect(s.failingSince).toBe(1_000);
    s = fail(s, "network", 2_000);
    expect(s.failingSince).toBe(1_000);
    expect(s.status).toBe("retrying");
    s = fail(s, "network", 1_000 + LOCAL_NETWORK_GRACE_MS);
    expect(s.status).toBe("blocked");
  });
});

describe("auth", () => {
  it("treats a rejected token as terminal until the user acts", () => {
    const s = fail(reduce(selected(), { type: "success", at: 0 }), "unauthorized", 1_000);
    expect(s.status).toBe("unauthorized");
    expect(shouldAutoRetry(s)).toBe(false);
  });

  it("treats a lockout as its own thing and does not retry into it", () => {
    // Every retry during a lockout extends the lockout.
    const s = fail(reduce(selected(), { type: "success", at: 0 }), "auth_lockout", 1_000, 60_000);
    expect(s.status).toBe("unauthorized");
    expect(s.retryAfterMs).toBe(60_000);
    expect(shouldAutoRetry(s)).toBe(false);
    expect(mediaEnabled(s)).toBe(false);
  });
});

describe("rate limiting is never neutral", () => {
  it("leaves the connected state so the banner cannot lie", () => {
    /**
     * A 429 means we DID reach the Mac, which is exactly why a naive reducer
     * treats it as success — and then shows a green banner over an app that
     * cannot load a single thumbnail for the next minute.
     */
    const connected = reduce(selected(), { type: "success", at: 0 });
    const s = fail(connected, "rate_limited", 1_000, 5_000);
    expect(s.status).toBe("throttled");
    expect(mediaEnabled(s)).toBe(false);
    expect(shouldAutoRetry(s)).toBe(true);
    expect(retryDelay(s, 2_000)).toBe(5_000);
  });
});

describe("mediaEnabled", () => {
  it("is true only while connected", () => {
    const connected = reduce(selected(), { type: "success", at: 0 });
    expect(mediaEnabled(connected)).toBe(true);
    for (const s of [
      initialConnectionState,
      selected(),
      fail(selected(), "network", 0),
      fail(connected, "unauthorized", 1),
    ]) {
      expect(mediaEnabled(s)).toBe(false);
    }
  });
});

describe("retryDelay", () => {
  it("backs off while unreachable, and caps", () => {
    const connected = reduce(selected(), { type: "success", at: 0 });
    const s = fail(connected, "network", 1_000);
    expect(retryDelay(s, 1_000)).toBe(2_000);
    expect(retryDelay(s, 1_000 + 120_000)).toBe(30_000);
  });
});

describe("cleared", () => {
  it("forgets the Mac but keeps the permission fact", () => {
    // The Local Network grant is app-wide and survives forgetting one Mac;
    // discarding it would send the user to Settings for no reason.
    const connected = reduce(selected(), { type: "success", at: 0 });
    const s = reduce(connected, { type: "cleared" });
    expect(s.status).toBe("idle");
    expect(s.host).toBeNull();
    expect(s.hasEverConnectedLocally).toBe(true);
  });
});

describe("copy", () => {
  it("has a distinct, non-empty line for every status", () => {
    const connected = reduce(selected(), {
      type: "success",
      at: 0,
      whoami: {
        device: { id: "d1", name: "Sam's iPhone", created_at: 0 },
        loopback: false,
        server: {
          version: "0.6.0", lan_enabled: true, auth_required: true,
          job_workers: 2, max_upload_bytes: 1_000, media_token_ttl_s: 60,
        },
      },
    });
    const states: ConnectionState[] = [
      initialConnectionState,
      selected(),
      connected,
      fail(selected(), "network", 0),
      fail(fail(selected(), "network", 0), "network", LOCAL_NETWORK_GRACE_MS + 1),
      fail(connected, "network", 1),
      fail(connected, "unauthorized", 1),
      fail(connected, "rate_limited", 1),
      fail(connected, "host_rejected", 1),
    ];
    const lines = states.map(connectionMessage);
    for (const l of lines) expect(l.length).toBeGreaterThan(10);
    expect(new Set(lines).size).toBe(lines.length);
    // The Mac sends no name for itself — whoami.device is THIS PHONE — so the
    // address is the only honest identifier the strip can show.
    expect(connectionMessage(connected)).toBe(`Connected to ${LOCAL}.`);
  });

  it("states the honesty notice unless connected", () => {
    const connected = reduce(selected(), { type: "success", at: 0 });
    expect(needsMacNotice(connected)).toBeNull();
    expect(needsMacNotice(initialConnectionState)).toMatch(/needs your Mac/i);
  });
});

/**
 * The retry policy is only a policy if something obeys it.
 *
 * `shouldAutoRetry` and `retryDelay` shipped with unit tests and no CALLER —
 * a grep over app/, components/, lib/ and constants/ matched only this file and
 * connection.ts itself. That made `retrying`, `unreachable` and `throttled`
 * terminal states whose copy promises a recovery: the Local Network prompt case
 * sat on "Looking for your Mac at 192.168.1.20…" forever after the user tapped
 * Allow, and one 429 burst turned every thumbnail and the player off under a
 * bar reading "Pausing for a moment."
 *
 * A source check rather than a render test because there is no
 * `@testing-library/react-native` here and no simulator on this machine — but a
 * missing caller is exactly the failure that got through, so it is the thing
 * worth pinning.
 */
describe("the auto-retry loop is actually wired up", () => {
  const read = (rel: string) =>
    readFileSync(join(__dirname, "..", "..", rel), "utf8");

  it("has a single owner that calls both policy functions", () => {
    const hook = read("lib/useAutoReconnect.ts");
    expect(hook).toContain("shouldAutoRetry(");
    expect(hook).toContain("retryDelay(");
    expect(hook).toContain("probe()");
    // It must not poll while the app is in the background: iOS freezes timers
    // anyway, but a timer that survives would spend the rate-limit budget on
    // requests nobody is waiting for.
    expect(hook).toContain("AppState");
  });

  it("is mounted once, in the root layout, so it outlives navigation", () => {
    const layout = read("app/_layout.tsx");
    expect(layout).toContain("useAutoReconnect");
    // Exactly one mount: two would double the request rate against a limiter
    // this app is deliberately careful with.
    expect(layout.match(/useAutoReconnect\(\)/g)).toHaveLength(1);
  });
});
