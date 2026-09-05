/**
 * The download-and-save path, tested against fakes for the two native modules
 * it uses.
 *
 * WHY THIS FILE IS SHAPED THE WAY IT IS. There is no simulator on the machine
 * this app is built from, so every one of these assertions is standing in for
 * a device run that cannot happen. That makes the *identity* of what gets
 * passed to expo-file-system the thing worth asserting — not just that a
 * download happened, but that the destination was built from `Paths.cache`
 * with the `File` constructor, that the bearer actually rode along in
 * `headers`, and that the Photos permission request carried its `true`. An
 * earlier revision of the spec asserted an API (`createDownloadResumable`,
 * `cacheDirectory`) that does not exist in expo-file-system 57 — a green test
 * over a crash on first tap. These fakes mirror the real 57 surface exactly:
 * `Paths.cache` is an object, `File` takes `(Directory, name)`, `totalBytes`
 * is `-1` when the server sent no Content-Length, and a paused download
 * RESOLVES with `null` rather than rejecting.
 */

interface FakeProgress {
  bytesWritten: number;
  totalBytes: number;
}

const mockFileArgs: unknown[][] = [];
const mockDirArgs: unknown[][] = [];
const mockDeleted: string[] = [];
const mockCreatedDirs: string[] = [];
const mockExistingFiles = new Map<string, number>();
const mockExistingDirs = new Set<string>();

const mockDisk = { available: 64 * 1024 * 1024 * 1024 };

const mockDownload = {
  calls: [] as { url: string; destination: { uri: string }; options: Record<string, unknown> }[],
  progress: [] as FakeProgress[],
  outcome: "file" as "file" | "paused" | "error",
};

/**
 * What `DownloadTask.downloadAsync()` really does, kept OUT of the jest.mock
 * factory: babel-plugin-jest-hoist refuses any identifier inside a factory that
 * is not on its allowlist, and a TypeScript parameter annotation counts. The
 * factory below therefore calls this by its `mock`-prefixed name and contains
 * no types of its own.
 */
async function mockPerformDownload(
  destination: { uri: string },
  options: Record<string, unknown>,
): Promise<{ uri: string } | null> {
  const onProgress = options.onProgress as ((step: FakeProgress) => void) | undefined;
  const signal = options.signal as AbortSignal | undefined;
  for (const step of mockDownload.progress) {
    onProgress?.(step);
    if (signal?.aborted) {
      // What the real task does: an aborted transfer REJECTS.
      const err = new Error("Aborted");
      err.name = "AbortError";
      throw err;
    }
  }
  if (mockDownload.outcome === "paused") return null;
  if (mockDownload.outcome === "error") throw new Error("connection lost");
  mockExistingFiles.set(destination.uri, 4_242_424);
  return destination;
}

/** Same reason as above: `Directory`/`File` URIs are joined out here. */
function mockJoinUris(parts: unknown[]): string {
  return parts
    .map((p) => (typeof p === "string" ? p : ((p as { uri?: string })?.uri ?? String(p))))
    .join("/");
}

jest.mock("expo-file-system", () => {
  class FakeDirectory {
    uri = "";
    constructor(...parts: unknown[]) {
      mockDirArgs.push(parts);
      this.uri = mockJoinUris(parts);
    }
    get exists() {
      return mockExistingDirs.has(this.uri);
    }
    create() {
      mockExistingDirs.add(this.uri);
      mockCreatedDirs.push(this.uri);
    }
  }

  class FakeFile {
    uri = "";
    constructor(...parts: unknown[]) {
      mockFileArgs.push(parts);
      this.uri = mockJoinUris(parts);
    }
    get exists() {
      return mockExistingFiles.has(this.uri);
    }
    get size() {
      return mockExistingFiles.get(this.uri) ?? 0;
    }
    delete() {
      mockDeleted.push(this.uri);
      mockExistingFiles.delete(this.uri);
    }
    static createDownloadTask(
      url: string,
      destination: { uri: string },
      options: Record<string, unknown>,
    ) {
      mockDownload.calls.push({ url, destination, options });
      return { downloadAsync: () => mockPerformDownload(destination, options) };
    }
  }

  return {
    File: FakeFile,
    Directory: FakeDirectory,
    Paths: {
      cache: { uri: "file:///cache" },
      document: { uri: "file:///docs" },
      get availableDiskSpace() {
        return mockDisk.available;
      },
    },
  };
});

