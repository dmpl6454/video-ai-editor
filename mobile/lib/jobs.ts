/**
 * Running a long tool as a background job and watching it finish.
 *
 * The Mac has TWO job workers (api/jobs.py), and they are shared with whoever
 * is sitting at the Mac. One export plus one upscale is enough to park every
 * other job in `queued` at 0% for minutes. A client that renders "queued" the
 * same as "running at 0%" makes that look like a broken render, so `queued` is
 * surfaced as its own state here, with the queue depth when we can get it.
 *
 * The polling loop is deliberately parameterised over its clock, its sleep and
 * its fetches so that every one of those behaviours can be tested without a
 * network or a real second passing.
 */

import type { Job, JobStatus } from "./types";

/**
 * Tools that load a torch/ONNX model and walk every frame. Run on the
 * synchronous endpoint they hold a request-thread worker for minutes, which is
 * what makes the rest of the app stop responding mid-operation. These go
 * through the job queue instead.
 *
 * MUST stay in step with `main.py`'s ASYNC_DISPATCH_TOOLS (the backend permits
 * `wait=0` for any tool, so drift here costs latency, not correctness).
 * `tests/test_mobile_catalog_drift.py` on the Mac side pins the two together.
 */
export const ASYNC_DISPATCH_TOOLS: ReadonlySet<string> = new Set([
  "remove_background",
  "object_erase",
  "upscale",
  "stabilize",
  "smooth_slow_motion",
  "vocal_isolate",
  "instrumental_isolate",
  "motion_track",
  "auto_caption",
  "multicam",
]);

export function isAsyncDispatchTool(tool: string): boolean {
  return ASYNC_DISPATCH_TOOLS.has(tool);
}

/** Matches the desktop's cadence. Fast enough to feel live, slow enough that a
 *  filmstrip and a poll do not fight for the same rate-limit bucket. */
export const JOB_POLL_MS = 700;

/**
 * A poll interval longer than this is assumed to be the OS having suspended
 * us, not time the job spent working, and is not counted toward `maxMs`.
 *
 * WHY: iOS suspends the app the moment it is backgrounded, and every timer
 * with it. A twelve-minute export that succeeded while the phone was in a
 * pocket would otherwise be reported as a timeout the instant the user came
 * back — the one outcome that is both wrong and infuriating. Clamping each
 * observed interval means `maxMs` measures time we actually watched.
 */
const SUSPEND_CLAMP_MS = JOB_POLL_MS * 4;

export interface JobProgress {
  jobId: string;
  status: JobStatus;
  /** 0..1, or null when the handler advertises no `reports_progress` and the
   *  bar should be indeterminate rather than confidently stuck at zero. */
  progress: number | null;
  /** Jobs ahead of this one while it is queued, when we could count them. */
  queueDepth: number | null;
}

export type JobOutcome =
  | { kind: "completed"; result: Record<string, unknown> | null }
  | { kind: "failed"; message: string }
  | { kind: "cancelled" }
  /**
   * The Mac has no record of this job. It prunes finished jobs, so this is
   * usually a job that COMPLETED while we were suspended and then aged out —
   * which is why it is not folded into `failed`: the honest thing to tell the
   * user is that we lost track of it, and to go and look at the result.
   */
  | { kind: "vanished" }
  | { kind: "timeout" };

export interface RunJobOptions {
  /** Fetch the job. Must reject with an ApiError whose kind is "not_found"
   *  when the Mac has pruned it. */
  getJob: (jobId: string) => Promise<Job>;
  /** Count how many jobs sit ahead of this one. Called only while queued. */
  getQueueDepth?: (jobId: string) => Promise<number | null>;
  onProgress?: (p: JobProgress) => void;
  /** Whether this tool reports real progress (ToolSchema.reports_progress). */
  reportsProgress?: boolean;
  /** Give up after this much OBSERVED time. Omit to wait indefinitely. */
  maxMs?: number;
  /** Injected for tests. */
  sleep?: (ms: number) => Promise<void>;
  now?: () => number;
  /** Set to stop polling; resolves as "cancelled". */
  signal?: { aborted: boolean };
}

function isNotFound(e: unknown): boolean {
  return typeof e === "object" && e !== null && (e as { kind?: unknown }).kind === "not_found";
}

const defaultSleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/**
 * Watch a job to a terminal state. Never throws for a job-level failure — the
 * outcome union carries it — so a caller cannot accidentally treat "the render
 * failed" and "the network died" as the same thing. A transport failure DOES
 * throw, because that is the connection reducer's business, not this loop's.
 */
export async function runJob(jobId: string, opts: RunJobOptions): Promise<JobOutcome> {
  const sleep = opts.sleep ?? defaultSleep;
  const now = opts.now ?? Date.now;

  let observedMs = 0;
  let last = now();

  for (;;) {
    if (opts.signal?.aborted) return { kind: "cancelled" };

    let job: Job;
    try {
      job = await opts.getJob(jobId);
    } catch (e) {
      if (isNotFound(e)) return { kind: "vanished" };
      throw e;
    }

    let queueDepth: number | null = null;
    if (job.status === "queued" && opts.getQueueDepth) {
      // A failure to count the queue must never fail the job we are watching;
      // the depth is a nicety and its absence renders as "waiting".
      queueDepth = await opts.getQueueDepth(jobId).catch(() => null);
    }

    opts.onProgress?.({
      jobId,
      status: job.status,
      progress: opts.reportsProgress === false ? null : (job.progress ?? 0),
      queueDepth,
    });

    if (job.status === "completed") return { kind: "completed", result: job.result };
    if (job.status === "failed") return { kind: "failed", message: job.error ?? "The job failed." };
    if (job.status === "cancelled") return { kind: "cancelled" };

    await sleep(JOB_POLL_MS);

    const t = now();
    observedMs += Math.min(t - last, SUSPEND_CLAMP_MS);
    last = t;
    if (opts.maxMs !== undefined && observedMs >= opts.maxMs) {
      // One last look before giving up: the job may have finished during the
      // sleep that pushed us over the line.
      try {
        const final = await opts.getJob(jobId);
        if (final.status === "completed") return { kind: "completed", result: final.result };
        if (final.status === "failed") return { kind: "failed", message: final.error ?? "The job failed." };
        if (final.status === "cancelled") return { kind: "cancelled" };
      } catch (e) {
        if (isNotFound(e)) return { kind: "vanished" };
        throw e;
      }
      return { kind: "timeout" };
    }
  }
}

/** One line describing where a job is, for a status strip. */
export function jobStatusLine(p: JobProgress): string {
  if (p.status === "queued") {
    if (p.queueDepth === null) return "Waiting for your Mac to start this.";
    if (p.queueDepth <= 0) return "Next in line on your Mac.";
    return `Waiting on your Mac — ${p.queueDepth} job${p.queueDepth === 1 ? "" : "s"} ahead.`;
  }
  if (p.status === "running") {
    if (p.progress === null) return "Working on your Mac…";
    return `Working on your Mac — ${Math.round(p.progress * 100)}%.`;
  }
  if (p.status === "completed") return "Done.";
  if (p.status === "cancelled") return "Cancelled.";
  return "Failed.";
}
