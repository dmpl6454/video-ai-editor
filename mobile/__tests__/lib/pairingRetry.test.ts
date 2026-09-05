/**
 * The first-pairing dead end, driven as the round trips it actually is.
 *
 * WHAT SHIPPED, AND HOW IT LOOKED FROM THE COUCH
 * ----------------------------------------------
 * `whoami` is the connection check precisely BECAUSE it needs the bearer — a
 * phone the Mac has revoked must not be able to show green (`store.attach`).
 * The corollary is that calling it without a credential is a guaranteed 401,
 * and `kindForStatus(401)` is `unauthorized`, which `reduceFailure` treats as
 * terminal.
 *
 * On a real first pairing that is exactly what happened. `pairWith()` keeps its
 * anonymous client in a local and only `attach()` ever puts one on the store,
 * so `client` was still null when the claim failed — and it fails on a real
 * device, because iOS refuses the first LAN request outright while the Local
 * Network prompt is on screen rather than holding it. The reducer went to
 * `retrying`, `useAutoReconnect` armed its 1.5 s timer, `probe()` FABRICATED a
 * token-less client, and the instant the user tapped Allow the anonymous
 * `whoami` came back 401: "Your Mac no longer recognises this phone. Pair it
 * again." — on a phone that had never been paired. `shouldAutoRetry` is false
 * for `unauthorized`, so the loop stopped there for good, and the Retry button
 * (wired straight to `probe()`) reproduced the same dead end and fed the Mac's
 * failed-auth counter on every tap.
 *
 * So these tests do not assert on a fake probe. They run the real store
 * against a stubbed transport, with the REAL `ApiError`s the real
 * `apiErrorFromThrow` / `apiErrorFromResponse` build, and they watch the
 * status the reducer actually reaches. Two things have to be true at once, and
 * a test that only checked the first is how this class of bug ships: a phone
 * mid-pairing must never be told it was revoked, and a phone that HAS been
 * revoked must still be told so.
 */

const mockFiles = new Map<string, string>();
const mockKeychain = new Map<string, string>();

jest.mock("expo-file-system", () => {
  class FakeFile {
    uri: string;
    constructor(dir: { uri: string }, name: string) {
      this.uri = `${dir.uri}/${name}`;
    }
    get exists() {
      return mockFiles.has(this.uri);
    }
    create() {
      if (!mockFiles.has(this.uri)) mockFiles.set(this.uri, "");
    }
    write(content: string) {
      mockFiles.set(this.uri, content);
    }
    async text() {
      return mockFiles.get(this.uri) ?? "";
    }
  }
  class FakeDirectory {
    uri: string;
    constructor(dir: { uri: string }) {
      this.uri = dir.uri;
    }
    get exists() {
      return true;
    }
    create() {}
  }
  return {
    File: FakeFile,
    Directory: FakeDirectory,
    Paths: { document: { uri: "file:///docs" }, cache: { uri: "file:///cache" } },
  };
});

jest.mock("expo-secure-store", () => ({
  WHEN_UNLOCKED_THIS_DEVICE_ONLY: "whenUnlockedThisDeviceOnly",
  setItemAsync: jest.fn(async (key: string, value: string) => {
    mockKeychain.set(key, value);
  }),
  getItemAsync: jest.fn(async (key: string) => mockKeychain.get(key) ?? null),
  deleteItemAsync: jest.fn(async (key: string) => {
    mockKeychain.delete(key);
  }),
}));

// The store asks the OS what this phone is called so the Mac's paired-device
// list is revocable by a human. Nothing here depends on the value.
jest.mock("expo-device", () => ({ deviceName: "Test iPhone", modelName: "iPhone17,1" }));

import { createClient } from "../../lib/api";
import { initialConnectionState, type ConnectionStatus } from "../../lib/connection";
import type { PairPayload } from "../../lib/pair";
import { selectCanReconnect, useStore } from "../../lib/store";
import { connectionId, readToken, saveToken, type SavedConnection } from "../../lib/vault";

const HOST = "192.168.1.20";
const PORT = 8765;
/** `api/pairing.py::_CODE_RE` — `secrets.token_hex(16)`. */
const CODE = "0123456789abcdef0123456789abcdef";
const PAYLOAD: PairPayload = { host: HOST, port: PORT, code: CODE };
const ID = connectionId(HOST, PORT);

const SERVER = {
  version: "0.6.0",
  lan_enabled: true,
  auth_required: true,
  job_workers: 2,
  max_upload_bytes: 4_000_000_000,
  media_token_ttl_s: 300,
};

