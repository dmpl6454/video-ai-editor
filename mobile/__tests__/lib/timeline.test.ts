/**
 * Filmstrip geometry.
 *
 * Two of these assertions are load-bearing for the Mac's health rather than
 * for the look of the app: the absolute cap on tiles, and the quantisation of
 * thumbnail times. `api/hardening.py`'s rate limiter keys on the path WITHOUT
 * its query, so every `/thumb` request in the app shares one 60-per-second
 * bucket — an uncapped strip does not render slowly, it 429s and renders
 * nothing at all. And an unquantised time is a guaranteed cache miss on both
 * ends, which turns a pinch-zoom into a few hundred ffmpeg invocations.
 */

import { MAX_THUMBS_IN_FLIGHT } from "../../lib/net";
import {
  clampPixelsPerSecond,
  clampTime,
  clipAt,
  DEFAULT_PIXELS_PER_SECOND,
  frameStep,
  IDEAL_TILE_PX,
  MAX_PIXELS_PER_SECOND,
  MIN_PIXELS_PER_SECOND,
  stripClipsOf,
  THUMB_HEIGHT_PX,
  THUMB_STEPS,
  thumbKey,
  thumbPath,
  thumbStepSeconds,
  tickStepSeconds,
  tickTimes,
  timeAtX,
  visibleThumbTimes,
  xForTime,
  type StripClip,
} from "../../lib/timeline";
import type { Clip, EDL, Track } from "../../lib/types";

function strip(over: Partial<StripClip> = {}): StripClip {
  return { id: "c1", src: "/w/s_a/uploads/a.mp4", in: 0, out: 10, start: 0, duration: 10, speed: 1, ...over };
}

describe("clampPixelsPerSecond", () => {
  test("clamps to [8, 220]", () => {
    expect(MIN_PIXELS_PER_SECOND).toBe(8);
    expect(MAX_PIXELS_PER_SECOND).toBe(220);
    expect(clampPixelsPerSecond(0)).toBe(8);
    expect(clampPixelsPerSecond(-100)).toBe(8);
    expect(clampPixelsPerSecond(1e6)).toBe(220);
    expect(clampPixelsPerSecond(56)).toBe(56);
  });

  test("a non-finite zoom falls back to the default instead of producing NaN", () => {
    // A NaN pixels-per-second silently makes every x coordinate NaN, and the
    // strip renders as an empty box with no error anywhere.
    expect(clampPixelsPerSecond(Number.NaN)).toBe(DEFAULT_PIXELS_PER_SECOND);
    expect(clampPixelsPerSecond(Number.POSITIVE_INFINITY)).toBe(DEFAULT_PIXELS_PER_SECOND);
  });
});

describe("timeAtX / xForTime", () => {
  test("round-trip within 1e-6 across the whole zoom range", () => {
    for (const pps of [MIN_PIXELS_PER_SECOND, 13, 56, 137, MAX_PIXELS_PER_SECOND]) {
      for (const t of [0, 0.041666, 1, 63.5, 3599.75]) {
        expect(timeAtX(xForTime(t, pps), pps)).toBeCloseTo(t, 6);
      }
    }
  });

  test("both clamp the zoom identically, so they cannot disagree", () => {
    expect(xForTime(2, 1e9)).toBe(xForTime(2, MAX_PIXELS_PER_SECOND));
    expect(timeAtX(440, 1e9)).toBe(timeAtX(440, MAX_PIXELS_PER_SECOND));
  });
});

describe("thumbStepSeconds", () => {
  test("never finer than 0.5 s, at any zoom", () => {
    for (let pps = 1; pps <= 400; pps += 1) {
      expect(thumbStepSeconds(pps)).toBeGreaterThanOrEqual(0.5);
    }
  });

  test("only ever returns a value from the quantised ladder", () => {
    for (let pps = 1; pps <= 400; pps += 1) {
      expect(THUMB_STEPS).toContain(thumbStepSeconds(pps));
    }
  });

  test("one step always covers at least a tile's width where the ladder allows", () => {
    for (const pps of [8, 20, 56, 120, 220]) {
      const step = thumbStepSeconds(pps);
      expect(step * clampPixelsPerSecond(pps)).toBeGreaterThanOrEqual(IDEAL_TILE_PX);
    }
  });

  test("gets coarser as you zoom out and finer as you zoom in", () => {
    expect(thumbStepSeconds(MAX_PIXELS_PER_SECOND)).toBe(0.5);
    expect(thumbStepSeconds(MIN_PIXELS_PER_SECOND)).toBeGreaterThan(thumbStepSeconds(MAX_PIXELS_PER_SECOND));
  });
});

