/**
 * The import contract.
 *
 * Two classes of assertion here. The first is about the REQUEST: `main.py`'s
 * upload route reads a multipart field literally called `file` and two form
 * flags, and gets any of them wrong by answering 422 in a way that reads to
 * the user as "your video is broken". The second is about HONESTY: the picker
 * lies about size, reports duration in the wrong unit, and hands over a copy
 * iOS may have re-encoded — and the screen's copy is only true if these
 * functions are.
 */

import { ApiError } from "../../lib/errors";
import {
  describePickedAsset,
  parseUploadResponse,
  safeAssetName,
  uploadClip,
  UPLOAD_FIELD_NAME,
  uploadRefusal,
  uploadTiming,
  uploadTimingLine,
  uploadUrl,
  type ClipToUpload,
  type NativeUploadResult,
  type UploadTaskSpec,
} from "../../lib/upload";

const OK_BODY = JSON.stringify({
  src: "/w/s_a/uploads/clip.mp4",
  normalized: "/w/s_a/uploads/clip/normalized.mp4",
  duration: 12.5,
  probe: {},
  edl_hash: "abc123",
  transcript_pending: true,
});

function clip(over: Partial<ClipToUpload> = {}): ClipToUpload {
  return {
    uri: "file:///var/mobile/tmp/clip.mov",
    name: "clip.mov",
    sizeBytes: 20_000_000,
    durationSeconds: 12.5,
    mimeType: "video/quicktime",
    ...over,
  };
}

describe("describePickedAsset", () => {
  test("converts the picker's MILLISECOND duration to seconds", () => {
    // Mixing these once put a 1000x duration on the timeline.
    expect(describePickedAsset({ uri: "file:///a.mov", duration: 12500 }).durationSeconds).toBe(12.5);
  });

  test("a missing duration is null, not zero", () => {
    expect(describePickedAsset({ uri: "file:///a.mov" }).durationSeconds).toBeNull();
    expect(describePickedAsset({ uri: "file:///a.mov", duration: null }).durationSeconds).toBeNull();
  });

  test("prefers the MEASURED size — that is the copy actually being uploaded", () => {
    // `asset.fileSize` describes the library original; for a transcoded pick
    // the file on disk is a different size, and it is the one going over Wi-Fi.
    const described = describePickedAsset({ uri: "file:///a.mov", fileSize: 999 }, 4242);
    expect(described.sizeBytes).toBe(4242);
  });

  test("falls back to the picker's size, then to null", () => {
    expect(describePickedAsset({ uri: "file:///a.mov", fileSize: 999 }).sizeBytes).toBe(999);
    expect(describePickedAsset({ uri: "file:///a.mov" }).sizeBytes).toBeNull();
    expect(describePickedAsset({ uri: "file:///a.mov" }, 0).sizeBytes).toBeNull();
  });

  test("keeps a sane mime type when the picker gives none or nonsense", () => {
    expect(describePickedAsset({ uri: "file:///a.mov" }).mimeType).toBe("video/mp4");
    expect(describePickedAsset({ uri: "file:///a.mov", mimeType: "video" }).mimeType).toBe("video/mp4");
    expect(describePickedAsset({ uri: "file:///a.mov", mimeType: "video/quicktime" }).mimeType).toBe(
      "video/quicktime",
    );
  });
});

describe("safeAssetName", () => {
  test("uses the picker's filename when there is one", () => {
    expect(safeAssetName({ uri: "file:///tmp/xyz.mov", fileName: "Sunset.MOV" })).toBe("Sunset.MOV");
  });

  test("falls back to the last path segment, url-decoded", () => {
    expect(safeAssetName({ uri: "file:///tmp/My%20Clip.mov" })).toBe("My Clip.mov");
  });

  test("drops the query string iOS sometimes appends", () => {
    expect(safeAssetName({ uri: "file:///tmp/a.mov?width=1920" })).toBe("a.mov");
  });

  test("never returns anything that reads as a path", () => {
    const name = safeAssetName({ uri: "file:///tmp/x.mov", fileName: "../../etc/passwd" });
    expect(name).not.toContain("/");
    expect(name).not.toContain("\\");
  });

  test("gives an extension-less name one, and never returns empty", () => {
    expect(safeAssetName({ uri: "file:///tmp/x.mov", fileName: "holiday" })).toBe("holiday.mp4");
    expect(safeAssetName({ uri: "" })).toBe("clip.mp4");
  });
});

