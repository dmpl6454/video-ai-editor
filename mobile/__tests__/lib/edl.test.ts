/**
 * EDL arithmetic.
 *
 * The numbers here decide where clips are DRAWN. If they disagree with the
 * numbers the Mac used to place those clips, the timeline shows one cut and
 * the render produces another — and the disagreement is invisible until an
 * export comes out wrong. So each function is checked against the backend
 * source it ports, named in the test.
 */

import {
  allMediaClips,
  clipDuration,
  clipEnd,
  clipSpeedFactor,
  findClip,
  isEmptyTimeline,
  mediaClipsOf,
  posterSpec,
  timelineExtent,
  trackById,
  VIDEO_TRACK_ID,
  videoExtent,
} from "../../lib/edl";
import { isMediaClip, isTextClip, type AnyClip, type Clip, type EDL, type TextClip, type Track } from "../../lib/types";

function mediaClip(over: Partial<Clip> = {}): Clip {
  return { id: "c1", src: "/w/s_a/uploads/a.mp4", in: 0, out: 4, start: 0, ...over };
}

function textClip(over: Partial<TextClip> = {}): TextClip {
  return { id: "t1", text: "hello", start: 1, end: 3, ...over };
}

function track(id: string, type: string, clips: AnyClip[]): Track {
  return { id, type, z: 0, clips };
}

function edlWith(tracks: Track[], duration = 0): EDL {
  return {
    version: 1,
    duration,
    canvas: { w: 1920, h: 1080, fps: 30, bg: "#000000" },
    tracks,
  };
}

describe("clipSpeedFactor", () => {
  test("absent speed is 1x", () => {
    expect(clipSpeedFactor(mediaClip())).toBe(1);
  });

  test("a positive number is the factor", () => {
    expect(clipSpeedFactor(mediaClip({ speed: 2 }))).toBe(2);
  });

  test("null, zero, negative and NaN all fall back to 1x", () => {
    expect(clipSpeedFactor(mediaClip({ speed: null }))).toBe(1);
    expect(clipSpeedFactor(mediaClip({ speed: 0 }))).toBe(1);
    expect(clipSpeedFactor(mediaClip({ speed: -2 }))).toBe(1);
    expect(clipSpeedFactor(mediaClip({ speed: Number.NaN }))).toBe(1);
  });

  test("a speed CURVE dict is 1x, not NaN — mirrors backend speed_factor", () => {
    // A ramped clip carries a dict here. Dividing by it would produce NaN and
    // the clip would vanish from the strip entirely.
    expect(clipSpeedFactor(mediaClip({ speed: { "0": 1, "2": 2 } }))).toBe(1);
  });
});

describe("clipDuration", () => {
  test("a 1x media clip occupies out - in", () => {
    expect(clipDuration(mediaClip({ in: 1, out: 5 }))).toBe(4);
  });

  test("a 2x clip occupies HALF its source length (frontend/src/types.ts:101-113)", () => {
    // This is the bug the port exists to prevent: drawing 4s for a 2x clip
    // overlaps the neighbours the backend has already rippled left.
    expect(clipDuration(mediaClip({ in: 0, out: 4, speed: 2 }))).toBe(2);
  });

  test("a 0.5x clip occupies twice its source length", () => {
    expect(clipDuration(mediaClip({ in: 0, out: 4, speed: 0.5 }))).toBe(8);
  });

  test("a text clip is end - start and ignores speed entirely", () => {
    expect(clipDuration(textClip({ start: 2, end: 6.5 }))).toBe(4.5);
  });
});

describe("clipEnd", () => {
  test("media: start + effective duration, not start + (out - in)", () => {
    expect(clipEnd(mediaClip({ start: 10, in: 0, out: 4, speed: 2 }))).toBe(12);
  });

  test("text: the absolute `end`, which is already a timeline second", () => {
    expect(clipEnd(textClip({ start: 2, end: 6 }))).toBe(6);
  });
});