const mockPermissionCalls: unknown[][] = [];
const mockPermission = { granted: true, canAskAgain: true };
const mockAssetCreate = jest.fn(async (uri: string) => ({ id: `asset:${uri}` }));

jest.mock("expo-media-library", () => ({
  requestPermissionsAsync: jest.fn(async (...args: unknown[]) => {
    mockPermissionCalls.push(args);
    return { granted: mockPermission.granted, canAskAgain: mockPermission.canAskAgain };
  }),
  Asset: {
    create: (uri: string) => mockAssetCreate(uri),
  },
}));

import { ApiError } from "../../lib/errors";
import {
  DEFAULT_EXPORT_OPTIONS,
  describeOptions,
  discardDownload,
  downloadExport,
  exportRequestBody,
  freeSpace,
  HEIGHT_CHOICES,
  MIN_FREE_BYTES,
  QUALITY_CHOICES,
  safeFilename,
  saveToPhotos,
  SPACE_HEADROOM_BYTES,
  toReport,
} from "../../lib/exportSave";

const GB = 1024 * 1024 * 1024;

function reset() {
  mockFileArgs.length = 0;
  mockDirArgs.length = 0;
  mockDeleted.length = 0;
  mockCreatedDirs.length = 0;
  mockExistingFiles.clear();
  mockExistingDirs.clear();
  mockPermissionCalls.length = 0;
  mockDownload.calls.length = 0;
  mockDownload.progress = [{ bytesWritten: 10, totalBytes: 100 }];
  mockDownload.outcome = "file";
  mockDisk.available = 64 * GB;
  mockPermission.granted = true;
  mockPermission.canAskAgain = true;
  mockAssetCreate.mockClear();
}

beforeEach(reset);

const ARGS = {
  url: "http://10.0.0.4:8765/api/sessions/s1/files/exports/export_abc.mp4?k=tok",
  filename: "export_abc.mp4",
  headers: { authorization: "Bearer secret-token" },
};

// ---------------------------------------------------------------------------

describe("export options", () => {
  it("omits a null height and a null fps rather than sending zero", () => {
    // ExportRequest.height is `int | None` on the Mac. A 0 would be read as a
    // real request for a zero-pixel render, not as "use the canvas".
    const body = exportRequestBody(DEFAULT_EXPORT_OPTIONS);
    expect(body).toEqual({ crf: 18, container: "mp4" });
    expect("height" in body).toBe(false);
    expect("fps" in body).toBe(false);
  });

  it("sends a chosen height and fps", () => {
    expect(exportRequestBody({ height: 1080, fps: 30, crf: 22, container: "mov" })).toEqual({
      height: 1080,
      fps: 30,
      crf: 22,
      container: "mov",
    });
  });

  it("defaults to the same CRF the Mac app uses, so both ends agree", () => {
    expect(DEFAULT_EXPORT_OPTIONS.crf).toBe(18);
    expect(QUALITY_CHOICES.find((q) => q.id === "high")?.crf).toBe(18);
  });

  it("offers Source resolution first and every other choice descending", () => {
    expect(HEIGHT_CHOICES[0]).toBeNull();
    const rest = HEIGHT_CHOICES.slice(1) as number[];
    expect([...rest].sort((a, b) => b - a)).toEqual(rest);
  });

  it("describes what is about to be rendered in words", () => {
    expect(describeOptions(DEFAULT_EXPORT_OPTIONS)).toBe("Source resolution MP4");
    expect(describeOptions({ height: 1080, fps: 30, crf: 18, container: "mov" })).toBe(
      "1080p MOV at 30 fps",
    );
  });
});

