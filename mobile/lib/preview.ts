/**
 * Getting a playable preview URL, without ever hanging a request thread on the
 * Mac.
 *
 * THE TRAP THIS FILE EXISTS TO AVOID. `GET /api/sessions/{sid}/preview.mp4`
 * (`main.py:995`) renders SYNCHRONOUSLY when the file for the requested hash
 * is missing. Point a video player straight at it on a fresh session and the
 * Mac spends thirty seconds or more inside a request worker while AVFoundation
 * — which has its own patience — gives up and reports a black player. There
 * are two job workers in total (`api/jobs.py`) and they are shared with
 * whoever is sitting at the Mac, so the honest thing is to ASK for the render
 * as a job, watch it, and only then hand the player a URL that will answer
 * immediately.
 *
 * So the rule for the editor screen is absolute: never set a video source from
 * a hash that has not been rendered. `ensurePreview()` is the only way to get
 * one, and it returns the URL the Mac itself named rather than a URL this app
 * assembled — `main.py::_preview_payload` includes the `?h=` of the render
 * that actually happened, and asking for a different hash is a 404 by design
 * (serving the wrong bytes for a range request is what used to make playback
 * stall and reset).
 */

import type { ApiClient } from "./api";
import { ApiError } from "./errors";
import { jobStatusLine, runJob, type JobProgress } from "./jobs";
import type { Job } from "./types";

/**
 * Give up watching after this much OBSERVED time (time suspended in the user's
 * pocket does not count — see `jobs.ts`). A preview is a proxy render at a
 * reduced height; eight minutes is far beyond any real one, and stopping at
 * all matters because the alternative is a spinner with no end.
 */
export const PREVIEW_MAX_MS = 8 * 60_000;

/** What the player needs, and what the screen shows about it. */
export interface PreviewSource {
  /** Absolute URL, already carrying the media token where one was issued. */
  url: string;
  /** The same thing as the Mac named it — a relative API path, no token. Kept
   *  because `client.probeMedia()` signs a path itself and would otherwise be
   *  handed a URL with a token already in it. */
  path: string;
  /** The hash the Mac actually rendered. Compare against the EDL's to know
   *  whether the picture on screen is the picture in the project. */
  edlHash: string;
  /** True when the Mac had this render on disk already. */
  cached: boolean;
}

/**
 * The four calls this module makes, named as an interface so the whole render
 * lifecycle can be tested without a network. `previewBackend()` binds the real
 * client to it.
 */
export interface PreviewBackend {
  startPreview: (sid: string) => Promise<{ job_id: string }>;
  getJob: (jobId: string) => Promise<Job>;
  queueDepthFor: (sid: string, jobId: string) => Promise<number | null>;
  mediaUrl: (path: string) => Promise<string>;
}

export function previewBackend(client: ApiClient): PreviewBackend {
  return {
    startPreview: (sid) =>
      client.request<{ job_id: string }>("POST", `/api/sessions/${sid}/preview?wait=0`),
    getJob: (jobId) => client.getJob(jobId),
    queueDepthFor: (sid, jobId) => client.queueDepthFor(sid, jobId),
    mediaUrl: (path) => client.mediaUrl(path),
  };
}

export interface EnsurePreviewOptions {
  /** One sentence about where the render is, for the player's overlay. */
  onStatus?: (line: string, progress: number | null) => void;
  /** Set `aborted` to stop watching (the screen unmounted, the EDL moved on). */
  signal?: { aborted: boolean };
}

/** `main.py::_preview_payload`, validated rather than trusted. */
function readPayload(result: Record<string, unknown> | null): PreviewSource {
  const url = result?.["url"];
  const hash = result?.["edl_hash"];
  if (typeof url !== "string" || typeof hash !== "string") {
    // A render that "succeeded" without naming its output is not something to
    // paper over with a guessed URL: the player would 404 and the user would
    // be told the render failed, which is a different and wrong story.
    throw new ApiError({
      kind: "server",
      message: "Your Mac finished the preview but did not say where it is. Try again.",
    });
  }
  return { url, path: url, edlHash: hash, cached: result?.["cached"] === true };
}

/**
 * Render (or reuse) the preview for the session's CURRENT EDL and return a URL
 * a player can be handed.
 *
 * Throws an `ApiError` for every failure, so the caller has one branch and the
 * connection reducer sees transport failures unchanged.
 */
export async function ensurePreview(
  backend: PreviewBackend,
  sid: string,
  opts: EnsurePreviewOptions = {},
): Promise<PreviewSource> {
  opts.onStatus?.("Asking your Mac to render a preview…", null);

  const { job_id } = await backend.startPreview(sid);

  const outcome = await runJob(job_id, {
    getJob: backend.getJob,
    getQueueDepth: (id) => backend.queueDepthFor(sid, id),
    // render_preview takes no `set_progress`, so its job sits at 0 for the
    // whole run. An indeterminate bar is the truth; a stuck 0% reads as a hang.
    reportsProgress: false,
    maxMs: PREVIEW_MAX_MS,
    signal: opts.signal,
    onProgress: (p: JobProgress) => opts.onStatus?.(jobStatusLine(p), p.progress),
  });

  switch (outcome.kind) {
    case "completed": {
      const payload = readPayload(outcome.result);
      opts.onStatus?.(payload.cached ? "Ready." : "Rendered.", 1);
      // The Mac's own URL, with the media token appended. Native players do
      // not reliably carry an Authorization header across a range request,
      // which is exactly what an mp4 scrub is made of.
      return { ...payload, url: await backend.mediaUrl(payload.url) };
    }
    case "failed":
      throw new ApiError({ kind: "rejected", message: outcome.message });
    case "cancelled":
      throw new ApiError({ kind: "cancelled", message: "Preview render cancelled." });
    case "vanished":
      throw new ApiError({
        kind: "not_found",
        message: "Your Mac has no record of that preview render any more. Pull to refresh.",
      });
    case "timeout":
      throw new ApiError({
        kind: "timeout",
        message: "Your Mac is still rendering this preview. Leave it a moment and try again.",
      });
  }
}

/**
 * Whether the picture on screen still matches the project.
 *
 * The Mac and the phone share one store and one undo stack, so an edit made at
 * the Mac changes the EDL under a preview this phone rendered minutes ago. The
 * editor watches this rather than assuming its own dispatches are the only
 * source of change.
 */
export function isPreviewStale(source: PreviewSource | null, edlHash: string | null | undefined): boolean {
  if (!source || !edlHash) return false;
  return source.edlHash !== edlHash;
}