describe("videoExtent", () => {
  test("matches the backend's max(start + effective_duration) over v1", () => {
    // edl/schema.py::video_extent, the function main.py's upload path calls to
    // decide where the next clip is appended.
    const edl = edlWith([
      track(VIDEO_TRACK_ID, "video", [
        mediaClip({ id: "a", start: 0, in: 0, out: 4 }),
        mediaClip({ id: "b", start: 4, in: 0, out: 6, speed: 2 }),
      ]),
    ]);
    expect(videoExtent(edl)).toBe(7);
  });

  test("ignores every other track — a long music bed must not extend it", () => {
    // The concrete case from the backend docstring: a six-minute bed under a
    // 29-second cut. `duration` is 373; the video extent is 29.
    const edl = edlWith(
      [
        track(VIDEO_TRACK_ID, "video", [mediaClip({ start: 0, in: 0, out: 29 })]),
        track("a1", "music", [mediaClip({ id: "m", src: "/w/s_a/uploads/song.m4a", start: 0, in: 0, out: 373 })]),
      ],
      373,
    );
    expect(videoExtent(edl)).toBe(29);
  });

  test("text overlays on v1 do not count as video", () => {
    const edl = edlWith([track(VIDEO_TRACK_ID, "video", [textClip({ start: 0, end: 90 })])]);
    expect(videoExtent(edl)).toBe(0);
  });

  test("an empty or missing v1, and a null EDL, are all 0", () => {
    expect(videoExtent(edlWith([]))).toBe(0);
    expect(videoExtent(edlWith([track(VIDEO_TRACK_ID, "video", [])]))).toBe(0);
    expect(videoExtent(null)).toBe(0);
  });
});

describe("timelineExtent", () => {
  test("prefers the video extent when there is video", () => {
    const edl = edlWith([track(VIDEO_TRACK_ID, "video", [mediaClip({ start: 0, in: 0, out: 12 })])], 400);
    expect(timelineExtent(edl)).toBe(12);
  });

  test("falls back to the EDL duration for an audio-only project", () => {
    const edl = edlWith([track("a1", "music", [mediaClip({ start: 0, in: 0, out: 90 })])], 90);
    expect(timelineExtent(edl)).toBe(90);
  });

  test("a nonsense duration reads as zero rather than NaN", () => {
    const edl = edlWith([], Number.NaN);
    expect(timelineExtent(edl)).toBe(0);
  });
});

describe("clip narrowing", () => {
  test("a TextClip is not a media clip and carries no src", () => {
    const t = textClip();
    expect(isMediaClip(t)).toBe(false);
    expect(isTextClip(t)).toBe(true);
    expect(mediaClipsOf(track("v1", "video", [t]))).toEqual([]);
  });

  test("mediaClipsOf sorts by timeline start, not by array order", () => {
    const later = mediaClip({ id: "late", start: 10 });
    const earlier = mediaClip({ id: "early", start: 1 });
    expect(mediaClipsOf(track("v1", "video", [later, earlier])).map((c) => c.id)).toEqual(["early", "late"]);
  });

  test("allMediaClips reaches every lane", () => {
    const edl = edlWith([
      track("v1", "video", [mediaClip({ id: "v" })]),
      track("a1", "music", [mediaClip({ id: "a" })]),
      track("t1", "text", [textClip()]),
    ]);
    expect(allMediaClips(edl).map((c) => c.id).sort()).toEqual(["a", "v"]);
  });
});

describe("lookup helpers", () => {
  test("findClip returns the clip and the track it lives on", () => {
    const edl = edlWith([track("v1", "video", [mediaClip({ id: "x" })])]);
    expect(findClip(edl, "x")?.track.id).toBe("v1");
    expect(findClip(edl, "nope")).toBeNull();
    expect(findClip(null, "x")).toBeNull();
  });

  test("trackById tolerates a null EDL", () => {
    expect(trackById(null, "v1")).toBeNull();
  });

  test("isEmptyTimeline is true only when no track holds a clip", () => {
    expect(isEmptyTimeline(edlWith([track("v1", "video", [])]))).toBe(true);
    expect(isEmptyTimeline(edlWith([track("t1", "text", [textClip()])]))).toBe(false);
    expect(isEmptyTimeline(null)).toBe(true);
  });
});

describe("posterSpec", () => {
  test("samples a little way into the first v1 clip, in SOURCE seconds", () => {
    const edl = edlWith([track(VIDEO_TRACK_ID, "video", [mediaClip({ in: 10, out: 30, start: 0 })])]);
    // `t` is handed straight to ffmpeg against the clip's own file, so it must
    // be offset from `in`, not from the timeline start.
    expect(posterSpec(edl)).toEqual({ src: "/w/s_a/uploads/a.mp4", t: 10.6 });
  });

  test("a very short clip is sampled at its midpoint rather than past its end", () => {
    const edl = edlWith([track(VIDEO_TRACK_ID, "video", [mediaClip({ in: 0, out: 0.4 })])]);
    expect(posterSpec(edl)?.t).toBeCloseTo(0.2, 10);
  });

  test("falls back to any lane when v1 has no media", () => {
    const edl = edlWith([track("a1", "music", [mediaClip({ src: "/w/s_a/uploads/song.m4a", in: 0, out: 60 })])]);
    expect(posterSpec(edl)?.src).toBe("/w/s_a/uploads/song.m4a");
  });

  test("a project with nothing on it has no poster", () => {
    expect(posterSpec(edlWith([]))).toBeNull();
    expect(posterSpec(null)).toBeNull();
  });
});
