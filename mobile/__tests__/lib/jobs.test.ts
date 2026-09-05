/**
 * The job-watching loop. Two behaviours here are worth more than the rest:
 * `queued` being distinguishable from "running at 0%", and a suspended app not
 * reporting a successful twelve-minute export as a timeout.
 */

import { ApiError } from "../../lib/errors";
import {
  ASYNC_DISPATCH_TOOLS,
  isAsyncDispatchTool,
  JOB_POLL_MS,
  jobStatusLine,
  runJob,
  type JobProgress,
} from "../../lib/jobs";
import type { Job, JobStatus } from "../../lib/types";

function job(status: JobStatus, extra: Partial<Job> = {}): Job {
  return {
    id: "j1",
    kind: "dispatch",
    status,
    progress: 0,
    result: null,
    error: null,
    created_at: 0,
    started_at: null,
    completed_at: null,
    session_id: "s_1",
    ...extra,
  };
}

/** A clock that only moves when the loop sleeps — no real time passes. */
function fakeClock() {
  let t = 0;
  return {
    now: () => t,
    sleep: async (ms: number) => {
      t += ms;
    },
    advance: (ms: number) => {
      t += ms;
    },
  };
}

describe("ASYNC_DISPATCH_TOOLS", () => {
  it("matches main.py's frozenset exactly", () => {
    expect([...ASYNC_DISPATCH_TOOLS].sort()).toEqual(
      [
        "auto_caption", "instrumental_isolate", "motion_track", "multicam", "object_erase",
        "remove_background", "smooth_slow_motion", "stabilize", "upscale", "vocal_isolate",
      ].sort(),
    );
    expect(ASYNC_DISPATCH_TOOLS.size).toBe(10);
  });

  it("answers for a tool that is not in it", () => {
    expect(isAsyncDispatchTool("upscale")).toBe(true);
    expect(isAsyncDispatchTool("add_text")).toBe(false);
  });
});

