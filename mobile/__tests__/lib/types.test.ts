/**
 * The two narrowing predicates. They are the whole reason `AnyClip` is safe to
 * pass around, and getting either backwards would put a text clip through the
 * media-duration arithmetic (which reads `out`, a field it does not have).
 */

import { isMediaClip, isTextClip, type AnyClip, type Clip, type TextClip } from "../../lib/types";

const media: Clip = { id: "c1", src: "/a.mov", in: 0, out: 4, start: 0 };
const text: TextClip = { id: "t1", text: "Hello", start: 1, end: 3 };

describe("isMediaClip", () => {
  it("accepts a clip with a source and an out point", () => {
    expect(isMediaClip(media)).toBe(true);
  });

  it("rejects a text clip, which has neither", () => {
    // A TextClip carries `end`, not `out`; treating it as media is how a title
    // ends up drawn at NaN seconds.
    expect(isMediaClip(text)).toBe(false);
  });
});

describe("isTextClip", () => {
  it("accepts a text clip and rejects a media clip", () => {
    expect(isTextClip(text)).toBe(true);
    expect(isTextClip(media)).toBe(false);
  });
});

describe("the two predicates partition AnyClip", () => {
  it("classifies every clip as exactly one kind", () => {
    const clips: AnyClip[] = [media, text, { ...media, speed: 2 }, { ...text, role: "hook" }];
    for (const c of clips) {
      expect(isMediaClip(c)).not.toBe(isTextClip(c));
    }
  });
});

describe("speed is not simply a number", () => {
  it("types a speed-ramp curve, so arithmetic has to guard before dividing", () => {
    // The backend stores a ramp as a dict. `lib/edl.ts` reads it through a
    // guard for this reason; the type records the hazard.
    const ramped: Clip = { ...media, speed: { curve: "ease", from: 1, to: 2 } };
    expect(isMediaClip(ramped)).toBe(true);
    expect(typeof ramped.speed).toBe("object");
  });
});
