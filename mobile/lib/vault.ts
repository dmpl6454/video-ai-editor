/**
 * Where a pairing lives on the phone.
 *
 * Split deliberately in two:
 *
 *  - The TOKEN goes in the iOS keychain (expo-secure-store), one entry per
 *    saved Mac, pinned to this device. It is a bearer credential for something
 *    that can read files and run ffmpeg on someone's computer; it does not
 *    belong in a JSON file, and it must not ride an iCloud backup onto a
 *    second device the user never paired — hence
 *    `WHEN_UNLOCKED_THIS_DEVICE_ONLY` rather than the default `WHEN_UNLOCKED`.
 *
 *  - Everything else (host, port, the Mac's name, when we last reached it) is
 *    not secret and goes in a small JSON file. Keeping it out of the keychain
 *    also sidesteps SecureStore's per-value size limit as the list grows.
 *
 * Nothing in this module ever logs a token, and `redactToken` in `lib/pair.ts`
 * is the only thing allowed to put any part of one on screen.
 */

import { Directory, File, Paths } from "expo-file-system";
import * as SecureStore from "expo-secure-store";

export interface SavedConnection {
  /** Stable key. Derived from host+port, and also the keychain entry name. */
  id: string;
  host: string;
  port: number;
  /** Epoch ms of the last request that actually succeeded, or null. */
  lastConnectedAt: number | null;
}

export interface Vault {
  connections: SavedConnection[];
  activeId: string | null;
  /**
   * True once ANY request to a local host has succeeded on this install. It is
   * the single fact that lets `lib/net.ts` tell "iOS blocked us" apart from
   * "the Mac is asleep", so it is stored once for the whole app rather than
   * per connection — the Local Network permission is granted app-wide.
   */
  hasEverConnectedLocally: boolean;
}

const EMPTY: Vault = { connections: [], activeId: null, hasEverConnectedLocally: false };

/** More than this and the picker stops being a list and starts being a
 *  problem; the oldest unused entry is dropped. */
export const MAX_SAVED_CONNECTIONS = 8;

const FILE_NAME = "connections.json";
const KEY_PREFIX = "vae_token_";

/** SecureStore keys allow only alphanumerics, ".", "-" and "_". */
export function connectionId(host: string, port: number): string {
  const safe = host.trim().toLowerCase().replace(/[^a-z0-9.-]/g, "_");
  return `${safe}_${port}`;
}

function file(): File {
  return new File(Paths.document, FILE_NAME);
}

function coerce(parsed: unknown): Vault {
  if (typeof parsed !== "object" || parsed === null) return EMPTY;
  const v = parsed as Partial<Vault>;
  const rows = Array.isArray(v.connections) ? v.connections : [];
  const connections: SavedConnection[] = [];
  for (const row of rows) {
    if (typeof row !== "object" || row === null) continue;
    const r = row as Partial<SavedConnection>;
    if (typeof r.host !== "string" || typeof r.port !== "number") continue;
    connections.push({
      id: typeof r.id === "string" && r.id ? r.id : connectionId(r.host, r.port),
      host: r.host,
      port: r.port,
      lastConnectedAt: typeof r.lastConnectedAt === "number" ? r.lastConnectedAt : null,
    });
  }
  const activeId = typeof v.activeId === "string" && connections.some((c) => c.id === v.activeId)
    ? v.activeId
    : null;
  return { connections, activeId, hasEverConnectedLocally: v.hasEverConnectedLocally === true };
}

export async function loadVault(): Promise<Vault> {
  try {
    const f = file();
    if (!f.exists) return EMPTY;
    return coerce(JSON.parse(await f.text()));
  } catch {
    // A corrupt or unreadable file must not brick the app on launch — the user
    // can always pair again, and the alternative is a permanent crash loop.
    return EMPTY;
  }
}

async function writeVault(next: Vault): Promise<void> {
  const dir = new Directory(Paths.document);
  if (!dir.exists) dir.create({ intermediates: true, idempotent: true });
  const f = file();
  if (!f.exists) f.create({ overwrite: true });
  f.write(JSON.stringify(next));
}

/**
 * The only way anything changes the vault: read, transform, write. Screens
 * never spread a stale in-memory snapshot back onto disk, so marking a
 * connection reachable cannot erase a rename that happened on another screen.
 */
export async function updateVault(fn: (v: Vault) => Vault): Promise<Vault> {
  const next = fn(await loadVault());
  await writeVault(next);
  return next;
}

/** Newest first, so the list a user sees is ordered by when they last used it. */
function byRecency(a: SavedConnection, b: SavedConnection): number {
  return (b.lastConnectedAt ?? 0) - (a.lastConnectedAt ?? 0);
}

/** Add or update a connection and make it the active one. Pure — the caller
 *  hands the result to `updateVault`, which is what makes it testable. */
export function withConnection(v: Vault, conn: SavedConnection): Vault {
  const others = v.connections.filter((c) => c.id !== conn.id);
  const merged = [conn, ...others].sort(byRecency);
  return { ...v, connections: merged.slice(0, MAX_SAVED_CONNECTIONS), activeId: conn.id };
}

export function withoutConnection(v: Vault, id: string): Vault {
  const connections = v.connections.filter((c) => c.id !== id);
  return { ...v, connections, activeId: v.activeId === id ? null : v.activeId };
}

export function withConnectedNow(v: Vault, id: string, at: number): Vault {
  return {
    ...v,
    hasEverConnectedLocally: true,
    connections: v.connections.map((c) => (c.id === id ? { ...c, lastConnectedAt: at } : c)),
  };
}

export function activeConnection(v: Vault): SavedConnection | null {
  return v.connections.find((c) => c.id === v.activeId) ?? null;
}

// ---------------------------------------------------------------------------
// Tokens
// ---------------------------------------------------------------------------

const TOKEN_OPTIONS: SecureStore.SecureStoreOptions = {
  keychainAccessible: SecureStore.WHEN_UNLOCKED_THIS_DEVICE_ONLY,
};

export async function saveToken(id: string, token: string): Promise<void> {
  await SecureStore.setItemAsync(KEY_PREFIX + id, token, TOKEN_OPTIONS);
}

export async function readToken(id: string): Promise<string | null> {
  try {
    return await SecureStore.getItemAsync(KEY_PREFIX + id, TOKEN_OPTIONS);
  } catch {
    // The keychain refuses reads while the device is locked. A missing token
    // is handled everywhere as "not paired yet", which is the right behaviour
    // here too — the next read after unlock succeeds.
    return null;
  }
}

export async function deleteToken(id: string): Promise<void> {
  await SecureStore.deleteItemAsync(KEY_PREFIX + id, TOKEN_OPTIONS);
}

/** Forget a Mac completely: the metadata AND the credential. Used by "Remove"
 *  in the connection list, so a removed pairing leaves nothing behind. */
export async function forgetConnection(id: string): Promise<Vault> {
  await deleteToken(id);
  return updateVault((v) => withoutConnection(v, id));
}
