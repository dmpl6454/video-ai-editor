/**
 * The app's single source of truth.
 *
 * Mirrors the desktop's design on purpose: `dispatch()` is the ONE mutation
 * path, so a gesture on the phone, a gesture on the Mac and a tool call from
 * Claude all end up in the same place and produce the same op log. Nothing in
 * this app mutates an EDL locally and reconciles later.
 *
 * TWO RULES THAT ARE EASY TO GET WRONG AND EXPENSIVE TO GET WRONG:
 *
 * 1. A mutating dispatch that FAILED is never replayed on reconnect. We cannot
 *    know whether the Mac applied it before the connection dropped, and a
 *    replayed `ripple_delete` silently deletes twice. The recovery is always
 *    to refetch the EDL and let the user look — see `dispatch()` below.
 *
 * 2. The Mac and the phone share ONE store and ONE undo stack (main._STORES).
 *    An edit made at the Mac while the phone is looking at the timeline will
 *    not appear until `pollOps()` sees it, and a phone Undo can take back the
 *    Mac's last edit. `pollOps` is how the phone stays honest about that; the
 *    Settings copy says so in words.
 */

import * as Device from "expo-device";
import { create } from "zustand";

import { ApiClient, createClient, type WhoAmI } from "./api";
import {
  connectionMessage,
  initialConnectionState,
  isConnectionFailure,
  mediaEnabled,
  reduce,
  type ConnectionEvent,
  type ConnectionState,
} from "./connection";
import { ApiError } from "./errors";
import { isAsyncDispatchTool, runJob, type JobProgress, type JobOutcome } from "./jobs";
import { baseUrl, DEFAULT_PORT } from "./net";
import type { PairPayload } from "./pair";
import type { DispatchResponse, EDL, Op, SessionInfo } from "./types";
import {
  activeConnection,
  connectionId,
  loadVault,
  readToken,
  saveToken,
  updateVault,
  withConnectedNow,
  withConnection,
  forgetConnection as forgetInVault,
  type SavedConnection,
  type Vault,
} from "./vault";

/** How often the phone asks the Mac what changed. See rule 2 above; 6 s is
 *  frequent enough that a two-person edit does not feel haunted, and light
 *  enough not to compete with a filmstrip for the rate limiter's budget. */
export const OPS_POLL_MS = 6_000;

const EMPTY_VAULT: Vault = { connections: [], activeId: null, hasEverConnectedLocally: false };

/**
 * What this phone calls itself in the Mac's paired-device list. It is the only
 * way a user with two phones can revoke the right one, so a real name is worth
 * asking the OS for — and "iPhone" is a truthful fallback, not a placeholder.
 */
function deviceName(): string {
  return Device.deviceName?.trim() || Device.modelName?.trim() || "iPhone";
}

/**
 * The `since` value to send on the NEXT `getOps` call.
 *
 * `GET /api/sessions/{sid}/ops?since=N` is a SLICE — the Mac answers
 * `store.ops.ops[N:]` — and `seq` equals the index only because
 * `edl/ops_log.py::append` sets `seq = len(self.ops)`. So the cursor is
 * `lastSeq + 1`. Sending `lastSeq` returns that op again on every poll: a
 * fresh project's `init` op (seq 0) came back every six seconds forever,
 * `edit.tsx` could not match it against `ownOpSeqs`, and the timeline showed a
 * permanent, un-dismissable "The project changed while you were looking at it"
 * note about an empty project nobody had touched — while spending three
 * requests per poll instead of one.
 *
 * Exported so a test can pin the arithmetic without a store.
 */
export function nextOpCursor(ops: readonly Op[], fallback = 0): number {
  const last = ops[ops.length - 1];
  return last === undefined ? fallback : last.seq + 1;
}

/** What the UI shows while a tool is running. */
export interface BusyState {
  tool: string;
  jobId: string | null;
  /** null means indeterminate — the handler advertises no progress. */
  progress: number | null;
  status: JobProgress["status"] | null;
  queueDepth: number | null;
  cancellable: boolean;
}

export interface StoreState {
  conn: ConnectionState;
  vault: Vault;
  client: ApiClient | null;

  sessionId: string | null;
  session: SessionInfo | null;
  edl: EDL | null;
  /** The `since` INDEX to ask for next — one past the last op seen, not the
   *  last seq. See `nextOpCursor`. */
  lastOpSeq: number;

  busy: BusyState | null;
  /** Set when a job was aborted from the UI. */
  abort: { aborted: boolean } | null;