interface Call {
  url: string;
  method: string;
  /** Undefined means the request went out with NO credential at all. */
  authorization: string | undefined;
}

const calls: Call[] = [];

/**
 * A model of the Mac, not a queue of canned answers.
 *
 * A FIFO of responses cannot reproduce this defect, because the defect is
 * about WHICH request gets sent: script the answers in order and a token-less
 * `whoami` quietly consumes the reply meant for a claim and the test passes
 * over the bug. So the fake answers by route and by credential, the way
 * `api/pairing.py` does — an anonymous request to a token-gated route is a 401,
 * a single-use code works once — and the transport in front of it can be shut
 * off to stand in for iOS killing a LAN request while the Local Network prompt
 * is up.
 */
const mac = {
  /** False = the request never lands. iOS during the permission prompt. */
  reachable: true,
  /** Kill every request after this many have landed — used to drop the
   *  `whoami` that follows a claim the Mac DID answer. */
  dropAfterDelivered: null as number | null,
  /** The one bearer this Mac accepts, or null if it knows no phone. */
  knownToken: null as string | null,
  /** Single-use, exactly as `secrets.token_hex(16)` codes are on the Mac. */
  codeSpent: false,
  /** The user removed this phone in the Mac's Phone panel. */
  revoked: false,
};

const ISSUED_TOKEN = "bearer-issued-by-the-mac";
let delivered = 0;

function respond(status: number, body: unknown): Response {
  const text = JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    text: async () => text,
    json: async () => JSON.parse(text),
  } as unknown as Response;
}

function serve(url: string, authorization: string | undefined): Response {
  // The transport. A dead LAN request throws, which is how it reaches
  // `apiErrorFromThrow` and gets classified `network` — the ambiguous kind the
  // whole `retrying` branch exists for.
  if (!mac.reachable || (mac.dropAfterDelivered !== null && delivered >= mac.dropAfterDelivered)) {
    throw new TypeError("Network request failed");
  }
  delivered += 1;

  if (url.endsWith("/api/pair/claim")) {
    if (mac.codeSpent) {
      return respond(401, { error: { message: "That pairing code has already been used." } });
    }
    mac.codeSpent = true;
    mac.knownToken = ISSUED_TOKEN;
    return respond(200, {
      id: "dev-1",
      name: "Test iPhone",
      created_at: 0,
      token: ISSUED_TOKEN,
      server: SERVER,
    });
  }

  if (url.endsWith("/api/pair/whoami")) {
    // The line the whole defect turns on: whoami REQUIRES the bearer, so an
    // anonymous probe is a guaranteed 401 — and 401 is `unauthorized`, which
    // the reducer treats as terminal.
    if (mac.revoked) {
      return respond(401, { error: { message: "This phone is no longer paired with this Mac." } });
    }
    if (mac.knownToken === null || authorization !== `Bearer ${mac.knownToken}`) {
      return respond(401, { error: { message: "Unknown device." } });
    }
    return respond(200, {
      device: { id: "dev-1", name: "Test iPhone", created_at: 0 },
      loopback: false,
      server: SERVER,
    });
  }

  return respond(404, { error: { message: `no route for ${url}` } });
}

function saved(): SavedConnection {
  return { id: ID, host: HOST, port: PORT, lastConnectedAt: 1_700_000_000_000 };
}

/** Every status the reducer passed through, in order. The assertion that
 *  matters most in this file is about a status the app must never REACH, and
 *  a final-state check would miss one it passed through and recovered from. */
function recordStatuses(): ConnectionStatus[] {
  const seen: ConnectionStatus[] = [useStore.getState().conn.status];
  useStore.subscribe((s) => {
    if (s.conn.status !== seen[seen.length - 1]) seen.push(s.conn.status);
  });
  return seen;
}

beforeEach(() => {
  mockFiles.clear();
  mockKeychain.clear();
  calls.length = 0;
  delivered = 0;
  mac.reachable = true;
  mac.dropAfterDelivered = null;
  mac.knownToken = null;
  mac.codeSpent = false;
  mac.revoked = false;
  useStore.setState({
    conn: initialConnectionState,
    vault: { connections: [], activeId: null, hasEverConnectedLocally: false },
    client: null,
    pendingPair: null,
    sessionId: null,
    session: null,
    edl: null,
    lastOpSeq: 0,
    busy: null,
    abort: null,
  });
  global.fetch = jest.fn(async (url: unknown, init: unknown) => {
    const opts = init as { method: string; headers: Record<string, string> };
    const authorization = opts.headers?.authorization;
    calls.push({ url: String(url), method: opts.method, authorization });
    return serve(String(url), authorization);
  }) as unknown as typeof fetch;
});