describe("safeFilename", () => {
  it("accepts the names the Mac actually produces", () => {
    expect(safeFilename("export_9f3a21c.mp4")).toBe("export_9f3a21c.mp4");
    expect(safeFilename("  my cut v2.mov  ")).toBe("my cut v2.mov");
  });

  it("refuses anything with a path separator", () => {
    expect(safeFilename("a/b.mp4")).toBeNull();
    expect(safeFilename("a\\b.mp4")).toBeNull();
    expect(safeFilename("/etc/passwd")).toBeNull();
  });

  it("refuses anything containing ..", () => {
    expect(safeFilename("..")).toBeNull();
    expect(safeFilename("../../secrets.mp4")).toBeNull();
    expect(safeFilename("x..mp4")).toBeNull();
  });

  it("refuses empty, dotfile and control-character names", () => {
    expect(safeFilename("")).toBeNull();
    expect(safeFilename("   ")).toBeNull();
    expect(safeFilename(".hidden.mp4")).toBeNull();
    expect(safeFilename("clip\u0007.mp4")).toBeNull();
    expect(safeFilename("clip\n.mp4")).toBeNull();
  });
});

describe("toReport", () => {
  it("maps a totalBytes of -1 to null so the bar goes indeterminate", () => {
    // -1 is what the task reports when the Mac sent no Content-Length. Used as
    // a denominator it renders a bar that runs backwards.
    const r = toReport(2048, -1);
    expect(r.totalBytes).toBeNull();
    expect(r.fraction).toBeNull();
    expect(r.bytesWritten).toBe(2048);
  });

  it("clamps a fraction into 0..1", () => {
    expect(toReport(50, 100).fraction).toBeCloseTo(0.5);
    expect(toReport(500, 100).fraction).toBe(1);
    expect(toReport(0, 100).fraction).toBe(0);
  });
});

describe("freeSpace", () => {
  it("reads the number iOS reports", () => {
    expect(freeSpace(() => 123)).toBe(123);
  });

  it("returns null rather than a bogus number when iOS declines to say", () => {
    expect(freeSpace(() => Number.NaN)).toBeNull();
    expect(freeSpace(() => -1)).toBeNull();
  });
});

describe("downloadExport", () => {
  it("builds the destination from Paths.cache with the File constructor", async () => {
    await downloadExport(ARGS);

    // The directory is constructed FROM the Paths.cache object itself, by
    // identity — not from a string that happens to look like its uri.
    const { Paths } = jest.requireMock("expo-file-system") as {
      Paths: { cache: object; document: object };
    };
    const dirCall = mockDirArgs.find((a) => a[0] === Paths.cache);
    expect(dirCall).toBeDefined();
    expect(dirCall?.[1]).toBe("exports");

    // And no part of the path is ever the document directory: an export is a
    // derived artefact, and Paths.document is backed up to iCloud forever.
    expect(mockDirArgs.flat()).not.toContain(Paths.document);
    expect(mockFileArgs.flat()).not.toContain(Paths.document);

    // The file is `new File(directory, name)` — two arguments, the first an
    // object. String concatenation onto a Directory yields "[object Object]/…".
    const fileCall = mockFileArgs.find((a) => a[1] === "export_abc.mp4");
    expect(fileCall).toBeDefined();
    expect(typeof fileCall?.[0]).toBe("object");
  });

  it("passes the bearer through headers and asks for a background session", async () => {
    await downloadExport(ARGS);
    const call = mockDownload.calls[0];
    expect(call?.url).toBe(ARGS.url);
    expect(call?.options.headers).toEqual({ authorization: "Bearer secret-token" });
    // Without this the transfer dies the moment the phone is pocketed.
    expect(call?.options.sessionType).toBe("background");
  });

  it("creates the exports directory once and returns the written file", async () => {
    const out = await downloadExport(ARGS);
    expect(mockCreatedDirs).toEqual(["file:///cache/exports"]);
    expect(out.filename).toBe("export_abc.mp4");
    expect(out.uri).toBe("file:///cache/exports/export_abc.mp4");
    expect(out.size).toBe(4_242_424);
  });

  it("deletes a leftover file from a previous attempt before downloading", async () => {
    mockExistingDirs.add("file:///cache/exports");
    mockExistingFiles.set("file:///cache/exports/export_abc.mp4", 10);
    await downloadExport(ARGS);
    expect(mockDeleted).toContain("file:///cache/exports/export_abc.mp4");
  });

  it("reports progress with an unknown total as null", async () => {
    mockDownload.progress = [{ bytesWritten: 1, totalBytes: -1 }];
    const seen: (number | null)[] = [];
    await downloadExport({ ...ARGS, onProgress: (r) => seen.push(r.fraction) });
    expect(seen).toEqual([null]);
  });

  it("rejects a filename with a separator before touching the filesystem", async () => {
    await expect(downloadExport({ ...ARGS, filename: "../../../etc/passwd" })).rejects.toThrow(
      ApiError,
    );
    expect(mockDownload.calls).toHaveLength(0);
    expect(mockCreatedDirs).toHaveLength(0);
    expect(mockFileArgs).toHaveLength(0);
  });

  it("refuses to start when the phone is nearly full", async () => {
    mockDisk.available = MIN_FREE_BYTES - 1;
    const err = await downloadExport(ARGS).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).kind).toBe("rejected");
    // The copy has to say the render survived, or the user re-renders for
    // nothing.
    expect((err as ApiError).message).toContain("waiting on your Mac");
    expect(mockDownload.calls).toHaveLength(0);
  });

  it("aborts mid-transfer once the real size will not fit, and says the size", async () => {
    mockDisk.available = MIN_FREE_BYTES + SPACE_HEADROOM_BYTES;
    mockDownload.progress = [{ bytesWritten: 1, totalBytes: 8 * GB }];
    const err = await downloadExport(ARGS).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).kind).toBe("rejected");
    expect((err as ApiError).message).toContain("8.0 GB");
    expect((err as ApiError).message).toContain("still on your Mac");
  });

  it("treats a null from downloadAsync as a cancellation, not a success", async () => {
    // `downloadAsync` RESOLVES with null when the task is paused. Treating that
    // as success hands the UI a saved state pointing at a file that is not
    // there.
    mockDownload.outcome = "paused";
    const err = await downloadExport(ARGS).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).kind).toBe("cancelled");
  });

  it("reports a caller cancellation as cancelled", async () => {
    const controller = new AbortController();
    mockDownload.progress = [{ bytesWritten: 1, totalBytes: 100 }];
    const promise = downloadExport({
      ...ARGS,
      signal: controller.signal,
      onProgress: () => controller.abort(),
    });
    const err = await promise.catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).kind).toBe("cancelled");
  });

  it("reports a transport failure as a network error that names the Mac", async () => {
    mockDownload.outcome = "error";
    const err = await downloadExport(ARGS).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).kind).toBe("network");
    expect((err as ApiError).message).toContain("Mac");
  });
});

