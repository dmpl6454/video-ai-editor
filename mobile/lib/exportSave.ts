/**
 * Getting a finished render off the Mac and onto the phone.
 *
 * The Mac renders. This module only moves bytes and hands them to iOS, and
 * every decision in it exists because of something that goes wrong when it is
 * done the obvious way:
 *
 * 1. THE FILE LANDS IN THE CACHE DIRECTORY, NEVER `Paths.document`. An export
 *    is a 200 MB derived artefact; the user's real copy is the one they save
 *    to Photos or send through the share sheet. `Paths.document` is backed up
 *    to iCloud and is never reclaimed by iOS, so writing exports there would
 *    quietly grow the app's footprint forever and put the user's video in
 *    their backup twice. `Paths.cache` is the directory iOS is allowed to
 *    empty under storage pressure, which is exactly the right contract for a
 *    file that can always be re-downloaded from the Mac.
 *
 * 2. THE DESTINATION IS BUILT WITH `new File(dir, name)`, NEVER BY JOINING
 *    STRINGS. `Paths.cache` is a `Directory` object in expo-file-system 57,
 *    not a string — `Paths.cache + '/' + name` produces "[object Object]/x.mp4"
 *    and the download fails on device while a test that only checked the
 *    string would pass. The filename itself is validated before it is used at
 *    all: it arrives from the Mac, and a name containing a separator or `..`
 *    would escape the directory this module is allowed to write to.
 *
 * 3. `totalBytes` IS `-1` WHEN THE MAC SENDS NO `Content-Length`, and -1 as a
 *    denominator produces a progress bar that runs backwards. It is mapped to
 *    `null` here so the caller renders an indeterminate bar — "working, amount
 *    unknown" — instead of a confident lie.
 *
 * 4. A PAUSED DOWNLOAD RESOLVES TO `null`, IT DOES NOT REJECT. `downloadAsync`
 *    returns `File | null`, and a `null` that is treated as success gives the
 *    UI a "saved" state pointing at a file that does not exist. It is turned
 *    into a `cancelled` ApiError here so it travels the same path as a tap on
 *    Cancel.
 *
 * 5. SAVING TO PHOTOS ASKS FOR WRITE-ONLY PERMISSION. `requestPermissionsAsync()`
 *    with no argument asks for full read AND write access to the entire photo
 *    library — under a plist string that promises this app never reads it.
 *    That is dishonest, and it is a plausible App Review rejection. The
 *    `true` in `requestPermissionsAsync(true)` is the whole point and
 *    `__tests__/lib/exportSave.test.ts` asserts it explicitly.
 */

import { Directory, File, Paths } from "expo-file-system";
import * as MediaLibrary from "expo-media-library";

import { ApiError } from "./errors";
import { humanBytes } from "./format";

// ---------------------------------------------------------------------------
// Export options
// ---------------------------------------------------------------------------

export type ExportContainer = "mp4" | "mov";

/**
 * `null` height means "whatever the canvas is". It has to travel as an ABSENT
 * key, not as `0`: `ExportRequest.height` on the Mac is `int | None`, and a 0
 * would be taken as a real request for a zero-pixel-tall render.
 */
export interface ExportOptions {
  height: number | null;
  fps: number | null;
  crf: number;
  container: ExportContainer;
}

/** Matches the desktop's export popover, plus 2160 for a 4K canvas. */
export const HEIGHT_CHOICES: readonly (number | null)[] = [null, 2160, 1440, 1080, 720, 480];

/**
 * Three named qualities rather than a raw CRF slider. CRF is a
 * rate-control parameter, not a user-facing concept, and the desktop's own
 * default is 18 — which is what "High" is here, so an export started on the
 * phone and one started at the Mac produce the same file.
 */
export const QUALITY_CHOICES = [
  { id: "high", label: "High", crf: 18, note: "The Mac app's default. Biggest file." },
  { id: "balanced", label: "Balanced", crf: 22, note: "About half the size. Hard to tell apart." },
  { id: "small", label: "Small", crf: 26, note: "For sending over a message." },
] as const;

export type QualityId = (typeof QUALITY_CHOICES)[number]["id"];

export const DEFAULT_EXPORT_OPTIONS: ExportOptions = {
  height: null,
  fps: null,
  crf: 18,
  container: "mp4",
};

/**
 * The POST body for `/api/sessions/{sid}/export`. Nulls are DROPPED rather
 * than sent — see the note on `ExportOptions.height`.
 */
export function exportRequestBody(o: ExportOptions): Record<string, unknown> {
  const body: Record<string, unknown> = { crf: o.crf, container: o.container };
  if (o.height !== null) body.height = o.height;
  if (o.fps !== null) body.fps = o.fps;
  return body;
}