describe("visibleThumbTimes", () => {
  test("returns times that are EXACT multiples of the step", () => {
    const clips = [strip({ in: 1.3, out: 9.7, start: 0, duration: 8.4 })];
    for (const pps of [8, 24, 56, 130, 220]) {
      const step = thumbStepSeconds(pps);
      for (const tile of visibleThumbTimes({ clips, windowStart: 0, windowEnd: 9, pixelsPerSecond: pps })) {
        expect(Math.abs(tile.t / step - Math.round(tile.t / step))).toBeLessThan(1e-9);
      }
    }
  });

  test("caps at 24 tiles for a three-hour timeline fully on screen at max zoom", () => {
    // The pathological input: nothing about the window bounds this, only the
    // hard cap does. Ungated this is ~21,600 requests into a 60/s bucket.
    const threeHours = 3 * 60 * 60;
    const clips = [strip({ in: 0, out: threeHours, start: 0, duration: threeHours })];
    const tiles = visibleThumbTimes({
      clips,
      windowStart: 0,
      windowEnd: threeHours,
      pixelsPerSecond: MAX_PIXELS_PER_SECOND,
    });
    expect(tiles.length).toBeLessThanOrEqual(MAX_THUMBS_IN_FLIGHT);
    expect(MAX_THUMBS_IN_FLIGHT).toBe(24);
  });

  test("over the cap it strides rather than truncating, so the whole strip is sampled", () => {
    const threeHours = 3 * 60 * 60;
    const clips = [strip({ in: 0, out: threeHours, start: 0, duration: threeHours })];
    const tiles = visibleThumbTimes({
      clips,
      windowStart: 0,
      windowEnd: threeHours,
      pixelsPerSecond: MAX_PIXELS_PER_SECOND,
    });
    const last = tiles[tiles.length - 1];
    expect(tiles.length).toBeGreaterThan(1);
    // Truncation would leave the last tile a second or two in; striding puts
    // it near the far end of the timeline.
    expect(last?.t ?? 0).toBeGreaterThan(threeHours / 2);
  });

  test("a src in failedSrcs produces no tiles at all", () => {
    const clips = [
      strip({ id: "ok", src: "/w/s_a/uploads/ok.mp4", start: 0, duration: 10, in: 0, out: 10 }),
      strip({ id: "bad", src: "/elsewhere/outside.mp4", start: 10, duration: 10, in: 0, out: 10 }),
    ];
    const tiles = visibleThumbTimes({
      clips,
      windowStart: 0,
      windowEnd: 20,
      pixelsPerSecond: 56,
      failedSrcs: new Set(["/elsewhere/outside.mp4"]),
    });
    expect(tiles.some((t) => t.src === "/elsewhere/outside.mp4")).toBe(false);
    expect(tiles.some((t) => t.src === "/w/s_a/uploads/ok.mp4")).toBe(true);
  });

  test("maps timeline position to SOURCE time through the clip's in-point and speed", () => {
    // A clip trimmed to start at source second 20 and played at 2x: one second
    // into the clip on the timeline is source second 22.
    const clips = [strip({ in: 20, out: 40, start: 100, duration: 10, speed: 2 })];
    const tiles = visibleThumbTimes({ clips, windowStart: 100, windowEnd: 110, pixelsPerSecond: 220 });
    expect(tiles.length).toBeGreaterThan(0);
    for (const tile of tiles) {
      expect(tile.t).toBeGreaterThanOrEqual(20);
      expect(tile.t).toBeLessThan(40);
      // …and the tile is drawn where that source frame actually plays.
      const timelineSecond = 100 + (tile.t - 20) / 2;
      expect(tile.x).toBeCloseTo(xForTime(timelineSecond, 220), 6);
    }
  });

  test("a tile never runs past the end of its own clip", () => {
    const clips = [strip({ in: 0, out: 3, start: 0, duration: 3 })];
    const pps = 220;
    for (const tile of visibleThumbTimes({ clips, windowStart: 0, windowEnd: 3, pixelsPerSecond: pps })) {
      expect(tile.x + tile.width).toBeLessThanOrEqual(xForTime(3, pps) + 1e-6);
    }
  });

  test("clips outside the window contribute nothing", () => {
    const clips = [strip({ start: 500, duration: 10, in: 0, out: 10 })];
    expect(visibleThumbTimes({ clips, windowStart: 0, windowEnd: 20, pixelsPerSecond: 56 })).toEqual([]);
  });

  test("a short clip that straddles no grid point draws as a block, not an off-grid frame", () => {
    // Source seconds 0.6-0.8 contain no multiple of the 0.5 s step. Emitting a
    // bespoke time for it would cost a cache miss at every zoom level, for a
    // clip a few pixels wide.
    const clips = [strip({ in: 0.6, out: 0.8, start: 0, duration: 0.2 })];
    expect(visibleThumbTimes({ clips, windowStart: 0, windowEnd: 1, pixelsPerSecond: MAX_PIXELS_PER_SECOND })).toEqual(
      [],
    );
  });

  test("…but a short clip that DOES contain a grid point still gets its one frame", () => {
    const clips = [strip({ in: 0, out: 0.2, start: 0, duration: 0.2 })];
    const tiles = visibleThumbTimes({
      clips,
      windowStart: 0,
      windowEnd: 1,
      pixelsPerSecond: MAX_PIXELS_PER_SECOND,
    });
    expect(tiles.map((t) => t.t)).toEqual([0]);
    // …and the tile is clipped to the clip, not to a full step's width.
    expect(tiles[0]?.width).toBeCloseTo(0.2 * MAX_PIXELS_PER_SECOND, 6);
  });

  test("two clips cut from the same file share cache keys for the same frame", () => {
    const clips = [
      strip({ id: "a", start: 0, duration: 4, in: 0, out: 4 }),
      strip({ id: "b", start: 4, duration: 4, in: 0, out: 4 }),
    ];
    const tiles = visibleThumbTimes({ clips, windowStart: 0, windowEnd: 8, pixelsPerSecond: 220 });
    const aKeys = tiles.filter((t) => t.clipId === "a").map((t) => t.key);
    const bKeys = tiles.filter((t) => t.clipId === "b").map((t) => t.key);
    expect(aKeys.length).toBeGreaterThan(0);
    expect(bKeys).toEqual(aKeys);
  });

  test("an empty clip list is empty output, not a crash", () => {
    expect(visibleThumbTimes({ clips: [], windowStart: 0, windowEnd: 10, pixelsPerSecond: 56 })).toEqual([]);
  });

  test("maxTiles is honoured when a caller asks for a smaller budget", () => {
    const clips = [strip({ in: 0, out: 600, start: 0, duration: 600 })];
    const tiles = visibleThumbTimes({
      clips,
      windowStart: 0,
      windowEnd: 600,
      pixelsPerSecond: 220,
      maxTiles: 5,
    });
    expect(tiles.length).toBeLessThanOrEqual(5);
  });
});

