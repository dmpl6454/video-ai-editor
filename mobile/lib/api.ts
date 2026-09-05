/**
 * The one way this app talks to the Mac.
 *
 * Every request goes through `request()`, which means there is exactly one
 * place that knows how to attach the bearer, one place that turns a non-2xx
 * response into a typed `ApiError`, and one place that can time out. Screens
 * never call `fetch`.
 *
 * THREE THINGS ARE NOT OBVIOUS AND MATTER A LOT:
 *
 * 1. `X-VAE-Client: 1` goes on every request. It is not informational — it is
 *    a header no simple form or image tag can set, so its presence forces a
 *    CORS preflight that the Mac's origin allowlist then denies. That is what
 *    stops a web page the user happens to be visiting from driving their
 *    editor through the same LAN endpoint this app uses.
 *
 * 2. Media (preview video, waveform, thumbnails) is loaded by NATIVE players
 *    and image loaders, not by this module, and those do not reliably keep a
 *    custom Authorization header across a redirect or a range request. So
 *    media has two independent ways in: `mediaHeaders()` for loaders that do
 *    honour headers, and a short-lived `?k=` token accepted only on media
 *    paths. Both, because neither can be verified without a device.
 *
 * 3. Media requests are excluded from the Mac's failed-auth counter, and this
 *    module keeps them on a separate path for the same reason: a filmstrip
 *    fires two dozen requests in well under a second, and if a stale token
 *    made each one a 401 the phone would trip a lockout instantly and then sit
 *    behind a green "connected" banner for a minute doing nothing.
 */

import { ApiError, apiErrorFromResponse, apiErrorFromThrow, type ApiErrorKind } from "./errors";
import type {
  DispatchResponse,
  EDL,
  FeatureReport,
  Job,
  Op,
  SessionInfo,
  SessionSummaryRow,
  ToolSchema,
} from "./types";

/** Plenty for a JSON round trip on a LAN; short enough that a sleeping Mac is
 *  reported in seconds rather than after iOS's own 60-second default. */
export const DEFAULT_TIMEOUT_MS = 12_000;

/** A synchronous dispatch can legitimately run for a while (a render, a probe).
 *  Anything longer than this belongs on the job queue. */
export const DISPATCH_TIMEOUT_MS = 90_000;

/** Media tokens are minted with a 60 s life; renew early so an in-flight
 *  filmstrip never straddles the boundary. */
const MEDIA_TOKEN_RENEW_BEFORE_MS = 15_000;

/** What `probeMedia` learned. `kind` is null only when the probe succeeded. */
export interface MediaProbeResult {
  ok: boolean;
  kind: ApiErrorKind | null;
  detail: string;
}

/** `api/pairing.py::server_info` — the facts the phone sets expectations from. */
export interface ServerInfo {
  version: string;
  lan_enabled: boolean;
  auth_required: boolean;
  /**
   * api/jobs.py runs two workers, SHARED with whoever is sitting at the Mac.
   * Surfaced so the AI and Export screens can say why a job is sitting in a
   * queue rather than letting it read as a stalled render.
   */
  job_workers: number;
  /** Byte ceiling on an upload, so Import can refuse before sending 4 GB. */
  max_upload_bytes: number;
  media_token_ttl_s: number;
}

/** This phone as the Mac has it recorded. Null for the desktop's own UI. */
export interface PairedDevice {
  id: string;
  name: string;
  created_at: number;
}

/** `GET /api/pair/whoami`. */
export interface WhoAmI {
  device: PairedDevice | null;
  /** True for the desktop's own frontend, which is trusted by address. */
  loopback: boolean;
  server: ServerInfo;
}

/** `POST /api/pair/claim` — the only time the bearer token ever crosses the
 *  wire. There is no second copy on the Mac; it is stored hashed there. */
export interface ClaimResult extends PairedDevice {
  token: string;
  server: ServerInfo;
}