describe("discardDownload", () => {
  it("removes a file that exists and is silent about one that does not", () => {
    mockExistingFiles.set("file:///cache/exports/gone.mp4", 1);
    discardDownload("file:///cache/exports/gone.mp4");
    expect(mockDeleted).toEqual(["file:///cache/exports/gone.mp4"]);
    expect(() => discardDownload("file:///cache/exports/never.mp4")).not.toThrow();
  });
});

describe("saveToPhotos", () => {
  it("asks for WRITE-ONLY permission before saving", async () => {
    await saveToPhotos("file:///cache/exports/export_abc.mp4");
    // The bare call requests full read+write over the whole library, under a
    // plist string promising this app never reads it. The `true` is the point.
    expect(mockPermissionCalls).toEqual([[true]]);
    expect(mockAssetCreate).toHaveBeenCalledWith("file:///cache/exports/export_abc.mp4");
  });

  it("does not save when permission is refused", async () => {
    mockPermission.granted = false;
    const err = await saveToPhotos("file:///cache/exports/x.mp4").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).kind).toBe("forbidden");
    expect(mockAssetCreate).not.toHaveBeenCalled();
  });

  it("tells the user where the switch is when it cannot ask again", async () => {
    mockPermission.granted = false;
    mockPermission.canAskAgain = false;
    const err = (await saveToPhotos("file:///x.mp4").catch((e: unknown) => e)) as ApiError;
    expect(err.message).toContain("iPhone Settings");
    expect(err.message).toContain("Add Photos Only");
  });

  it("turns a refusal from Photos into a readable error, not a silent no-op", async () => {
    mockAssetCreate.mockRejectedValueOnce(new Error("PHPhotosErrorDomain 3300"));
    const err = (await saveToPhotos("file:///x.mp4").catch((e: unknown) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.message).not.toContain("PHPhotosErrorDomain");
    expect(err.message).toContain("share sheet");
  });
});
