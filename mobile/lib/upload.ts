/**
 * Sending a clip from the phone's library to the Mac.
 *
 * This is the one place in the app where the phone hands the Mac something
 * rather than asking it for something, and it is the only operation whose
 * failure costs the user real time — a five-minute upload that dies at 97 %
 * because the Mac's disk was full is the worst thing this screen can do. So:
 *
 *   - the size is CHECKED against the Mac's own ceiling before a byte moves
 *     (`/api/health` and `whoami` both report `max_upload_bytes`), and the
 *     refusal names the number rather than saying "too large";
 *   - progress is real, from the native upload task, not a fake animation;
 *   - the time remaining is MEASURED and is `null` until there is enough
 *     evidence to measure it. An estimate invented from a guessed Wi-Fi speed
 *     is a lie that gets found out at minute three.
 *
 * WHAT THE PICKER ACTUALLY GIVES US, as opposed to what it looks like it
 * gives us. `asset.fileSize` is frequently undefined; `asset.duration` is in
 * MILLISECONDS while every field on the Mac is in seconds; and iOS hands over
 * its own copy of the clip, which for a HEVC or slow-motion original is often
 * re-encoded and a different size from what the Photos app shows. Every one of
 * those has a named function here so no screen has to remember.
 *
 * The transport is injected rather than imported. `expo-file-system`'s
 * `createUploadTask` is the only API that reports real progress, and it is
 * native — keeping it behind `StartUploadTask` means the request contract
 * below (the field name, the two form flags, the two 422 shapes) is testable
 * without a device, which is the only kind of testing available here.
 */

import { apiErrorFromResponse, apiErrorFromThrow, ApiError } from "./errors";
import { humanBytes, humanDuration, millisToSeconds } from "./format";

/** `main.py::upload` reads the file from a multipart field with this name. */
export const UPLOAD_FIELD_NAME = "file";

/** How long to watch before believing the throughput number. */
const MEASURE_AFTER_MS = 1_500;
/** …and how much of the file. Both, because a 4 GB file's first 3 % is still
 *  100 MB, while a 20 MB file's first 1.5 s may be all of it. */
const MEASURE_AFTER_FRACTION = 0.03;

// ---------------------------------------------------------------------------
// What the picker handed us
// ---------------------------------------------------------------------------

/** The subset of `ImagePickerAsset` this module reasons about. */
export interface PickedAsset {
  uri: string;
  fileName?: string | null;
  fileSize?: number;
  /** MILLISECONDS, per expo-image-picker. Never seconds. */
  duration?: number | null;
  mimeType?: string;
  width?: number;
  height?: number;
  type?: string | null;
}

export interface ClipToUpload {
  uri: string;
  /** A filename the Mac can keep. `main.py::_safe_filename` sanitises it. */
  name: string;
  /** Bytes, or null when neither the picker nor the filesystem could say. */
  sizeBytes: number | null;
  /** SECONDS. Converted from the picker's milliseconds. */
  durationSeconds: number | null;
  mimeType: string;
}

/** Extensions the Mac's ingest normalises without complaint. */
const DEFAULT_MIME = "video/mp4";

/**
 * Normalise one picked asset. `measuredSize` comes from the filesystem —
 * `new File(asset.uri).size` — because `asset.fileSize` is so often missing
 * that a progress bar built on it alone is usually indeterminate.
 */
export function describePickedAsset(asset: PickedAsset, measuredSize?: number | null): ClipToUpload {
  const measured = typeof measuredSize === "number" && measuredSize > 0 ? measuredSize : null;
  const reported = typeof asset.fileSize === "number" && asset.fileSize > 0 ? asset.fileSize : null;
  return {
    uri: asset.uri,
    name: safeAssetName(asset),
    // Prefer what we measured: it describes the copy iOS actually produced,
    // which is the thing being uploaded. The picker's number describes the
    // library original, and for a transcoded pick the two differ.
    sizeBytes: measured ?? reported,
    durationSeconds: millisToSeconds(asset.duration),
    mimeType: asset.mimeType && asset.mimeType.includes("/") ? asset.mimeType : DEFAULT_MIME,
  };
}