describe("runJob", () => {
  it("resolves with the result when the job completes", async () => {
    const clock = fakeClock();
    const statuses: JobStatus[] = ["queued", "running", "completed"];
    let i = 0;
    const outcome = await runJob("j1", {
      getJob: async () => job(statuses[i++] as JobStatus, { result: { edl_hash: "abc" } }),
      ...clock,
    });
    expect(outcome).toEqual({ kind: "completed", result: { edl_hash: "abc" } });
  });

  it("reports queued as its own state, with the depth ahead of it", async () => {
    const clock = fakeClock();
    const seen: JobProgress[] = [];
    let i = 0;
    await runJob("j1", {
      getJob: async () => job(i++ === 0 ? "queued" : "completed"),
      getQueueDepth: async () => 3,
      onProgress: (p) => seen.push(p),
      ...clock,
    });
    expect(seen[0]?.status).toBe("queued");
    expect(seen[0]?.queueDepth).toBe(3);
    // The depth is only asked for while queued; a running job must not spend a
    // rate-limit slot on it.
    expect(seen[1]?.queueDepth).toBeNull();
  });

  it("does not fail the job when the queue count fails", async () => {
    const clock = fakeClock();
    let i = 0;
    const outcome = await runJob("j1", {
      getJob: async () => job(i++ === 0 ? "queued" : "completed"),
      getQueueDepth: async () => {
        throw new Error("no");
      },
      ...clock,
    });
    expect(outcome.kind).toBe("completed");
  });

  it("reports null progress for a handler that advertises none", async () => {
    const clock = fakeClock();
    const seen: JobProgress[] = [];
    await runJob("j1", {
      getJob: async () => job("completed"),
      reportsProgress: false,
      onProgress: (p) => seen.push(p),
      ...clock,
    });
    // A confident 0% that never moves reads as a hang; indeterminate is honest.
    expect(seen[0]?.progress).toBeNull();
  });

  it("returns a failure with the Mac's own message", async () => {
    const clock = fakeClock();
    const outcome = await runJob("j1", {
      getJob: async () => job("failed", { error: "upscale only supports media clips" }),
      ...clock,
    });
    expect(outcome).toEqual({ kind: "failed", message: "upscale only supports media clips" });
  });

  it("returns cancelled when the Mac cancels", async () => {
    const clock = fakeClock();
    expect(await runJob("j1", { getJob: async () => job("cancelled"), ...clock })).toEqual({ kind: "cancelled" });
  });

  it("returns cancelled immediately when the caller aborts", async () => {
    const clock = fakeClock();
    const outcome = await runJob("j1", {
      getJob: async () => job("running"),
      signal: { aborted: true },
      ...clock,
    });
    expect(outcome).toEqual({ kind: "cancelled" });
  });

  it("returns 'vanished' rather than 'failed' for a pruned job", async () => {
    const clock = fakeClock();
    const outcome = await runJob("j1", {
      getJob: async () => {
        throw new ApiError({ kind: "not_found", message: "gone" });
      },
      ...clock,
    });
    // A pruned job usually SUCCEEDED and then aged out; calling that a failure
    // would tell the user their export broke when it is sitting on the Mac.
    expect(outcome).toEqual({ kind: "vanished" });
  });

  it("rethrows a transport failure instead of swallowing it as a job outcome", async () => {
    const clock = fakeClock();
    await expect(
      runJob("j1", {
        getJob: async () => {
          throw new ApiError({ kind: "network", message: "no route" });
        },
        ...clock,
      }),
    ).rejects.toThrow("no route");
  });

  it("times out on observed time", async () => {
    const clock = fakeClock();
    const outcome = await runJob("j1", {
      getJob: async () => job("running"),
      maxMs: JOB_POLL_MS * 3,
      ...clock,
    });
    expect(outcome).toEqual({ kind: "timeout" });
  });

  it("does not count time the OS suspended us against the timeout", async () => {
    /**
     * The scenario this exists for: a twelve-minute export, the phone in a
     * pocket. Wall-clock accounting would report the successful export as a
     * timeout the moment the user came back.
     */
    const clock = fakeClock();
    let polls = 0;
    const outcome = await runJob("j1", {
      getJob: async () => {
        polls += 1;
        if (polls > 3) return job("completed", { result: { ok: true } });
        return job("running");
      },
      sleep: async (ms) => {
        clock.advance(ms);
        // Ten minutes of suspension between two polls.
        clock.advance(600_000);
      },
      now: clock.now,
      maxMs: 10_000,
    });
    expect(outcome).toEqual({ kind: "completed", result: { ok: true } });
  });

  it("takes one last look before declaring a timeout", async () => {
    const clock = fakeClock();
    let polls = 0;
    const outcome = await runJob("j1", {
      getJob: async () => {
        polls += 1;
        // Finishes exactly on the poll that would have tripped the timeout.
        return polls >= 2 ? job("completed", { result: { late: true } }) : job("running");
      },
      maxMs: JOB_POLL_MS,
      ...clock,
    });
    expect(outcome).toEqual({ kind: "completed", result: { late: true } });
  });
});

describe("jobStatusLine", () => {
  const base = { jobId: "j1", progress: null, queueDepth: null } as const;

  it("says why nothing is happening while queued", () => {
    expect(jobStatusLine({ ...base, status: "queued", queueDepth: 3 })).toBe(
      "Waiting on your Mac — 3 jobs ahead.",
    );
    expect(jobStatusLine({ ...base, status: "queued", queueDepth: 1 })).toBe(
      "Waiting on your Mac — 1 job ahead.",
    );
    expect(jobStatusLine({ ...base, status: "queued", queueDepth: 0 })).toBe("Next in line on your Mac.");
    expect(jobStatusLine({ ...base, status: "queued" })).toMatch(/Waiting/);
  });

  it("never shows a percentage it does not have", () => {
    expect(jobStatusLine({ ...base, status: "running" })).toBe("Working on your Mac…");
    expect(jobStatusLine({ ...base, status: "running", progress: 0.42 })).toBe("Working on your Mac — 42%.");
  });

  it("has a line for every terminal state", () => {
    expect(jobStatusLine({ ...base, status: "completed" })).toBe("Done.");
    expect(jobStatusLine({ ...base, status: "cancelled" })).toBe("Cancelled.");
    expect(jobStatusLine({ ...base, status: "failed" })).toBe("Failed.");
  });
});
