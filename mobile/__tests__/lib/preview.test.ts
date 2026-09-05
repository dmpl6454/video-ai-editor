/**
 * Getting a preview without stalling the Mac.
 *
 * The single most important assertion in this file is the first one: the
 * request goes to `POST …/preview?wait=0`. `GET …/preview.mp4` renders
 * SYNCHRONOUSLY when the file for the requested hash is missing, which parks
 * one of the Mac's request workers for the length of an ffmpeg run while
 * AVFoundation times out and shows a black rectangle. Every other test here
 * is about making sure the four ways that job can end produce four different,
 * true sentences instead of one generic failure.
 */

import type { ApiClient } from "../../lib/api";
import { ApiError } from "../../lib/errors";
import { ensurePreview, isPreviewStale, previewBackend, type PreviewBackend } from "../../lib/preview";
import type { Job } from "../../lib/types";

function job(over: Partial<Job> = {}): Job {
  return {
    id: "j1",
    kind: "preview",
    status: "completed",
    progress: 0,
    result: null,
    error: null,
    created_at: 0,
    started_at: 0,
    completed_at: 0,
    session_id: "s_a",
    ...over,
  };
}

const PAYLOAD = {
  path: "/w/s_a/previews/abc123.mp4",
  cached: false,
  edl_hash: "abc123",
  url: "/api/sessions/s_a/preview.mp4?h=abc123",
};

function backend(over: Partial<PreviewBackend> = {}): PreviewBackend {
  return {
    startPreview: async () => ({ job_id: "j1" }),
    getJob: async () => job({ result: PAYLOAD }),
    queueDepthFor: async () => null,
    mediaUrl: async (p) => `http://10.0.0.2:8765${p}&k=tok`,
    ...over,
  };
}

describe("previewBackend", () => {
  test("asks for the ASYNCHRONOUS render — wait=0, never the streaming GET", () => {
    const calls: [string, string][] = [];
    const fake = {
      request: async (method: string, path: string) => {
        calls.push([method, path]);
        return { job_id: "j1" };
      },
      getJob: async () => job(),
      queueDepthFor: async () => null,
      mediaUrl: async (p: string) => p,
    } as unknown as ApiClient;

    void previewBackend(fake).startPreview("s_a");
    expect(calls).toEqual([["POST", "/api/sessions/s_a/preview?wait=0"]]);
  });
});

describe("ensurePreview", () => {
  test("returns the Mac's own URL with the media token appended", async () => {
    const source = await ensurePreview(backend(), "s_a");
    expect(source.edlHash).toBe("abc123");
    expect(source.cached).toBe(false);
    // The URL is the one `_preview_payload` named, hash and all. Assembling
    // our own would ask for a hash the Mac may not have rendered — a 404 by
    // design, because serving different bytes for a range request is what
    // used to make playback stall and reset.
    expect(source.url).toBe("http://10.0.0.2:8765/api/sessions/s_a/preview.mp4?h=abc123&k=tok");
    // …and the untokenised path is kept, because anything that signs a path
    // itself (the media probe) must not be handed one that is already signed.
    expect(source.path).toBe("/api/sessions/s_a/preview.mp4?h=abc123");
  });

  test("reports a cached render as cached", async () => {
    const b = backend({ getJob: async () => job({ result: { ...PAYLOAD, cached: true } }) });
    expect((await ensurePreview(b, "s_a")).cached).toBe(true);
  });

  test("narrates the job while it runs", async () => {
    const lines: string[] = [];
    await ensurePreview(backend(), "s_a", { onStatus: (line) => lines.push(line) });
    expect(lines[0]).toBe("Asking your Mac to render a preview…");
    expect(lines[lines.length - 1]).toBe("Rendered.");
  });

  test("a failed render surfaces the Mac's own sentence, not a generic one", async () => {
    // main.py turns an ffmpeg failure into an actionable message; losing it
    // here would send the user hunting for a corrupt file that is fine.
    const message = "Couldn't render — a clip's source file (b.mp4) is missing.";
    const b = backend({ getJob: async () => job({ status: "failed", error: message }) });
    await expect(ensurePreview(b, "s_a")).rejects.toMatchObject({ kind: "rejected", message });
  });

  test("a job the Mac has pruned is 'lost track of', not 'failed'", async () => {
    const b = backend({
      getJob: async () => {
        throw new ApiError({ kind: "not_found", message: "job j1 not found" });
      },
    });
    await expect(ensurePreview(b, "s_a")).rejects.toMatchObject({ kind: "not_found" });
  });

  test("an aborted watch is a cancellation, and nothing is fetched", async () => {
    const signal = { aborted: true };
    let fetched = 0;
    const b = backend({
      getJob: async () => {
        fetched += 1;
        return job({ result: PAYLOAD });
      },
    });
    await expect(ensurePreview(b, "s_a", { signal })).rejects.toMatchObject({ kind: "cancelled" });
    expect(fetched).toBe(0);
  });

  test("a job cancelled on the Mac is a cancellation too", async () => {
    const b = backend({ getJob: async () => job({ status: "cancelled" }) });
    await expect(ensurePreview(b, "s_a")).rejects.toMatchObject({ kind: "cancelled" });
  });

  test("a 'completed' job with no url is refused rather than guessed at", async () => {
    // Guessing the URL would 404 and the user would be told the render failed
    // — a different, and wrong, story.
    const b = backend({ getJob: async () => job({ result: { path: "/x.mp4" } }) });
    await expect(ensurePreview(b, "s_a")).rejects.toMatchObject({ kind: "server" });
  });

  test("a transport failure propagates untouched, so the connection reducer sees it", async () => {
    const boom = new ApiError({ kind: "network", message: "Could not reach your Mac." });
    const b = backend({
      startPreview: async () => {
        throw boom;
      },
    });
    await expect(ensurePreview(b, "s_a")).rejects.toBe(boom);
  });
});

describe("isPreviewStale", () => {
  const source = { url: "u", path: "/p", edlHash: "abc123", cached: false };

  test("true once the project has moved on from the render on screen", () => {
    expect(isPreviewStale(source, "def456")).toBe(true);
  });

  test("false when they match", () => {
    expect(isPreviewStale(source, "abc123")).toBe(false);
  });

  test("nothing to compare is not stale — an unrendered project is not a stale one", () => {
    expect(isPreviewStale(null, "abc123")).toBe(false);
    expect(isPreviewStale(source, null)).toBe(false);
    expect(isPreviewStale(source, undefined)).toBe(false);
  });
});