/** A filename the user will recognise in the media bin, with no path in it. */
export function safeAssetName(asset: PickedAsset): string {
  const raw = (asset.fileName ?? "").trim();
  const fromUri = decodeURIComponent(asset.uri.split("?")[0] ?? "").split("/").pop() ?? "";
  const candidate = raw || fromUri;
  // Strip anything that could read as a path. The Mac sanitises too, but a
  // name shown in this app's own list should already be the real one.
  const cleaned = candidate.replace(/[\\/]+/g, "").trim();
  if (!cleaned) return "clip.mp4";
  return cleaned.includes(".") ? cleaned : `${cleaned}.mp4`;
}

/**
 * Why this clip cannot be sent, in a sentence, or null if it can.
 *
 * Returning the sentence rather than a boolean is deliberate: there are three
 * different reasons and each one needs different words, and a screen that has
 * to build them itself will eventually build only two.
 */
export function uploadRefusal(clip: ClipToUpload, maxUploadBytes: number | null): string | null {
  if (clip.sizeBytes !== null && clip.sizeBytes === 0) {
    return "That clip is empty — iPhone gave us a file with no data in it. Try picking it again.";
  }
  if (maxUploadBytes !== null && maxUploadBytes > 0 && clip.sizeBytes !== null && clip.sizeBytes > maxUploadBytes) {
    return `That clip is ${humanBytes(clip.sizeBytes)}. Your Mac is set to accept uploads up to ${humanBytes(
      maxUploadBytes,
    )}, so it would be refused partway through. Trim it first, or raise the limit on the Mac.`;
  }
  return null;
}

// ---------------------------------------------------------------------------
// Progress and honest timing
// ---------------------------------------------------------------------------

export interface UploadTiming {
  /** 0..1, or null when the total is unknown and the bar must be vague. */
  fraction: number | null;
  /** Measured, never assumed. Null until there is enough evidence. */
  bytesPerSecond: number | null;
  secondsRemaining: number | null;
}

/**
 * Turn bytes-so-far into a fraction and — only once it can be justified — a
 * time remaining.
 *
 * `totalBytes` of 0 or less means the native task could not determine a total;
 * `Progress` then goes indeterminate rather than showing a confident 0 %.
 */
export function uploadTiming(bytesSent: number, totalBytes: number, elapsedMs: number): UploadTiming {
  const total = Number.isFinite(totalBytes) && totalBytes > 0 ? totalBytes : null;
  const sent = Number.isFinite(bytesSent) && bytesSent > 0 ? bytesSent : 0;
  const fraction = total === null ? null : Math.min(1, sent / total);

  const measurable =
    elapsedMs >= MEASURE_AFTER_MS && sent > 0 && (total === null || sent / total >= MEASURE_AFTER_FRACTION);
  if (!measurable) return { fraction, bytesPerSecond: null, secondsRemaining: null };

  const bytesPerSecond = (sent / elapsedMs) * 1000;
  if (total === null || !(bytesPerSecond > 0)) return { fraction, bytesPerSecond, secondsRemaining: null };
  return { fraction, bytesPerSecond, secondsRemaining: Math.max(0, (total - sent) / bytesPerSecond) };
}

/** The line under the progress bar. Never claims to know what it does not. */
export function uploadTimingLine(t: UploadTiming): string {
  if (t.bytesPerSecond === null) return "Measuring your Wi-Fi…";
  const rate = `${humanBytes(t.bytesPerSecond)}/s`;
  if (t.secondsRemaining === null) return `Sending at ${rate}.`;
  if (t.secondsRemaining < 3) return `Almost there — ${rate}.`;
  return `About ${humanDuration(t.secondsRemaining)} left at ${rate}.`;
}

// ---------------------------------------------------------------------------
// The request
// ---------------------------------------------------------------------------

/** What `main.py::upload` returns on success. */
export interface UploadResponse {
  src: string;
  normalized: string;
  duration: number;
  edl_hash: string;
  transcript_pending: boolean;
}

