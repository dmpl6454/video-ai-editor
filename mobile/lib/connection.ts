/**
 * What state the link to the Mac is in, and — the harder half — what to SAY
 * about it.
 *
 * The whole app is a remote control for a program running somewhere else. If
 * the connection state is vague, every other failure in the app gets blamed on
 * the wrong thing: a black player reads as a broken render, a queued job reads
 * as a hang, an expired token reads as a lost project. So the states here are
 * deliberately narrow and each one has exactly one sentence attached.
 *
 * `reduce` is a pure function of (state, event). No fetching, no timers, no
 * storage — those live in `lib/store.ts`. That is what lets every branch,
 * including the three local-network branches that cannot be reproduced without
 * a device, be pinned by a unit test.
 */

import { ApiError, type ApiErrorKind } from "./errors";
import { diagnoseLocalNetwork, diagnosisMessage, LOCAL_NETWORK_RETRY_MS } from "./net";
import type { WhoAmI } from "./api";

export type ConnectionStatus =
  /** Nothing paired yet. */
  | "idle"
  /** A request is in flight and we have no recent success to fall back on. */
  | "connecting"
  /** The Mac answered. The ONLY state in which media loaders may run. */
  | "connected"
  /** Failing, but plausibly because iOS is showing the Local Network prompt. */
  | "retrying"
  /** The address is fine and permission is granted; the Mac is not answering. */
  | "unreachable"
  /** iOS is blocking local-network access for this app. */
  | "blocked"
  /** The host is outside the ATS local-network exception; it can never work. */
  | "not_local"
  /** The Mac answered and refused the address we used to reach it (421). */
  | "refused"
  /** The token is gone, wrong, or locked out. */
  | "unauthorized"
  /** The Mac is rate-limiting us. Distinct because it looks like success. */
  | "throttled";

export interface ConnectionState {
  status: ConnectionStatus;
  host: string | null;
  port: number | null;
  /** `vault.connectionId(host, port)` for the Mac we are pointed at. */
  connectionId: string | null;
  /** Epoch ms when the current unbroken run of failures began. */
  failingSince: number | null;
  /** Survives across launches; see `lib/vault.ts` for why it is app-wide. */
  hasEverConnectedLocally: boolean;
  /** Populated on the first success; the source of the "job workers" and
   *  upload-ceiling facts other screens are required to state. */
  whoami: WhoAmI | null;
  /** When the Mac told us to come back (lockout, rate limit). */
  retryAfterMs: number | null;
  lastError: string | null;
}

export const initialConnectionState: ConnectionState = {
  status: "idle",
  host: null,
  port: null,
  connectionId: null,
  failingSince: null,
  hasEverConnectedLocally: false,
  whoami: null,
  retryAfterMs: null,
  lastError: null,
};

export type ConnectionEvent =
  /** Point at a Mac (from a saved connection, or a fresh pairing). */
  | { type: "select"; host: string; port: number; connectionId: string }
  /** A probe is starting. */
  | { type: "attempt" }
  | { type: "success"; at: number; whoami?: WhoAmI | null }
  | { type: "failure"; at: number; error: ApiError }
  /** The user signed out or removed this Mac. */
  | { type: "cleared" }
  /** Restored from disk at launch. */
  | { type: "hydrate"; hasEverConnectedLocally: boolean };

/**
 * Failures that say something about the CONNECTION. Everything else — a 422
 * from one tool, a 403 on one thumbnail, a 404 for a pruned job — is that
 * request's problem, and must not knock the whole app offline. Getting this
 * wrong is how one clip with an external source path takes down the session.
 */
const CONNECTION_KINDS: ReadonlySet<ApiErrorKind> = new Set<ApiErrorKind>([
  "network",
  "timeout",
  "unauthorized",
  "auth_lockout",
  "host_rejected",
  "rate_limited",
]);

export function isConnectionFailure(e: unknown): boolean {
  return e instanceof ApiError && CONNECTION_KINDS.has(e.kind);
}

/**
 * Native video players and image loaders may run ONLY while connected.
 *
 * They are the reason this flag exists rather than screens each deciding for
 * themselves: a filmstrip is two dozen requests fired in under a second, and
 * if they keep firing through a token failure they trip the Mac's lockout
 * instantly — leaving a phone that shows a calm banner over an app that will
 * do nothing at all for the next minute.
 */
export function mediaEnabled(s: ConnectionState): boolean {
  return s.status === "connected";
}

