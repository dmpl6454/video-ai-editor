/**
 * Saved connections and where the credential lives.
 *
 * The pure list transforms are tested directly; `loadVault`/`updateVault` and
 * the token helpers are tested against fakes for expo-file-system and
 * expo-secure-store, because the point of those assertions is WHICH store each
 * piece of data lands in — a token in the JSON file would be a real defect and
 * no amount of testing the transforms would catch it.
 */

const mockFiles = new Map<string, string>();
const mockKeychain = new Map<string, string>();
const mockSecureCalls: { key: string; options?: unknown }[] = [];

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
  setItemAsync: jest.fn(async (key: string, value: string, options?: unknown) => {
    mockSecureCalls.push({ key, options });
    mockKeychain.set(key, value);
  }),
  getItemAsync: jest.fn(async (key: string) => mockKeychain.get(key) ?? null),
  deleteItemAsync: jest.fn(async (key: string) => {
    mockKeychain.delete(key);
  }),
}));

import {
  activeConnection,
  connectionId,
  deleteToken,
  forgetConnection,
  loadVault,
  MAX_SAVED_CONNECTIONS,
  readToken,
  saveToken,
  updateVault,
  withConnectedNow,
  withConnection,
  withoutConnection,
  type SavedConnection,
  type Vault,
} from "../../lib/vault";

const EMPTY: Vault = { connections: [], activeId: null, hasEverConnectedLocally: false };

function conn(id: string, lastConnectedAt: number | null = null): SavedConnection {
  return { id, host: `10.0.0.${id.length}`, port: 8765, lastConnectedAt };
}

beforeEach(() => {
  mockFiles.clear();
  mockKeychain.clear();
  mockSecureCalls.length = 0;
});

describe("connectionId", () => {
  it("produces a key the mockKeychain will accept", () => {
    // SecureStore keys allow only alphanumerics, ".", "-" and "_", so a colon
    // from an IPv6 literal has to be replaced or every save silently fails.
    expect(connectionId("192.168.1.20", 8765)).toBe("192.168.1.20_8765");
    expect(connectionId("fe80::1", 8765)).toMatch(/^[A-Za-z0-9._-]+$/);
    expect(connectionId("Studio-Mac.local", 8765)).toBe("studio-mac.local_8765");
  });

  it("is stable for the same host and port", () => {
    expect(connectionId("10.0.0.5", 8765)).toBe(connectionId(" 10.0.0.5 ", 8765));
  });
});

describe("list transforms", () => {
  it("adds a connection and makes it active", () => {
    const v = withConnection(EMPTY, conn("a"));
    expect(v.connections).toHaveLength(1);
    expect(v.activeId).toBe("a");
  });

  it("replaces rather than duplicates an existing connection", () => {
    const once = withConnection(EMPTY, conn("a"));
    const twice = withConnection(once, { ...conn("a"), lastConnectedAt: 99 });
    expect(twice.connections).toHaveLength(1);
    expect(twice.connections[0]?.lastConnectedAt).toBe(99);
  });

  it("orders by recency and drops the oldest past the cap", () => {
    let v = EMPTY;
    for (let i = 0; i < MAX_SAVED_CONNECTIONS + 3; i += 1) {
      v = withConnection(v, conn(`m${i}`, i));
    }
    expect(v.connections).toHaveLength(MAX_SAVED_CONNECTIONS);
    expect(v.connections[0]?.lastConnectedAt).toBeGreaterThan(v.connections[1]?.lastConnectedAt ?? 0);
  });

  it("clears the active id when the active connection is removed", () => {
    const v = withoutConnection(withConnection(EMPTY, conn("a")), "a");
    expect(v.connections).toHaveLength(0);
    expect(v.activeId).toBeNull();
  });

  it("leaves the active id alone when another connection is removed", () => {
    const v = withConnection(withConnection(EMPTY, conn("a")), conn("b"));
    expect(withoutConnection(v, "a").activeId).toBe("b");
  });

  it("records a successful connection and the durable local-network fact", () => {
    const v = withConnectedNow(withConnection(EMPTY, conn("a")), "a", 1_700);
    expect(v.connections[0]?.lastConnectedAt).toBe(1_700);
    expect(v.hasEverConnectedLocally).toBe(true);
  });

  it("finds the active connection, or nothing", () => {
    expect(activeConnection(EMPTY)).toBeNull();
    expect(activeConnection(withConnection(EMPTY, conn("a")))?.id).toBe("a");
  });
});

describe("persistence", () => {
  it("starts empty when nothing has been written", async () => {
    expect(await loadVault()).toEqual(EMPTY);
  });

  it("round-trips through the file", async () => {
    await updateVault((v) => withConnection(v, conn("a")));
    const loaded = await loadVault();
    expect(loaded.connections[0]?.id).toBe("a");
    expect(loaded.activeId).toBe("a");
  });

  it("recovers from a corrupt file instead of crashing on launch", async () => {
    mockFiles.set("file:///docs/connections.json", "{not json");
    expect(await loadVault()).toEqual(EMPTY);
  });

  it("discards rows that are missing the fields that matter", async () => {
    mockFiles.set(
      "file:///docs/connections.json",
      JSON.stringify({ connections: [{ id: "x" }, { host: "10.0.0.5", port: 8765 }], activeId: "x" }),
    );
    const v = await loadVault();
    expect(v.connections).toHaveLength(1);
    // The surviving row had no id of its own, so one was derived.
    expect(v.connections[0]?.id).toBe("10.0.0.5_8765");
    // "x" no longer exists, so it cannot stay active.
    expect(v.activeId).toBeNull();
  });

  it("never writes a token into the JSON file", async () => {
    await saveToken("a", "super-secret-token-value");
    await updateVault((v) => withConnection(v, conn("a")));
    for (const contents of mockFiles.values()) {
      expect(contents).not.toContain("super-secret-token-value");
    }
  });
});

describe("tokens", () => {
  it("stores the credential in the keychain, pinned to this device", async () => {
    await saveToken("a", "tok");
    expect(await readToken("a")).toBe("tok");
    // Not `WHEN_UNLOCKED`: the default would ride an iCloud mockKeychain onto a
    // second device that was never paired with this Mac.
    expect(mockSecureCalls[0]?.options).toEqual({ keychainAccessible: "whenUnlockedThisDeviceOnly" });
  });

  it("namespaces the key so it cannot collide with anything else", async () => {
    await saveToken("a", "tok");
    expect(mockSecureCalls[0]?.key).toBe("vae_token_a");
  });

  it("returns null rather than throwing when the keychain is unavailable", async () => {
    const store = jest.requireMock("expo-secure-store") as { getItemAsync: jest.Mock };
    store.getItemAsync.mockRejectedValueOnce(new Error("device locked"));
    expect(await readToken("a")).toBeNull();
  });

  it("deletes a token", async () => {
    await saveToken("a", "tok");
    await deleteToken("a");
    expect(await readToken("a")).toBeNull();
  });

  it("forgetting a Mac removes the credential as well as the row", async () => {
    await saveToken("a", "tok");
    await updateVault((v) => withConnection(v, conn("a")));
    const after = await forgetConnection("a");
    expect(after.connections).toHaveLength(0);
    expect(await readToken("a")).toBeNull();
  });
});