describe("uploadRefusal", () => {
  test("refuses a clip over the Mac's ceiling, and says both numbers", () => {
    // Better than failing at 97% of a five-minute upload.
    // humanBytes is binary — 2e9 bytes is 1.9 GiB, not "2 GB". The sentence
    // therefore quotes what the phone will actually show, not the round
    // decimal number the Mac's env var was probably set to.
    const message = uploadRefusal(clip({ sizeBytes: 3_000_000_000 }), 2_000_000_000);
    expect(message).toContain("1.9 GB");
    expect(message).toContain("2.8 GB");
  });

  test("allows a clip inside the ceiling", () => {
    expect(uploadRefusal(clip({ sizeBytes: 20_000_000 }), 2_000_000_000)).toBeNull();
  });

  test("refuses an empty file", () => {
    expect(uploadRefusal(clip({ sizeBytes: 0 }), 2_000_000_000)).toContain("empty");
  });

  test("an unknown size or an unknown ceiling is not a refusal", () => {
    // We only refuse what we can prove will be rejected; the Mac's own 413 is
    // the backstop for everything else.
    expect(uploadRefusal(clip({ sizeBytes: null }), 10)).toBeNull();
    expect(uploadRefusal(clip({ sizeBytes: 5_000_000_000 }), null)).toBeNull();
    expect(uploadRefusal(clip({ sizeBytes: 5_000_000_000 }), 0)).toBeNull();
  });
});

describe("uploadTiming", () => {
  test("no total means an indeterminate bar, never a confident 0 %", () => {
    expect(uploadTiming(0, 0, 0).fraction).toBeNull();
    expect(uploadTiming(1024, -1, 5000).fraction).toBeNull();
  });

  test("refuses to estimate before there is enough evidence", () => {
    // A number invented from a guessed Wi-Fi speed gets found out at minute
    // three, which is worse than saying nothing.
    const early = uploadTiming(1_000_000, 100_000_000, 200);
    expect(early.fraction).toBeCloseTo(0.01, 6);
    expect(early.bytesPerSecond).toBeNull();
    expect(early.secondsRemaining).toBeNull();
  });

  test("measures once enough time and enough of the file have passed", () => {
    // 10 MB of 100 MB in 2 s → 5 MB/s → 18 s left.
    const t = uploadTiming(10_000_000, 100_000_000, 2_000);
    expect(t.bytesPerSecond).toBeCloseTo(5_000_000, 0);
    expect(t.secondsRemaining).toBeCloseTo(18, 3);
  });

  test("clamps the fraction at 1 — multipart framing can push bytesSent past the file size", () => {
    expect(uploadTiming(101, 100, 5_000).fraction).toBe(1);
  });

  test("a finished upload reports no time remaining", () => {
    expect(uploadTiming(100_000_000, 100_000_000, 10_000).secondsRemaining).toBe(0);
  });
});

describe("uploadTimingLine", () => {
  test("says it is measuring rather than inventing a number", () => {
    expect(uploadTimingLine({ fraction: 0.01, bytesPerSecond: null, secondsRemaining: null })).toBe(
      "Measuring your Wi-Fi…",
    );
  });

  test("states the measured rate, and the time when it knows one", () => {
    expect(uploadTimingLine({ fraction: 0.1, bytesPerSecond: 5_000_000, secondsRemaining: 90 })).toBe(
      "About 1 min 30 s left at 4.8 MB/s.",
    );
  });

  test("stops promising a time in the last seconds", () => {
    expect(uploadTimingLine({ fraction: 0.99, bytesPerSecond: 5_000_000, secondsRemaining: 1 })).toContain(
      "Almost there",
    );
  });

  test("a rate with no total still reads sensibly", () => {
    expect(uploadTimingLine({ fraction: null, bytesPerSecond: 1_048_576, secondsRemaining: null })).toBe(
      "Sending at 1.0 MB/s.",
    );
  });
});

describe("uploadUrl", () => {
  test("builds the session upload path and tolerates a trailing slash", () => {
    expect(uploadUrl("http://10.0.0.2:8765", "s_a")).toBe("http://10.0.0.2:8765/api/sessions/s_a/upload");
    expect(uploadUrl("http://10.0.0.2:8765/", "s_a")).toBe("http://10.0.0.2:8765/api/sessions/s_a/upload");
  });
});