const whoamiCalls = () => calls.filter((c) => c.url.endsWith("/api/pair/whoami"));
const claimCalls = () => calls.filter((c) => c.url.endsWith("/api/pair/claim"));

describe("a first pairing interrupted by the Local Network prompt", () => {
  it("does not tell a never-paired phone that its Mac has revoked it", async () => {
    const statuses = recordStatuses();

    // 1. The claim goes out while the prompt is on screen; iOS kills it.
    mac.reachable = false;
    expect(await useStore.getState().pairWith(PAYLOAD)).toBe(false);
    expect(useStore.getState().conn.status).toBe("retrying");
    // The code is unspent: the Mac never answered, so it never burned it.
    expect(useStore.getState().pendingPair).toEqual(PAYLOAD);

    // 2. The user taps Allow — and from here the Mac answers everything,
    //    including the anonymous whoami the old retry sent, with the truth.
    mac.reachable = true;
    const resumed = await useStore.getState().reconnect();

    // The headline assertion goes first, because it is the sentence a real
    // user read on a phone they had never paired: "Your Mac no longer
    // recognises this phone. Pair it again." The status is checked across the
    // whole run, not just at the end — `unauthorized` is terminal, so passing
    // through it is the bug even if something later recovered.
    expect(statuses).not.toContain("unauthorized");
    expect(useStore.getState().conn.status).toBe("connected");
    expect(resumed).toBe(true);
    expect(await readToken(ID)).toBe(ISSUED_TOKEN);
  });

  it("issues no token-less whoami at any point, so no 401 can be manufactured", async () => {
    mac.reachable = false;
    await useStore.getState().pairWith(PAYLOAD);
    mac.reachable = true;
    await useStore.getState().reconnect();

    // The claim is the one request that is meant to be anonymous — the code IS
    // the credential. Two of them: the one iOS killed and the one that worked.
    expect(claimCalls()).toHaveLength(2);
    expect(claimCalls().every((c) => c.authorization === undefined)).toBe(true);
    expect(whoamiCalls()).toHaveLength(1);
    expect(whoamiCalls().every((c) => c.authorization === `Bearer ${ISSUED_TOKEN}`)).toBe(true);
  });

  it("sends nothing at all when probe() is called with no credential anywhere", async () => {
    // Where the old Retry button and the old retry timer both landed. The Mac
    // is up and would answer an anonymous whoami 401, exactly as in production;
    // the point is that it is never asked.
    mac.reachable = false;
    await useStore.getState().pairWith(PAYLOAD);
    mac.reachable = true;
    calls.length = 0;

    expect(await useStore.getState().probe()).toBe(false);
    expect(calls).toEqual([]);
    // And it leaves the state alone rather than turning a recoverable
    // "retrying" into a terminal one.
    expect(useStore.getState().conn.status).toBe("retrying");
    expect(useStore.getState().client).toBeNull();
  });

  it("refuses to promote a credential-less client that is already in the store", async () => {
    // Defence in depth for the fallback that used to live in probe(): even
    // handed a token-less client, it goes looking for a real token rather than
    // sending the request.
    useStore.setState({
      conn: { ...initialConnectionState, status: "retrying", host: HOST, port: PORT, connectionId: ID, failingSince: 1 },
      client: createClient({ baseUrl: `http://${HOST}:${PORT}`, token: null }),
    });

    expect(await useStore.getState().probe()).toBe(false);
    expect(calls).toEqual([]);
    expect(useStore.getState().conn.status).toBe("retrying");
  });

  it("stops the automatic loop from arming while there is nothing to send", () => {
    // `useAutoReconnect` reads this synchronously inside a zustand
    // subscription, which is why it is a selector and not a keychain read.
    expect(selectCanReconnect(useStore.getState())).toBe(false);
  });

  it("sends one claim when the timer and the Retry button fire together", async () => {
    // The race the fix itself introduced: the retry loop now resumes pairings,
    // so it can collide with a Retry tap. The code is single-use, so a second
    // claim would be answered "That pairing code has already been used" — a
    // 401, i.e. `unauthorized`, on a phone that paired a millisecond earlier.
    mac.reachable = false;
    await useStore.getState().pairWith(PAYLOAD);
    mac.reachable = true;
    calls.length = 0;

    const pending = useStore.getState().pendingPair as PairPayload;
    const [a, b] = await Promise.all([
      useStore.getState().reconnect(),
      useStore.getState().pairWith(pending),
    ]);

    expect([a, b]).toEqual([true, true]);
    expect(claimCalls()).toHaveLength(1);
    expect(useStore.getState().conn.status).toBe("connected");
  });

  it("arms the automatic loop again as soon as there is an unspent code", async () => {
    mac.reachable = false;
    await useStore.getState().pairWith(PAYLOAD);
    expect(selectCanReconnect(useStore.getState())).toBe(true);
  });
});