export interface ClientConfig {
  /** e.g. "http://10.120.2.82:8765" — build it with `net.baseUrl`.
   *
   *  The `Host` header is deliberately NOT set by hand anywhere in this file.
   *  iOS builds it from the URL authority, which is exactly the `host:port`
   *  the Mac's Host allowlist expects; `Host` is also a forbidden header name
   *  for `fetch`, so setting it would at best be ignored. Getting the origin
   *  right is therefore entirely a matter of getting `baseUrl` right. */
  baseUrl: string;
  /** The pairing bearer, or null before pairing / after revocation. */
  token: string | null;
}

interface RequestOptions {
  body?: unknown;
  timeoutMs?: number;
  signal?: AbortSignal;
  /** Send no Authorization header and no client header — used only by the
   *  media-token probe, which deliberately authenticates via the query. */
  unauthenticated?: boolean;
  headers?: Record<string, string>;
  /** Read the response as text rather than JSON (the media probe). */
  raw?: boolean;
}

interface MediaToken {
  token: string;
  /** Epoch ms. The Mac sends `expires_at` in epoch SECONDS. */
  expiresAt: number;
}

export class ApiClient {
  readonly baseUrl: string;
  readonly token: string | null;
  private readonly cfg: ClientConfig;
  private mediaToken: MediaToken | null = null;
  private mediaTokenInFlight: Promise<string | null> | null = null;

  constructor(cfg: ClientConfig) {
    this.cfg = cfg;
    this.baseUrl = cfg.baseUrl.replace(/\/+$/, "");
    this.token = cfg.token;
  }

  /** A copy pointed at the same Mac with a different credential. Immutable
   *  rather than a setter so a request already in flight cannot change
   *  identity halfway through, and so the cached media token — which is tied
   *  to the old credential — is dropped with it. */
  withToken(token: string | null): ApiClient {
    return new ApiClient({ ...this.cfg, token });
  }

  private baseHeaders(opts: RequestOptions): Record<string, string> {
    const headers: Record<string, string> = { accept: "application/json" };
    if (!opts.unauthenticated) {
      // See note 1 in the file header. This is a security control.
      headers["X-VAE-Client"] = "1";
      if (this.token) headers.authorization = `Bearer ${this.token}`;
    }
    if (opts.body !== undefined && !(opts.body instanceof FormData)) {
      headers["content-type"] = "application/json";
    }
    return { ...headers, ...opts.headers };
  }

  async request<T>(method: string, path: string, opts: RequestOptions = {}): Promise<T> {
    const controller = new AbortController();
    const timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    // WHY THE FLAG: both a timeout and a caller cancel come back from fetch as
    // the SAME abort, and `apiErrorFromThrow` cannot tell them apart —
    // so every timeout used to be reported as `kind: "cancelled"`. "cancelled"
    // is not in `connection.ts::CONNECTION_KINDS`, so `reduceFailure` returned
    // the state unchanged and a sleeping Mac never moved the connection off
    // "Connected to 192.168.1.20." Screens compound it: `edit.tsx` only shows a
    // banner `if (!isCancellation(e))`, so a 90-second dispatch against a
    // closed lid failed in complete silence. Recording which abort fired is the
    // whole fix — `"timeout"` is already a connection kind with its own
    // sentence, so the reducer, the banner and the retry path all start working.
    //
    // AND WHY THE FLAG IS NOW THE ONLY EVIDENCE THAT COUNTS: the fix above was
    // still leaning on `apiErrorFromThrow` recognising an abort by its shape,
    // and that recognition was written for React Native's whatwg-fetch. expo
    // installs its own `fetch` over the top of RN's, and an expo/fetch abort
    // has `name === "Error"` and a "fetch failed: …" message, so the shape
    // check matched nothing in a real build and BOTH branches were dead —
    // timeouts and cancels alike landed on `kind: "network"`. So the two facts
    // only this method holds are now passed explicitly and consulted FIRST.
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, timeoutMs);
    // A caller-supplied signal has to be able to abort us too, without
    // clobbering the timeout — hence the listener rather than passing it on.
    const onAbort = () => controller.abort();
    opts.signal?.addEventListener("abort", onAbort);