describe("uploadClip", () => {
  function capture(result: NativeUploadResult) {
    const specs: UploadTaskSpec[] = [];
    const start = async (spec: UploadTaskSpec) => {
      specs.push(spec);
      return result;
    };
    return { specs, start };
  }

  test("sends the field name and form flags main.py actually reads", async () => {
    const { specs, start } = capture({ status: 200, body: OK_BODY });
    await uploadClip(start, "http://10.0.0.2:8765", {
      sid: "s_a",
      clip: clip(),
      addToTimeline: true,
      transcribe: false,
      authHeaders: { authorization: "Bearer t", "X-VAE-Client": "1" },
    });
    const spec = specs[0];
    expect(spec?.url).toBe("http://10.0.0.2:8765/api/sessions/s_a/upload");
    // `file: UploadFile = File(...)` — any other field name is a 422 that
    // reads to the user as "your video is broken".
    expect(spec?.fieldName).toBe("file");
    expect(UPLOAD_FIELD_NAME).toBe("file");
    expect(spec?.parameters).toEqual({
      add_to_timeline: "true",
      transcribe: "false",
      whisper_model: "",
    });
  });

  test("carries the bearer AND the client header that forces the preflight", async () => {
    const { specs, start } = capture({ status: 200, body: OK_BODY });
    await uploadClip(start, "http://10.0.0.2:8765", {
      sid: "s_a",
      clip: clip(),
      addToTimeline: false,
      transcribe: true,
      authHeaders: { authorization: "Bearer t", "X-VAE-Client": "1" },
    });
    expect(specs[0]?.headers).toEqual({ authorization: "Bearer t", "X-VAE-Client": "1" });
    expect(specs[0]?.parameters.add_to_timeline).toBe("false");
    expect(specs[0]?.parameters.transcribe).toBe("true");
  });

  test("reports progress with a measured elapsed time", async () => {
    let clock = 1_000;
    const timings: (number | null)[] = [];
    const start = async (spec: UploadTaskSpec) => {
      clock += 5_000;
      spec.onProgress({ bytesSent: 50_000_000, totalBytes: 100_000_000 });
      return { status: 200, body: OK_BODY };
    };
    await uploadClip(start, "http://x", {
      sid: "s_a",
      clip: clip(),
      addToTimeline: true,
      transcribe: true,
      authHeaders: {},
      now: () => clock,
      onProgress: (t) => timings.push(t.secondsRemaining),
    });
    expect(timings).toEqual([5]);
  });

  test("a non-2xx resolves rather than rejects natively, so the status is checked here", async () => {
    // The Mac's audio-only refusal, verbatim from main.py. Losing it would
    // send the user to re-export a file that is perfectly fine.
    const body = JSON.stringify({
      detail: {
        file: "song.mp4",
        error: "audio_only_file",
        message: "This file has no video track — it's audio only.",
      },
    });
    const { start } = capture({ status: 422, body });
    await expect(
      uploadClip(start, "http://x", {
        sid: "s_a",
        clip: clip(),
        addToTimeline: true,
        transcribe: true,
        authHeaders: {},
      }),
    ).rejects.toMatchObject({ kind: "rejected", message: "This file has no video track — it's audio only." });
  });

  test("a 413 from the upload cap becomes the too_large kind", async () => {
    const { start } = capture({ status: 413, body: '{"error":{"code":"TOO_LARGE","message":"Upload too large."}}' });
    await expect(
      uploadClip(start, "http://x", {
        sid: "s_a",
        clip: clip(),
        addToTimeline: true,
        transcribe: true,
        authHeaders: {},
      }),
    ).rejects.toMatchObject({ kind: "too_large" });
  });
});

describe("parseUploadResponse", () => {
  test("reads the fields the Mac sends", () => {
    expect(parseUploadResponse(OK_BODY)).toEqual({
      src: "/w/s_a/uploads/clip.mp4",
      normalized: "/w/s_a/uploads/clip/normalized.mp4",
      duration: 12.5,
      edl_hash: "abc123",
      transcript_pending: true,
    });
  });

  test("a body that is not JSON is an error, not a silent success", () => {
    expect(() => parseUploadResponse("<html>oops</html>")).toThrow(ApiError);
  });

  test("a body missing the fields the screen depends on is refused", () => {
    expect(() => parseUploadResponse('{"ok":true}')).toThrow(ApiError);
  });

  test("falls back to `normalized` when `src` is absent", () => {
    const parsed = parseUploadResponse('{"normalized":"/n.mp4","edl_hash":"h"}');
    expect(parsed.src).toBe("/n.mp4");
    expect(parsed.duration).toBe(0);
    expect(parsed.transcript_pending).toBe(false);
  });
});