describe("clipAt", () => {
  const clips = [
    strip({ id: "a", start: 0, duration: 4 }),
    strip({ id: "b", start: 4, duration: 4 }),
    strip({ id: "c", start: 8, duration: 4 }),
  ];

  test("a playhead exactly on a cut belongs to the RIGHT-hand clip", () => {
    // Half-open [start, start+duration). This has to agree with `split_at`,
    // or "this is the clip" and "split here" name different clips.
    expect(clipAt(clips, 4)?.id).toBe("b");
    expect(clipAt(clips, 8)?.id).toBe("c");
  });

  test("inside a clip, and at its very start", () => {
    expect(clipAt(clips, 0)?.id).toBe("a");
    expect(clipAt(clips, 3.999)?.id).toBe("a");
    expect(clipAt(clips, 5.5)?.id).toBe("b");
  });

  test("past the end, and before the beginning, is nothing", () => {
    expect(clipAt(clips, 12)).toBeNull();
    expect(clipAt(clips, -1)).toBeNull();
  });

  test("overlapping clips resolve to the later one", () => {
    const overlapping = [strip({ id: "under", start: 0, duration: 6 }), strip({ id: "over", start: 4, duration: 6 })];
    expect(clipAt(overlapping, 5)?.id).toBe("over");
  });
});