/** The shape `expo-file-system`'s upload task resolves to. */
export interface NativeUploadResult {
  status: number;
  body: string;
}

export interface UploadTaskSpec {
  url: string;
  headers: Record<string, string>;
  fieldName: string;
  mimeType: string;
  /** Multipart form fields alongside the file. All values are strings. */
  parameters: Record<string, string>;
  onProgress: (p: { bytesSent: number; totalBytes: number }) => void;
}

/** Injected transport. The screen supplies the `expo-file-system` task. */
export type StartUploadTask = (spec: UploadTaskSpec) => Promise<NativeUploadResult>;

export interface UploadOptions {
  sid: string;
  clip: ClipToUpload;
  /** `main.py` appends to v1 when true. False imports to the bin only. */
  addToTimeline: boolean;
  /** Whisper runs on the Mac AFTER the response returns (a background task). */
  transcribe: boolean;
  authHeaders: Record<string, string>;
  onProgress?: (t: UploadTiming) => void;
  /** Injected for tests and for a real elapsed clock. */
  now?: () => number;
}

export function uploadUrl(baseUrl: string, sid: string): string {
  return `${baseUrl.replace(/\/+$/, "")}/api/sessions/${sid}/upload`;
}

/**
 * Send one clip and return what the Mac made of it.
 *
 * The native task resolves for NON-2xx responses too — it only rejects when
 * the file cannot be read or the request itself fails — so the status has to
 * be checked by hand here. Routing the body through `apiErrorFromResponse`
 * means the Mac's own sentence ("This file has no video track — it's audio
 * only…") reaches the user instead of a generic failure, which is the entire
 * reason `main.py` writes those two 422 bodies so carefully.
 */
export async function uploadClip(
  start: StartUploadTask,
  baseUrl: string,
  opts: UploadOptions,
): Promise<UploadResponse> {
  const now = opts.now ?? Date.now;
  const startedAt = now();

  const spec: UploadTaskSpec = {
    url: uploadUrl(baseUrl, opts.sid),
    headers: opts.authHeaders,
    fieldName: UPLOAD_FIELD_NAME,
    mimeType: opts.clip.mimeType,
    parameters: {
      add_to_timeline: opts.addToTimeline ? "true" : "false",
      transcribe: opts.transcribe ? "true" : "false",
      // `whisper_model: str = Form("")` — empty means "use the Mac's default".
      // Choosing a model from the phone would be picking a trade-off on
      // hardware this app cannot see.
      whisper_model: "",
    },
    onProgress: ({ bytesSent, totalBytes }) => {
      opts.onProgress?.(uploadTiming(bytesSent, totalBytes, now() - startedAt));
    },
  };

  let result: NativeUploadResult;
  try {
    result = await start(spec);
  } catch (e) {
    // The native task rejects for an unreadable file, a dead network, and a
    // user abort. `apiErrorFromThrow` sorts the abort into `cancelled`, which
    // the screen shows neutrally rather than as a failure the user caused.
    throw apiErrorFromThrow(e);
  }

  if (result.status < 200 || result.status >= 300) {
    throw apiErrorFromResponse(result.status, result.body);
  }

  return parseUploadResponse(result.body);
}

/** Parse the success body, refusing to invent the fields it must contain. */
export function parseUploadResponse(body: string): UploadResponse {
  let parsed: unknown;
  try {
    parsed = JSON.parse(body);
  } catch {
    throw new ApiError({
      kind: "server",
      message: "Your Mac accepted the clip but sent back something this app could not read.",
    });
  }
  const o = parsed as Partial<UploadResponse> | null;
  if (!o || typeof o.normalized !== "string" || typeof o.edl_hash !== "string") {
    throw new ApiError({
      kind: "server",
      message: "Your Mac accepted the clip but did not say where it put it. Open the project to check.",
    });
  }
  return {
    src: typeof o.src === "string" ? o.src : o.normalized,
    normalized: o.normalized,
    duration: typeof o.duration === "number" ? o.duration : 0,
    edl_hash: o.edl_hash,
    transcript_pending: o.transcript_pending === true,
  };
}