describe("once the claim has been answered the code is spent", () => {
  it("retries with the bearer instead of re-sending a burnt code", async () => {
    // The other half of the ambiguity: the claim SUCCEEDED and the token is in
    // the keychain, but the whoami that follows it died on the network. A retry
    // that re-sent the code here would earn a truthful 401 — "That pairing code
    // has already been used" — for a phone that is, in fact, paired.
    mac.dropAfterDelivered = 1;
    expect(await useStore.getState().pairWith(PAYLOAD)).toBe(false);
    expect(useStore.getState().pendingPair).toBeNull();
    expect(await readToken(ID)).toBe(ISSUED_TOKEN);

    mac.dropAfterDelivered = null;
    calls.length = 0;
    expect(await useStore.getState().reconnect()).toBe(true);
    expect(claimCalls()).toEqual([]);
    expect(whoamiCalls()).toHaveLength(1);
    expect(whoamiCalls()[0]?.authorization).toBe(`Bearer ${ISSUED_TOKEN}`);
    expect(useStore.getState().conn.status).toBe("connected");
  });

  it("forgets an unspent code along with the Mac it belongs to", async () => {
    mac.reachable = false;
    await useStore.getState().pairWith(PAYLOAD);

    await useStore.getState().forget(ID);
    expect(useStore.getState().pendingPair).toBeNull();
    expect(selectCanReconnect(useStore.getState())).toBe(false);
  });

  it("abandons an unspent code when the user picks a saved Mac instead", async () => {
    mac.reachable = false;
    await useStore.getState().pairWith(PAYLOAD);

    mac.reachable = true;
    mac.knownToken = "bearer-saved";
    await saveToken(ID, "bearer-saved");
    expect(await useStore.getState().useSaved(saved())).toBe(true);
    expect(useStore.getState().pendingPair).toBeNull();
    expect(claimCalls()).toHaveLength(1); // the first, failed one and no more
  });
});

describe("a phone the Mac really has revoked", () => {
  it("still reaches unauthorized when a saved credential is rejected", async () => {
    await saveToken(ID, "bearer-revoked");
    mac.revoked = true;

    expect(await useStore.getState().useSaved(saved())).toBe(false);
    expect(calls[0]?.authorization).toBe("Bearer bearer-revoked");
    expect(useStore.getState().conn.status).toBe("unauthorized");
    expect(useStore.getState().conn.lastError).toBe("This phone is no longer paired with this Mac.");
  });

  it("still reaches unauthorized when the bearer is rejected on a later probe", async () => {
    // Revoked at the Mac while the phone was connected. The fix must not blind
    // the app to a 401 that a REAL credential earned.
    await saveToken(ID, "bearer-revoked");
    mac.knownToken = "bearer-revoked";
    expect(await useStore.getState().useSaved(saved())).toBe(true);
    expect(useStore.getState().conn.status).toBe("connected");

    mac.revoked = true;
    expect(await useStore.getState().probe()).toBe(false);
    expect(useStore.getState().conn.status).toBe("unauthorized");
    expect(whoamiCalls()).toHaveLength(2);
    expect(whoamiCalls().every((c) => c.authorization === "Bearer bearer-revoked")).toBe(true);
  });

  it("rebuilds the client from the keychain rather than from nothing", async () => {
    // The legitimate half of the old fallback: a store that has lost its client
    // while a saved pairing is still good. It rebuilds WITH the token.
    await saveToken(ID, "bearer-keychain");
    mac.knownToken = "bearer-keychain";
    useStore.setState({
      conn: { ...initialConnectionState, status: "unreachable", host: HOST, port: PORT, connectionId: ID, failingSince: 1 },
      client: null,
    });

    expect(await useStore.getState().probe()).toBe(true);
    expect(calls[0]?.authorization).toBe("Bearer bearer-keychain");
    expect(useStore.getState().conn.status).toBe("connected");
  });
});