    try {
      const res = await fetch(`${this.baseUrl}${path}`, {
        method,
        headers: this.baseHeaders(opts),
        body:
          opts.body === undefined
            ? undefined
            : opts.body instanceof FormData
              ? opts.body
              : JSON.stringify(opts.body),
        signal: controller.signal,
      });

      if (!res.ok) {
        const text = await res.text().catch(() => "");
        throw apiErrorFromResponse(res.status, text, {
          requestId: res.headers.get("x-request-id"),
          retryAfter: res.headers.get("retry-after"),
        });
      }

      if (opts.raw) return (await res.text()) as unknown as T;
      if (res.status === 204) return undefined as unknown as T;
      return (await res.json()) as T;
    } catch (e) {
      // The caller's own signal wins when both fired: a user who tapped Cancel
      // should not be told their Mac is unreachable, and `cancelled` keeps the
      // failure out of `connection.ts::CONNECTION_KINDS` — so the connection
      // stays up, the media token survives, and no red banner appears for an
      // action the user deliberately took.
      const callerAborted = opts.signal?.aborted === true;
      throw apiErrorFromThrow(e, {
        timedOut: timedOut && !callerAborted,
        cancelled: callerAborted,
        timeoutMs,
      });
    } finally {
      clearTimeout(timer);
      opts.signal?.removeEventListener("abort", onAbort);
    }
  }

  // -------------------------------------------------------------------------
  // Identity and health
  // -------------------------------------------------------------------------

  health() {
    return this.request<{ ok: boolean; version?: string }>("GET", "/api/health");
  }

  version() {
    return this.request<{ version: string; build: string }>("GET", "/api/version");
  }

  /** Who the Mac thinks we are, plus the few facts the UI has to state
   *  honestly (worker count, upload ceiling). Requires the bearer, which is
   *  why it — and not `/api/health` — is what a connection check calls. */
  whoami() {
    return this.request<WhoAmI>("GET", "/api/pair/whoami");
  }

  /**
   * Exchange a scanned claim code for this device's bearer token.
   *
   * Sent with no Authorization header, because at this point there is nothing
   * to send: the code IS the credential, it is single-use, and the Mac burns
   * it here. The returned token is the only copy that will ever exist — the
   * Mac stores just a hash — so a caller that drops it has to pair again.
   */
  claim(code: string, deviceName: string) {
    return this.request<ClaimResult>("POST", "/api/pair/claim", {
      body: { code, device_name: deviceName },
    });
  }

  // -------------------------------------------------------------------------
  // Sessions
  // -------------------------------------------------------------------------

  listSessions() {
    return this.request<{ sessions: SessionSummaryRow[] }>("GET", "/api/sessions");
  }

  createSession(name?: string) {
    return this.request<{ id: string; name: string }>("POST", "/api/sessions", { body: { name } });
  }

  getSession(sid: string) {
    return this.request<SessionInfo>("GET", `/api/sessions/${sid}`);
  }

  getEdl(sid: string) {
    return this.request<EDL>("GET", `/api/sessions/${sid}/edl`);
  }

  /**
   * Ops from `since` onward.
   *
   * `since` is an INDEX into the op log, not a `seq` to search for: the Mac
   * answers with `store.ops.ops[since:]` (main.py::get_ops) and `seq` happens
   * to equal that index only because `edl/ops_log.py::append` sets
   * `seq = len(self.ops)`. So the cursor to send next is the last op's
   * `seq + 1`, never its `seq` — passing the seq re-returns that same op on
   * every poll, forever. See `store.ts::pollOps`.
   */
  getOps(sid: string, since = 0) {
    return this.request<{ ops: Op[] }>("GET", `/api/sessions/${sid}/ops?since=${since}`);
  }

  // -------------------------------------------------------------------------
  // The one mutation path
  // -------------------------------------------------------------------------

  /** Synchronous dispatch: blocks on the Mac until the tool returns. */
  dispatch(sid: string, tool: string, args: Record<string, unknown>) {
    return this.request<DispatchResponse>("POST", `/api/sessions/${sid}/dispatch?wait=1`, {
      body: { tool, args },
      timeoutMs: DISPATCH_TIMEOUT_MS,
    });
  }

  /** Asynchronous dispatch: 202 and a job id. See `lib/jobs.ts::runJob`. */
  dispatchAsync(sid: string, tool: string, args: Record<string, unknown>) {
    return this.request<{ job_id: string; status_url?: string }>(
      "POST",
      `/api/sessions/${sid}/dispatch?wait=0`,
      { body: { tool, args } },
    );
  }

  // -------------------------------------------------------------------------
  // Jobs
  // -------------------------------------------------------------------------

  getJob(jobId: string) {
    return this.request<Job>("GET", `/api/jobs/${jobId}`);
  }

  cancelJob(jobId: string) {
    return this.request<{ cancelled: boolean }>("POST", `/api/jobs/${jobId}/cancel`);
  }

  /**
   * Recent jobs for one session.
   *
   * SESSION-SCOPED because that is the only jobs COLLECTION the Mac has.
   * This used to call `GET /api/jobs`, which is not a route — `main.py` exposes
   * `/api/jobs/{job_id}`, `/api/jobs/{job_id}/cancel` and
   * `/api/sessions/{sid}/jobs`, and nothing at the collection path. So every
   * call 404'd, `runJob` swallowed it, `queueDepth` was permanently null, and
   * every queued render on the AI, preview and export screens showed
   * `jobStatusLine`'s fallback "Waiting for your Mac to start this." instead of
   * "Waiting on your Mac — 3 jobs ahead." — the exact honesty the Mac ships
   * `job_workers` in `whoami` to support. It also spent one wasted request per
   * 700 ms poll for the whole time a job sat queued.
   */
  listJobs(sid: string) {
    return this.request<{ jobs: Job[] }>("GET", `/api/sessions/${sid}/jobs`);
  }

  /**
   * How many jobs are ahead of this one. Counted from the queue rather than
   * assumed, because the Mac's workers are shared with the person sitting at it
   * and "3 ahead" is the difference between a bug report and a user who
   * understands why nothing is happening.
   */
  async queueDepthFor(sid: string, jobId: string): Promise<number | null> {
    const { jobs } = await this.listJobs(sid);
    const mine = jobs.find((j) => j.id === jobId);
    if (!mine) return null;
    return jobs.filter(
      (j) => j.id !== jobId && (j.status === "queued" || j.status === "running") && j.created_at < mine.created_at,
    ).length;
  }

  // -------------------------------------------------------------------------
  // Catalogue
  // -------------------------------------------------------------------------

  getTools() {
    return this.request<{ tools: ToolSchema[] }>("GET", "/api/tools");
  }

  getFeatures() {
    return this.request<FeatureReport>("GET", "/api/features");
  }

  // -------------------------------------------------------------------------
  // Media
  // -------------------------------------------------------------------------

  /** Headers for a native loader that honours them. Deliberately minimal: an
   *  `AVURLAsset` that drops everything but `Authorization` still works. */
  mediaHeaders(): Record<string, string> {
    return this.token ? { authorization: `Bearer ${this.token}` } : {};
  }

  /**
   * A short-lived token the Mac accepts in the query string, on media paths
   * only. This exists because header-based auth on native media loaders cannot
   * be verified from this machine — there is no simulator here — and a black
   * player with no explanation is the single worst failure this app could ship.
   */
  private async ensureMediaToken(): Promise<string | null> {
    const now = Date.now();
    if (this.mediaToken && this.mediaToken.expiresAt - now > MEDIA_TOKEN_RENEW_BEFORE_MS) {
      return this.mediaToken.token;
    }
    // Collapse concurrent callers (a filmstrip asks 24 times at once) onto one
    // mint, or we would spend the rate-limit budget on tokens.
    if (!this.mediaTokenInFlight) {
      this.mediaTokenInFlight = this.request<{ token: string; expires_at: number; ttl_s: number }>(
        "POST",
        "/api/pair/media_token",
      )
        .then((r) => {
          // `expires_at` is epoch SECONDS from the Mac's clock. Deriving the
          // deadline from `ttl_s` and OUR clock instead means a phone whose
          // time is minutes off does not throw away every token on arrival.
          this.mediaToken = { token: r.token, expiresAt: Date.now() + r.ttl_s * 1000 };
          return this.mediaToken.token;
        })
        .catch(() => null)
        .finally(() => {
          this.mediaTokenInFlight = null;
        });
    }
    return this.mediaTokenInFlight;
  }

  /**
   * An absolute URL a native loader can be handed. Pass the API path
   * (`/api/sessions/x/thumb?src=…`); the media token is appended if we have
   * one, and the caller should ALSO apply `mediaHeaders()` where it can.
   */
  async mediaUrl(path: string): Promise<string> {
    const k = await this.ensureMediaToken();
    if (!k) return `${this.baseUrl}${path}`;
    const sep = path.includes("?") ? "&" : "?";
    return `${this.baseUrl}${path}${sep}k=${encodeURIComponent(k)}`;
  }

  /**
   * When the `?k=` token currently baked into signed URLs stops being accepted,
   * as epoch ms on THIS phone's clock. Null when there is no token.
   *
   * Callers that CACHE a signed URL need this. The token lives sixty seconds
   * (`api/pairing.py::MEDIA_TOKEN_TTL_S`) and `verify_media_token` refuses it
   * the moment it expires, so a URL held in a map across a scroll is a URL that
   * will 401 — see `TimelineStrip::useThumbUrls`, which cached them for the
   * whole session and then recorded the resulting 401 as a permanent failure.
   */
  mediaTokenExpiresAt(): number | null {
    return this.mediaToken?.expiresAt ?? null;
  }

  /** Drop the cached media token — call this the moment auth changes. */
  clearMediaToken(): void {
    this.mediaToken = null;
  }

  /**
   * Check that a request authenticated ONLY by the `?k=` query is accepted on
   * a REAL media path — the caller passes the thumbnail or preview URL it is
   * about to hand a native loader.
   *
   * It deliberately does not default to some cheap endpoint of its own. The
   * media token is accepted on media paths only, so probing `/api/health`
   * would either be refused in a perfectly healthy system (a false alarm) or
   * succeed because health needs no auth at all (a green light that proved
   * nothing). A probe that can report "fine" without having tested anything is
   * worse than no probe, so this one insists on the path that matters.
   *
   * It is still not a test of AVFoundation — nothing on this machine can be —
   * but it does prove the half of the story that lives on the Mac, so a later
   * black player can be reported as "the player" and not "the app".
   */
  async probeMedia(mediaPath: string): Promise<MediaProbeResult> {
    const k = await this.ensureMediaToken();
    if (!k) {
      return { ok: false, kind: "unauthorized", detail: "Your Mac did not issue a media token." };
    }
    const sep = mediaPath.includes("?") ? "&" : "?";
    try {
      await this.request<string>("GET", `${mediaPath}${sep}k=${encodeURIComponent(k)}`, {
        unauthenticated: true,
        raw: true,
        timeoutMs: 6_000,
      });
      return { ok: true, kind: null, detail: "Video and thumbnails should load." };
    } catch (e) {
      // `kind` is reported, not just interpolated into a sentence. A 403 means
      // the Mac answered and enforced a boundary — a permanent fact about ONE
      // path — while a 401 or a network blip is transient and must not be
      // recorded as permanent. See `TimelineStrip::onThumbError`.
      const kind = e instanceof ApiError ? e.kind : "network";
      return { ok: false, kind, detail: `Media requests were refused (${kind}).` };
    }
  }
}

export function createClient(cfg: ClientConfig): ApiClient {
  return new ApiClient(cfg);
}