describe("stripClipsOf", () => {
  function edlWithClips(clips: Clip[]): EDL {
    const v1: Track = { id: "v1", type: "video", z: 0, clips };
    return { version: 1, duration: 0, canvas: { w: 1920, h: 1080, fps: 30, bg: "#000" }, tracks: [v1] };
  }

  test("flattens speed into both the duration and the speed field", () => {
    const [clip] = stripClipsOf(edlWithClips([{ id: "x", src: "/a.mp4", in: 0, out: 8, start: 0, speed: 2 }]));
    expect(clip?.duration).toBe(4);
    expect(clip?.speed).toBe(2);
  });

  test("a speed CURVE flattens to 1x rather than NaN", () => {
    const [clip] = stripClipsOf(
      edlWithClips([{ id: "x", src: "/a.mp4", in: 0, out: 8, start: 0, speed: { "0": 1 } as never }]),
    );
    expect(clip?.speed).toBe(1);
    expect(clip?.duration).toBe(8);
  });

  test("a null EDL is an empty strip", () => {
    expect(stripClipsOf(null)).toEqual([]);
  });
});

describe("ruler", () => {
  test("a tick label always has room — the step grows as the zoom shrinks", () => {
    expect(tickStepSeconds(MIN_PIXELS_PER_SECOND)).toBeGreaterThan(tickStepSeconds(MAX_PIXELS_PER_SECOND));
    for (const pps of [8, 30, 90, 220]) {
      expect(tickStepSeconds(pps) * clampPixelsPerSecond(pps)).toBeGreaterThanOrEqual(68);
    }
  });

  test("tickTimes are multiples of the step and stay inside the window", () => {
    const times = tickTimes(3.2, 20, 5);
    expect(times).toEqual([5, 10, 15, 20]);
  });

  test("tickTimes refuses an inverted window and caps a huge one", () => {
    expect(tickTimes(20, 3, 5)).toEqual([]);
    expect(tickTimes(0, 1e9, 0.5).length).toBeLessThanOrEqual(60);
  });
});

describe("frameStep and clampTime", () => {
  test("frameStep is 1/fps, and falls back to 30fps for nonsense", () => {
    expect(frameStep(30)).toBeCloseTo(1 / 30, 12);
    expect(frameStep(0)).toBeCloseTo(1 / 30, 12);
    expect(frameStep(undefined)).toBeCloseTo(1 / 30, 12);
  });

  test("clampTime keeps the playhead on the timeline", () => {
    expect(clampTime(-5, 10)).toBe(0);
    expect(clampTime(50, 10)).toBe(10);
    expect(clampTime(Number.NaN, 10)).toBe(0);
    expect(clampTime(3, -1)).toBe(0);
  });
});

describe("thumb URLs", () => {
  test("the cache key is shared by src and time, and ignores the height", () => {
    expect(thumbKey("/a.mp4", 1.5)).toBe(thumbKey("/a.mp4", 1.5));
    expect(thumbKey("/a.mp4", 1.5)).not.toBe(thumbKey("/b.mp4", 1.5));
  });

  test("the path encodes the source and asks for a height inside the server's clamp", () => {
    const path = thumbPath("s_abc", "/w/s abc/uploads/a b.mp4", 2.5);
    expect(path).toBe(`/api/sessions/s_abc/thumb?src=%2Fw%2Fs%20abc%2Fuploads%2Fa%20b.mp4&t=2.5&h=${THUMB_HEIGHT_PX}`);
    // main.py::get_thumb clamps h to [16, 270]; asking outside that range gets
    // silently changed, which would break the cache key on the Mac's side.
    expect(THUMB_HEIGHT_PX).toBeGreaterThanOrEqual(16);
    expect(THUMB_HEIGHT_PX).toBeLessThanOrEqual(270);
  });
});