export function reduce(state: ConnectionState, event: ConnectionEvent): ConnectionState {
  switch (event.type) {
    case "hydrate":
      return { ...state, hasEverConnectedLocally: event.hasEverConnectedLocally };

    case "select":
      return {
        ...state,
        status: "connecting",
        host: event.host,
        port: event.port,
        connectionId: event.connectionId,
        failingSince: null,
        whoami: null,
        retryAfterMs: null,
        lastError: null,
      };

    case "attempt":
      // A live connection stays live while we re-check it; dropping to
      // "connecting" on every poll would flicker the banner once a second and
      // stop every media loader in the app along with it.
      return state.status === "connected" ? state : { ...state, status: "connecting" };

    case "success":
      return {
        ...state,
        status: "connected",
        failingSince: null,
        hasEverConnectedLocally: true,
        whoami: event.whoami ?? state.whoami,
        retryAfterMs: null,
        lastError: null,
      };

    case "cleared":
      return { ...initialConnectionState, hasEverConnectedLocally: state.hasEverConnectedLocally };

    case "failure":
      return reduceFailure(state, event.at, event.error);
  }
}

function reduceFailure(state: ConnectionState, at: number, error: ApiError): ConnectionState {
  if (!CONNECTION_KINDS.has(error.kind)) return state;

  const failingSince = state.failingSince ?? at;
  const base = { ...state, failingSince, lastError: error.message };

  // A rejected token is terminal until the user pairs again. A lockout is the
  // same state with a clock on it — critically NOT a state to keep retrying
  // in, since every retry extends the lockout.
  if (error.kind === "unauthorized" || error.kind === "auth_lockout") {
    return { ...base, status: "unauthorized", retryAfterMs: error.retryAfterMs };
  }

  // 429. Looks like success to a naive reducer (we did reach the Mac), which
  // is exactly how a green banner ends up over an app that cannot load a
  // single thumbnail. Media stays off until it clears.
  if (error.kind === "rate_limited") {
    return { ...base, status: "throttled", retryAfterMs: error.retryAfterMs ?? LOCAL_NETWORK_RETRY_MS };
  }

  if (error.kind === "host_rejected") {
    return { ...base, status: "refused", retryAfterMs: null };
  }

  // network / timeout — the ambiguous case. lib/net.ts owns the reasoning.
  const host = state.host ?? "";
  const diagnosis = diagnoseLocalNetwork({
    host,
    hasEverConnectedLocally: state.hasEverConnectedLocally,
    elapsedMs: at - failingSince,
  });

  const status: ConnectionStatus =
    diagnosis.kind === "retry"
      ? "retrying"
      : diagnosis.kind === "permission_denied"
        ? "blocked"
        : diagnosis.kind === "not_local"
          ? "not_local"
          : "unreachable";

  return {
    ...base,
    status,
    // The diagnosis sentence is more useful than "Could not reach your Mac",
    // which is all the transport can ever tell us.
    lastError: diagnosisMessage(diagnosis, host),
    retryAfterMs: diagnosis.kind === "retry" ? diagnosis.afterMs : null,
  };
}

// ---------------------------------------------------------------------------
// What to say, and whether to try again
// ---------------------------------------------------------------------------

/** Whether an automatic retry is worth making. `unauthorized` and `not_local`
 *  are false: nothing changes until the user acts. */
export function shouldAutoRetry(s: ConnectionState): boolean {
  return s.status === "retrying" || s.status === "unreachable" || s.status === "throttled";
}

/** Backoff for the automatic retry, in ms. */
export function retryDelay(s: ConnectionState, at: number): number {
  if (s.retryAfterMs !== null) return s.retryAfterMs;
  if (s.status === "retrying") return LOCAL_NETWORK_RETRY_MS;
  // Unreachable: back off from 2 s toward 30 s so a Mac that is simply asleep
  // is not polled all afternoon, while one that just woke is found quickly.
  const failingFor = s.failingSince === null ? 0 : at - s.failingSince;
  return Math.min(30_000, 2_000 + failingFor / 2);
}

/** The single line shown in the status strip. Never empty, never jargon. */
export function connectionMessage(s: ConnectionState): string {
  const where = s.host ?? "your Mac";
  switch (s.status) {
    case "idle":
      return "Not paired with a Mac yet.";
    case "connecting":
      return `Connecting to ${where}…`;
    case "connected":
      // The Mac sends no name for itself — `whoami.device` names THIS PHONE —
      // so the address is the only honest identifier we have for it.
      return `Connected to ${where}.`;
    case "retrying":
    case "unreachable":
    case "blocked":
    case "not_local":
      return s.lastError ?? `No answer from ${where}.`;
    case "refused":
      return s.lastError ?? `${where} refused the address this phone used to reach it.`;
    case "unauthorized":
      return s.lastError ?? "Your Mac no longer recognises this phone. Pair it again.";
    case "throttled":
      return "Your Mac is asking this phone to slow down. Pausing for a moment.";
  }
}

/**
 * The honesty line. Every screen that needs the Mac renders this when the link
 * is down, because a companion app that hides the fact it is a companion is
 * the specific thing this milestone is not allowed to ship.
 */
export function needsMacNotice(s: ConnectionState): string | null {
  if (s.status === "connected") return null;
  return "This needs your Mac. Nothing is edited, rendered or exported on the phone.";
}