/** "1080p MP4" / "Source resolution MOV" — for a summary line and a filename. */
export function describeOptions(o: ExportOptions): string {
  const res = o.height === null ? "Source resolution" : `${o.height}p`;
  const fps = o.fps === null ? "" : ` at ${o.fps} fps`;
  return `${res} ${o.container.toUpperCase()}${fps}`;
}

// ---------------------------------------------------------------------------
// Disk space
// ---------------------------------------------------------------------------

/**
 * Refuse to start a download with less than this free. An export that fills
 * the last of a phone's storage does not just fail — it takes Photos, Messages
 * and the camera down with it until the user deletes something, and the app
 * that caused it is the one they will blame.
 */
export const MIN_FREE_BYTES = 250 * 1024 * 1024;

/** Kept clear on top of the file itself, once its real size is known. */
export const SPACE_HEADROOM_BYTES = 64 * 1024 * 1024;

/** Injectable so the guard can be tested without a device. */
export type FreeBytes = () => number;

const defaultFreeBytes: FreeBytes = () => Paths.availableDiskSpace;

/** Free space, or null when iOS declines to say (which is not an error). */
export function freeSpace(read: FreeBytes = defaultFreeBytes): number | null {
  const n = read();
  return Number.isFinite(n) && n >= 0 ? n : null;
}

// ---------------------------------------------------------------------------
// Filenames
// ---------------------------------------------------------------------------

/**
 * The name the Mac gave the file, if it is safe to write.
 *
 * Deliberately strict: any separator, any `..` anywhere, any leading dot, any
 * control character, anything empty. The Mac's own names are
 * `export_<hash>.mp4`, so nothing legitimate is refused — and the alternative
 * to strictness is a rule with an exception in it, which is how directory
 * traversal gets shipped. Returns null rather than throwing so the caller
 * decides how loud to be.
 */
export function safeFilename(raw: string): string | null {
  const name = raw.trim();
  if (!name) return null;
  if (name.length > 200) return null;
  if (name.includes("/") || name.includes("\\")) return null;
  if (name.includes("..")) return null;
  if (name.startsWith(".")) return null;
  // Control characters (a NUL or a newline inside a path) are exactly what
  // this is refusing; they cannot appear in a legitimate export name.
  if (/[\u0000-\u001f\u007f]/.test(name)) return null;
  return name;
}

// ---------------------------------------------------------------------------
// Download
// ---------------------------------------------------------------------------

export interface DownloadReport {
  bytesWritten: number;
  /** null when the Mac sent no Content-Length. See note 3 in the header. */
  totalBytes: number | null;
  /** 0..1, or null — hand this straight to `<Progress fraction>`. */
  fraction: number | null;
}

export interface DownloadArgs {
  /** Absolute, already carrying the `?k=` media token where we have one. */
  url: string;
  /** As reported by the Mac. Validated by `safeFilename` before it is used. */
  filename: string;
  /** `client.mediaHeaders()` — the bearer, for loaders that keep headers. */
  headers?: Record<string, string>;
  onProgress?: (r: DownloadReport) => void;
  signal?: AbortSignal;
  freeBytes?: FreeBytes;
}

export interface DownloadedExport {
  /** `file://…` — what the share sheet and MediaLibrary are handed. */
  uri: string;
  filename: string;
  /** Bytes on disk, or null if the file reports nothing readable. */
  size: number | null;
}

/** Progress numbers, with `-1` mapped to "unknown". Exported for the test. */
export function toReport(bytesWritten: number, totalBytes: number): DownloadReport {
  const total = Number.isFinite(totalBytes) && totalBytes > 0 ? totalBytes : null;
  return {
    bytesWritten,
    totalBytes: total,
    fraction: total === null ? null : Math.min(1, Math.max(0, bytesWritten / total)),
  };
}

/** The cache subdirectory exports live in, created if it is not there yet. */
function exportsDirectory(): Directory {
  const dir = new Directory(Paths.cache, "exports");
  if (!dir.exists) dir.create({ intermediates: true, idempotent: true });
  return dir;
}

/**
 * Pull one finished export down to the phone's cache.
 *
 * Throws an `ApiError` for everything: a refused filename and a cancelled
 * transfer arrive at the caller the same way a 404 does, so the Export screen
 * has one error path rather than three.
 */
