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

  /**
   * The payload of a pairing whose claim code we have NOT yet spent.
   *
   * It exists because "retry" means two different things on this screen and
   * getting them the wrong way round is how a phone that has never paired gets
   * told it has been revoked. Before the claim succeeds there is no credential
   * anywhere — not in the store, not in the keychain — so the only request that
   * can make progress is the claim itself, and the code is still good because
   * the Mac only burns it on a claim it actually answered. After the claim
   * succeeds the code is spent forever and the bearer is the only thing worth
   * retrying with.
   *
   * Set the moment a pairing starts, cleared the moment the token is saved.
   */
  pendingPair: PairPayload | null;

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
  /** What every Retry should call: resume the pairing, or re-check the Mac. */
  reconnect: () => Promise<boolean>;
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
  /** A claim that is already on the wire, shared by every caller that asks to
   *  pair with the same Mac while it is in flight. See `pairWith`. */
  let claimInFlight: Promise<boolean> | null = null;

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

  /**
   * Redeem a claim code. The two steps, and their order, are not negotiable.
   *
   * The code is SINGLE-USE: the Mac burns it the moment `/api/pair/claim`
   * succeeds, and the bearer it returns is the only copy that will ever exist —
   * the Mac keeps a hash. So the token goes into the keychain before anything
   * else can fail, and a probe that then fails leaves a saved connection the
   * user can retry rather than a burnt code and nothing to show for it.
   */
  async function claimAndAttach(payload: PairPayload): Promise<boolean> {
    const id = connectionId(payload.host, payload.port);
    const base = baseUrl(payload.host, payload.port);
    const anonymous = createClient({ baseUrl: base, token: null });

    // Remember the payload BEFORE the first request, and deliberately do NOT
    // put `anonymous` in the store. Two separate reasons, both learned the
    // hard way:
    //
    //  * `pendingPair` is what makes Retry mean "finish this pairing" instead
    //    of "re-check a Mac we have no credential for". The claim below is the
    //    ONLY request that can make progress until it succeeds, and the code
    //    survives a claim that never reached the Mac.
    //
    //  * The store's `client` is the app's "we hold a credential" signal —
    //    `guarded()` refuses to make any request while it is null. Caching a
    //    token-less client here would turn every guarded call, every retry and
    //    every Retry tap into an unauthenticated request that the Mac answers
    //    401 and counts toward `auth_lockout`, on a phone that has never been
    //    paired. `attach()` sets the client, once, with a bearer.
    set({ pendingPair: payload });

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

    // The code is spent the instant the Mac answered this claim: re-sending it
    // would earn a 401 and tell a phone that has just paired that it has been
    // revoked. From here the bearer is the only credential worth retrying.
    set({ pendingPair: null });

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
  }

  return {
    conn: initialConnectionState,
    vault: EMPTY_VAULT,
    client: null,
    pendingPair: null,
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
     * Redeem a claim code. See `claimAndAttach`; this is the de-duplicating
     * wrapper around it.
     *
     * A second claim for a code the first one is about to burn comes back 401,
     * and 401 is `unauthorized` — "Your Mac no longer recognises this phone" on
     * a phone that paired successfully one millisecond earlier. Two callers can
     * genuinely collide now that the retry loop resumes pairings as well as the
     * Retry button, so the second caller waits on the first answer instead of
     * racing it. (Both callers are always holding the SAME pending payload:
     * the manual Connect button is disabled while a pairing is in flight.)
     */
    async pairWith(payload) {
      const running = claimInFlight;
      if (running) return running;
      const attempt = claimAndAttach(payload).finally(() => {
        claimInFlight = null;
      });
      claimInFlight = attempt;
      return attempt;
    },

    async useSaved(saved) {
      // Choosing a saved Mac abandons any half-finished pairing: resubmitting
      // that code later would dial the wrong host with the wrong credential.
      set({ pendingPair: null });
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
     * Re-check the Mac WITH THE CREDENTIAL WE HOLD. Never without one.
     *
     * `whoami` is deliberately the connection check (see `attach`): it requires
     * the bearer, so a revoked phone cannot show green. That makes calling it
     * anonymously worse than useless — it is a guaranteed 401, and 401 is
     * `kindForStatus`'s `unauthorized`, which the reducer treats as terminal.
     *
     * That is exactly what used to happen on a first pairing. `pairWith` keeps
     * its anonymous client in a local and only `attach()` ever sets one on the
     * store, so `client` was still null when the first claim failed on the
     * network — the iOS Local Network prompt case, where iOS fails the request
     * outright rather than holding it while the prompt is up. `noteFailure`
     * drove the reducer to `retrying`, `useAutoReconnect` armed its 1.5 s
     * timer, and this function then FABRICATED a token-less client and asked
     * `whoami`. The moment the user tapped Allow, that anonymous request came
     * back 401 and a phone that had never been paired was told "Your Mac no
     * longer recognises this phone. Pair it again." `shouldAutoRetry` is false
     * for `unauthorized`, so the retry loop stopped there for good, and the
     * token-less client stayed cached in the store for everything else to use.
     *
     * So: no credential, no request. The keychain read is the rebuild path for
     * a store whose `client` was dropped while a saved pairing is still valid;
     * with no token at all this returns false and leaves the state alone, and
     * `reconnect()` below is what knows the other half — that an unfinished
     * pairing is resumed by claiming, not by probing.
     */
    async probe() {
      const { conn } = get();
      if (!conn.host) return false;

      // `client.token === null` matters as much as `client === null`: a
      // credential-less client must never be promoted into a whoami call just
      // because something happened to leave one in the store.
      let client = get().client;
      if (client === null || client.token === null) {
        if (!conn.connectionId) return false;
        const token = await readToken(conn.connectionId);
        if (!token) return false;
        client = createClient({ baseUrl: baseUrl(conn.host, conn.port ?? DEFAULT_PORT), token });
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

    /**
     * The one target for every retry — the Retry button and the automatic
     * loop alike.
     *
     * A pairing that never got its token is resumed by re-sending the claim:
     * the code is unburnt (the Mac burns it only on a claim it answered), and
     * it is the only request that can produce a credential. Everything else
     * re-checks with the bearer. Wiring Retry straight to `probe()` is what
     * made the first-pairing dead end reachable by hand as well as by timer.
     */
    async reconnect() {
      const pending = get().pendingPair;
      if (pending) return get().pairWith(pending);
      return get().probe();
    },

    async forget(id) {
      const vault = await forgetInVault(id);
      set({ vault });
      if (get().conn.connectionId === id) {
        connEvent({ type: "cleared" });
        // `pendingPair` goes with it: a code claimed against a Mac the user has
        // just removed must not be re-sent by the next retry.
        set({ client: null, pendingPair: null, sessionId: null, session: null, edl: null, lastOpSeq: 0 });
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

/**
 * Whether a retry has anything to retry WITH — read synchronously, because
 * `useAutoReconnect` has to decide whether to arm a timer inside a zustand
 * subscription and cannot await a keychain read there.
 *
 * A store with neither a client nor an unfinished pairing holds no credential
 * for the Mac it is pointed at, and the only thing a probe could send is an
 * anonymous `whoami` — the 401 that told never-paired phones they were
 * revoked. Both halves are set before the reducer can reach a retryable
 * status (`hydrate` and `useSaved` set the client, `pairWith` sets the pending
 * payload), so this gate turns off the loop only when there is genuinely
 * nothing to send.
 */
export const selectCanReconnect = (s: StoreState): boolean =>
  s.pendingPair !== null || s.client !== null;

/** Selector helpers, so screens do not re-derive these three everywhere. */
export const selectMediaEnabled = (s: StoreState): boolean => mediaEnabled(s.conn);
export const selectConnectionLine = (s: StoreState): string => connectionMessage(s.conn);
export const selectIsConnected = (s: StoreState): boolean => s.conn.status === "connected";