  // -- lifecycle ------------------------------------------------------------
  hydrate: () => Promise<void>;
  pairWith: (payload: PairPayload) => Promise<boolean>;
  useSaved: (conn: SavedConnection) => Promise<boolean>;
  probe: () => Promise<boolean>;
  forget: (id: string) => Promise<void>;

  // -- session --------------------------------------------------------------
  openSession: (sid: string) => Promise<void>;
  refreshEdl: () => Promise<void>;
  pollOps: () => Promise<Op[]>;

  // -- the one mutation path ------------------------------------------------
  dispatch: (tool: string, args: Record<string, unknown>, opts?: DispatchOptions) => Promise<DispatchResponse>;
  cancelBusy: () => Promise<void>;
}

export interface DispatchOptions {
  /** From `ToolSchema.cancellable` — never assume it. */
  cancellable?: boolean;
  /** From `ToolSchema.reports_progress` — never assume it. */
  reportsProgress?: boolean;
  /** Skip the EDL refetch when the caller is about to refetch anyway. */
  skipRefresh?: boolean;
}

export const useStore = create<StoreState>((set, get) => {
  /** Feed the connection reducer and keep media loaders in step with it. */
  function connEvent(event: ConnectionEvent): ConnectionState {
    const next = reduce(get().conn, event);
    set({ conn: next });
    return next;
  }

  /** Record a success once, including the durable "we have reached a local
   *  host at least once" fact `lib/net.ts` reasons from. */
  async function noteSuccess(whoami: WhoAmI | null): Promise<void> {
    const at = Date.now();
    const next = connEvent({ type: "success", at, whoami });
    if (next.connectionId) {
      const vault = await updateVault((v) => withConnectedNow(v, next.connectionId as string, at));
      set({ vault });
    }
  }

  function noteFailure(e: unknown): void {
    if (e instanceof ApiError && isConnectionFailure(e)) {
      connEvent({ type: "failure", at: Date.now(), error: e });
      // The media token is bound to the credential and the session that just
      // failed; keeping it would hand stale query tokens to native loaders.
      get().client?.clearMediaToken();
    }
  }

  /** Every call that talks to the Mac goes through here, so success and
   *  failure always update the connection state exactly once. */
  async function guarded<T>(fn: (client: ApiClient) => Promise<T>): Promise<T> {
    const client = get().client;
    if (!client) {
      throw new ApiError({ kind: "unauthorized", message: "This phone is not paired with a Mac yet." });
    }
    try {
      const out = await fn(client);
      if (get().conn.status !== "connected") await noteSuccess(null);
      return out;
    } catch (e) {
      noteFailure(e);
      throw e;
    }
  }

  async function attach(host: string, port: number, token: string, id: string): Promise<boolean> {
    const client = createClient({ baseUrl: baseUrl(host, port), token });
    set({ client });
    connEvent({ type: "select", host, port, connectionId: id });
    connEvent({ type: "attempt" });
    try {
      // `whoami` rather than `health`: health answers without a token, so it
      // would report a green connection for a phone the Mac has revoked.
      const who = await client.whoami();
      await noteSuccess(who);
      return true;
    } catch (e) {
      noteFailure(e);
      return false;
    }
  }

  return {
    conn: initialConnectionState,
    vault: EMPTY_VAULT,
    client: null,
    sessionId: null,
    session: null,
    edl: null,
    lastOpSeq: 0,
    busy: null,
    abort: null,

    /**
     * Boot: read the vault and the keychain, then start the probe WITHOUT
     * waiting for it.
     *
     * `_layout.tsx` holds the splash screen until this resolves, and the probe
     * is a network round trip with a 12 second timeout. On a same-subnet
     * address whose host is asleep, packets go nowhere and it runs the full
     * twelve seconds — so awaiting it meant launching next to a closed lid gave
     * twelve seconds of a featureless dark rectangle, no spinner, no text, no
     * way to cancel, before the Connect screen finally appeared to explain
     * itself. That is the worst possible cold start for an app whose whole
     * contract is that the connection state is always legible.
     *
     * The vault and keychain reads ARE awaited: they are local, they take
     * milliseconds, and they are what `app/index.tsx` needs to route without a
     * flash of the wrong screen. The connection reducer is already in
     * `connecting` by then, so the bar has an honest sentence to show while the
     * probe runs behind a rendered screen.
     */
    async hydrate() {
      const vault = await loadVault();
      set({ vault });
      connEvent({ type: "hydrate", hasEverConnectedLocally: vault.hasEverConnectedLocally });
      const saved = activeConnection(vault);
      if (!saved) return;
      const token = await readToken(saved.id);
      if (!token) return; // paired metadata without a credential: treat as unpaired
      // Point the reducer at this Mac and build the client synchronously, so a
      // screen that mounts in the next frame has both. Only the round trip is
      // deferred.
      const client = createClient({ baseUrl: baseUrl(saved.host, saved.port), token });
      set({ client });
      connEvent({ type: "select", host: saved.host, port: saved.port, connectionId: saved.id });
      connEvent({ type: "attempt" });
      void get().probe();
    },

    /**
     * Redeem a scanned claim code.
     *
     * Two steps, and the order is not negotiable. The code is SINGLE-USE: the
     * Mac burns it the moment `/api/pair/claim` succeeds, and the bearer it
     * returns is the only copy that will ever exist — the Mac keeps a hash.
     * So the token goes into the keychain before anything else can fail, and
     * a probe that then fails leaves a saved connection the user can retry
     * rather than a burnt code and nothing to show for it.
     */
    async pairWith(payload) {
      const id = connectionId(payload.host, payload.port);
      const base = baseUrl(payload.host, payload.port);
      const anonymous = createClient({ baseUrl: base, token: null });

      // Point the reducer at this Mac BEFORE the first request. Without it a
      // claim that fails on the network would be diagnosed against an empty
      // host, and `lib/net.ts` would correctly but uselessly report that ""
      // is not a local address. It also makes the strip read "Connecting to
      // 192.168.1.20…" while the claim is in flight.
      connEvent({ type: "select", host: payload.host, port: payload.port, connectionId: id });
      connEvent({ type: "attempt" });

      let claimed: Awaited<ReturnType<ApiClient["claim"]>>;
      try {
        claimed = await anonymous.claim(payload.code, deviceName());
      } catch (e) {
        noteFailure(e);
        // A bad, used or expired code is a 401 with one deliberate message.
        // Surface it even when it is not a connection-level failure, or the
        // screen would sit on "connecting" with nothing to read.
        if (e instanceof ApiError && !isConnectionFailure(e)) {
          // Rebuilt field by field, NOT by spreading `e`: `Error.message` is a
          // non-enumerable own property, so `{...e}` silently drops it and the
          // user would be shown an empty banner on the one failure that is
          // most likely to happen — a code that is stale or already used.
          connEvent({
            type: "failure",
            at: Date.now(),
            error: new ApiError({
              kind: "unauthorized",
              message: e.message,
              status: e.status,
              requestId: e.requestId,
              retryAfterMs: e.retryAfterMs,
            }),
          });
        }
        return false;
      }

      await saveToken(id, claimed.token);
      const vault = await updateVault((v) =>
        withConnection(v, {
          id,
          host: payload.host,
          port: payload.port,
          lastConnectedAt: null,
        }),
      );
      set({ vault });
      return attach(payload.host, payload.port, claimed.token, id);
    },

    async useSaved(saved) {
      const token = await readToken(saved.id);
      if (!token) {
        connEvent({
          type: "failure",
          at: Date.now(),
          error: new ApiError({
            kind: "unauthorized",
            message: `The saved credential for ${saved.host} is gone. Pair with that Mac again.`,
          }),
        });
        return false;
      }
      return attach(saved.host, saved.port, token, saved.id);
    },

    /**
     * Re-check the Mac. The target of every Retry button in the app.
     *
     * The `client` fallback is not defensive coding — it is a bug fix. During a
     * FIRST pairing, `pairWith` builds its anonymous client as a local and only
     * `attach()` ever calls `set({ client })`, which happens after a successful
     * claim. So when the first claim failed on the network — the iOS Local
     * Network prompt case, which is the single most likely first-run failure —
     * `connect.tsx` rendered a Retry button whose handler returned immediately:
     * no request, no state change, no feedback at all.
     */
    async probe() {
      const { conn } = get();
      if (!conn.host) return false;
      let client = get().client;
      if (!client) {
        client = createClient({ baseUrl: baseUrl(conn.host, conn.port ?? DEFAULT_PORT), token: null });
        set({ client });
      }
      connEvent({ type: "attempt" });
      try {
        const who = await client.whoami();
        await noteSuccess(who);
        return true;
      } catch (e) {
        noteFailure(e);
        return false;
      }
    },

    async forget(id) {
      const vault = await forgetInVault(id);
      set({ vault });
      if (get().conn.connectionId === id) {
        connEvent({ type: "cleared" });
        set({ client: null, sessionId: null, session: null, edl: null, lastOpSeq: 0 });
      }
    },

    async openSession(sid) {
      const session = await guarded((c) => c.getSession(sid));
      const edl = await guarded((c) => c.getEdl(sid));
      // The NEXT index to ask for, not the last seq we have seen. `GET
      // /ops?since=N` slices the log — `store.ops.ops[N:]` — so `since = lastSeq`
      // re-returns the op we already have on every single poll. A fresh project
      // has one op (`init`, seq 0), so this used to hand `edit.tsx` the same
      // `init` every six seconds: an un-dismissable "The project changed while
      // you were looking at it — Initial empty project" banner on a project
      // nobody had touched, plus a needless EDL + session refetch each time.
      set({ sessionId: sid, session, edl, lastOpSeq: nextOpCursor(session.ops) });
    },

    async refreshEdl() {
      const sid = get().sessionId;
      if (!sid) return;
      const [edl, session] = await Promise.all([
        guarded((c) => c.getEdl(sid)),
        guarded((c) => c.getSession(sid)),
      ]);
      set({ edl, session });
    },

    /**
     * Ask what changed on the Mac since we last looked. Returns the new ops so
     * a screen can say WHOSE edit arrived — the shared undo stack means "an
     * edit appeared" is not enough information to act on.
     */
    async pollOps() {
      const { sessionId, lastOpSeq } = get();
      if (!sessionId) return [];
      const { ops } = await guarded((c) => c.getOps(sessionId, lastOpSeq));
      if (ops.length === 0) return [];
      set({ lastOpSeq: nextOpCursor(ops, lastOpSeq) });
      await get().refreshEdl();
      return ops;
    },

    /**
     * Run a tool. THE only way anything in this app changes a project.
     *
     * A failure here is never retried automatically and never replayed on
     * reconnect: the Mac may have applied the edit before the connection
     * dropped, and replaying a `ripple_delete` deletes twice. The recovery is
     * always `refreshEdl()` and letting the user see the real state.
     */
    async dispatch(tool, args, opts = {}) {
      const sid = get().sessionId;
      if (!sid) throw new ApiError({ kind: "rejected", message: "Open a project first." });

      const abort = { aborted: false };
      set({
        abort,
        busy: {
          tool,
          jobId: null,
          progress: opts.reportsProgress === false ? null : 0,
          status: null,
          queueDepth: null,
          cancellable: opts.cancellable === true,
        },
      });

      try {
        let response: DispatchResponse;

        if (isAsyncDispatchTool(tool)) {
          const { job_id } = await guarded((c) => c.dispatchAsync(sid, tool, args));
          set((s) => (s.busy ? { busy: { ...s.busy, jobId: job_id } } : {}));

          const outcome: JobOutcome = await runJob(job_id, {
            getJob: (id) => guarded((c) => c.getJob(id)),
            getQueueDepth: (id) => guarded((c) => c.queueDepthFor(sid, id)),
            reportsProgress: opts.reportsProgress,
            signal: abort,
            onProgress: (p) =>
              set((s) =>
                s.busy
                  ? { busy: { ...s.busy, jobId: p.jobId, progress: p.progress, status: p.status, queueDepth: p.queueDepth } }
                  : {},
              ),
          });

          if (outcome.kind === "cancelled") {
            throw new ApiError({ kind: "cancelled", message: `${tool} was cancelled.` });
          }
          if (outcome.kind === "failed") {
            throw new ApiError({ kind: "rejected", message: outcome.message });
          }
          if (outcome.kind === "timeout") {
            throw new ApiError({ kind: "timeout", message: `${tool} is still running on your Mac. Check back shortly.` });
          }
          if (outcome.kind === "vanished") {
            throw new ApiError({
              kind: "not_found",
              message: `Your Mac has no record of that ${tool} job any more. Check the project — it may have finished.`,
            });
          }
          response = outcome.result as unknown as DispatchResponse;
        } else {
          response = await guarded((c) => c.dispatch(sid, tool, args));
        }

        if (!opts.skipRefresh) await get().refreshEdl();
        return response;
      } finally {
        set({ busy: null, abort: null });
      }
    },

    async cancelBusy() {
      const { busy, abort } = get();
      if (abort) abort.aborted = true;
      if (busy?.jobId && busy.cancellable) {
        // A cancel that the Mac refuses is not worth an error banner — the
        // local abort above already stopped us watching.
        await guarded((c) => c.cancelJob(busy.jobId as string)).catch(() => undefined);
      }
    },
  };
});

/** Selector helpers, so screens do not re-derive these three everywhere. */
export const selectMediaEnabled = (s: StoreState): boolean => mediaEnabled(s.conn);
export const selectConnectionLine = (s: StoreState): string => connectionMessage(s.conn);
export const selectIsConnected = (s: StoreState): boolean => s.conn.status === "connected";