export async function downloadExport(args: DownloadArgs): Promise<DownloadedExport> {
  const name = safeFilename(args.filename);
  if (name === null) {
    throw new ApiError({
      kind: "rejected",
      message: `Your Mac named that export “${args.filename}”, which this app will not write to disk. Re-export it.`,
    });
  }

  const free = freeSpace(args.freeBytes ?? defaultFreeBytes);
  if (free !== null && free < MIN_FREE_BYTES) {
    throw new ApiError({
      kind: "rejected",
      message: `This iPhone has ${humanBytes(free)} free, which is not enough room for a video export. Free up some space and try again — the render is finished and waiting on your Mac.`,
    });
  }

  const dest = new File(exportsDirectory(), name);
  // A previous attempt at the same render leaves a file behind, and
  // `createDownloadTask` has no idempotent flag — it would fail on a
  // destination that already exists.
  if (dest.exists) dest.delete();

  // The abort controller is ours, so an out-of-space abort mid-transfer and a
  // tap on Cancel both stop the same way. The caller's signal is chained onto
  // it rather than passed through.
  const controller = new AbortController();
  const onAbort = () => controller.abort();
  args.signal?.addEventListener("abort", onAbort);
  let outOfSpace: number | null = null;

  try {
    const task = File.createDownloadTask(args.url, dest, {
      headers: args.headers,
      // The transfer keeps running while the phone is in a pocket. The JS task
      // does not survive a full app relaunch — Apple's session does, ours does
      // not — which is why the Export screen also re-polls the job on
      // foreground rather than trusting this alone.
      sessionType: "background",
      signal: controller.signal,
      onProgress: ({ bytesWritten, totalBytes }) => {
        const report = toReport(bytesWritten, totalBytes);
        // The size is only knowable once the response headers land. Checking it
        // here, rather than guessing before the request, is the difference
        // between refusing a download that would have fit and letting one fill
        // the disk.
        if (outOfSpace === null && report.totalBytes !== null && free !== null) {
          if (report.totalBytes + SPACE_HEADROOM_BYTES > free) {
            outOfSpace = report.totalBytes;
            controller.abort();
            return;
          }
        }
        args.onProgress?.(report);
      },
    });

    const file = await task.downloadAsync();
    if (file === null) {
      // Paused, not finished. See note 4 in the header.
      throw new ApiError({ kind: "cancelled", message: "The download stopped before it finished." });
    }
    return { uri: file.uri, filename: name, size: readSize(file) };
  } catch (e) {
    if (outOfSpace !== null) {
      throw new ApiError({
        kind: "rejected",
        message: `That export is ${humanBytes(outOfSpace)} and this iPhone has ${humanBytes(free)} free. It is still on your Mac — free up some space, or export at a smaller size.`,
      });
    }
    if (e instanceof ApiError) throw e;
    if (isAbort(e)) {
      throw new ApiError({ kind: "cancelled", message: "The download was cancelled." });
    }
    throw new ApiError({
      kind: "network",
      message: "The export could not be copied from your Mac. It is still there — try again.",
      cause: e,
    });
  } finally {
    args.signal?.removeEventListener("abort", onAbort);
  }
}

function isAbort(e: unknown): boolean {
  return e instanceof Error && (e.name === "AbortError" || /abort|cancel/i.test(e.message));
}

/** `File.size` is 0 for a file that cannot be read; 0 is not a useful size. */
function readSize(file: File): number | null {
  const n = file.size;
  return Number.isFinite(n) && n > 0 ? n : null;
}

/** Remove a downloaded copy. Used when the user has saved it elsewhere and
 *  there is no reason to keep a second copy on a phone. */
export function discardDownload(uri: string): void {
  const f = new File(uri);
  if (f.exists) f.delete();
}

// ---------------------------------------------------------------------------
// Photos
// ---------------------------------------------------------------------------

/**
 * Put a downloaded export in the user's photo library.
 *
 * WRITE-ONLY. See note 5 in the header — this is the one call in the app where
 * an extra `true` is the difference between the permission the plist string
 * promises and the permission the app actually asks for.
 */
export async function saveToPhotos(uri: string): Promise<void> {
  const permission = await MediaLibrary.requestPermissionsAsync(true);
  if (!permission.granted) {
    throw new ApiError({
      kind: "forbidden",
      message: permission.canAskAgain
        ? "Saving to Photos needs permission to add videos. Tap Save again and choose Allow."
        : "Photos access is turned off for this app. Turn on “Add Photos Only” in iPhone Settings › Video AI Editor, then try again.",
    });
  }

  try {
    await MediaLibrary.Asset.create(uri);
  } catch (e) {
    throw new ApiError({
      kind: "rejected",
      message: "iPhone would not add that video to Photos. The file is still saved in this app — you can send it with the share sheet instead.",
      cause: e,
    });
  }
}
