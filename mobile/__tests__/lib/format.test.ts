import {
  basename,
  dimensions,
  formatCount,
  humanBytes,
  humanDuration,
  millisToSeconds,
  percent,
  relativeTime,
  relativeTimeFromEpochSeconds,
  shortHash,
  spoken,
  timecode,
  timecodeFrames,
} from "../../lib/format";

const DASH = "—";

describe("timecode", () => {
  it("formats under a minute, under an hour, and beyond", () => {
    expect(timecode(0)).toBe("0:00.0");
    expect(timecode(12.44)).toBe("0:12.4");
    expect(timecode(62.5)).toBe("1:02.5");
    // .25 rounds up at one decimal place — stated rather than avoided, so
    // nobody later "fixes" the ruler to truncate and shifts every label.
    expect(timecode(3723.25)).toBe("1:02:03.3");
    expect(timecode(3723.2)).toBe("1:02:03.2");
  });

  it("zero-pads the seconds so the ruler does not jitter", () => {
    expect(timecode(65)).toBe("1:05.0");
  });

  it("handles a negative offset without producing nonsense", () => {
    expect(timecode(-2.5)).toBe("-0:02.5");
  });

  it("refuses what it cannot format", () => {
    expect(timecode(Number.NaN)).toBe(DASH);
    expect(timecode(Number.POSITIVE_INFINITY)).toBe(DASH);
  });
});

describe("timecodeFrames", () => {
  it("formats hours, minutes, seconds and frames", () => {
    expect(timecodeFrames(62.5, 30)).toBe("00:01:02:15");
    expect(timecodeFrames(0, 24)).toBe("00:00:00:00");
  });

  it("carries a rounded remainder into the next second", () => {
    // 0.9999 s at 30 fps rounds to frame 30, which is not a timecode that can
    // exist. It must become second 1, frame 0.
    expect(timecodeFrames(0.9999, 30)).toBe("00:00:01:00");
  });

  it("refuses without a usable fps rather than guessing one", () => {
    expect(timecodeFrames(10, 0)).toBe(DASH);
    expect(timecodeFrames(10, Number.NaN)).toBe(DASH);
  });
});

describe("humanDuration", () => {
  it("steps from seconds through minutes to hours", () => {
    expect(humanDuration(38)).toBe("38 s");
    expect(humanDuration(252)).toBe("4 min 12 s");
    expect(humanDuration(240)).toBe("4 min");
    expect(humanDuration(3600)).toBe("1 hr");
    expect(humanDuration(3900)).toBe("1 hr 5 min");
  });

  it("refuses a negative or unusable duration", () => {
    expect(humanDuration(-1)).toBe(DASH);
    expect(humanDuration(Number.NaN)).toBe(DASH);
  });
});

describe("millisToSeconds", () => {
  it("converts the photo picker's milliseconds", () => {
    // asset.duration is milliseconds while every EDL field is seconds; mixing
    // them once put a 1000x duration on the timeline.
    expect(millisToSeconds(12_400)).toBe(12.4);
    expect(millisToSeconds(0)).toBe(0);
  });

  it("returns null for the values the picker actually emits when unsure", () => {
    expect(millisToSeconds(undefined)).toBeNull();
    expect(millisToSeconds(null)).toBeNull();
    expect(millisToSeconds(-1)).toBeNull();
    expect(millisToSeconds(Number.NaN)).toBeNull();
  });
});

describe("humanBytes", () => {
  it("scales through the units", () => {
    expect(humanBytes(512)).toBe("512 B");
    expect(humanBytes(1024)).toBe("1.0 KB");
    expect(humanBytes(1024 * 1024 * 3.5)).toBe("3.5 MB");
    expect(humanBytes(1024 ** 3 * 2)).toBe("2.0 GB");
  });

  it("drops the decimal once the number is three digits wide", () => {
    expect(humanBytes(1024 * 200)).toBe("200 KB");
  });

  it("returns a dash for an unknown size, which the picker very often gives", () => {
    expect(humanBytes(undefined)).toBe(DASH);
    expect(humanBytes(null)).toBe(DASH);
    expect(humanBytes(-1)).toBe(DASH);
  });
});

describe("percent", () => {
  it("clamps into 0..100", () => {
    expect(percent(0.5)).toBe(50);
    expect(percent(-1)).toBe(0);
    expect(percent(4)).toBe(100);
  });

  it("returns null when there is no denominator, so the bar goes indeterminate", () => {
    expect(percent(null)).toBeNull();
    expect(percent(Number.NaN)).toBeNull();
  });
});

describe("relativeTime", () => {
  const NOW = 1_700_000_000_000;

  it("is coarse on purpose", () => {
    expect(relativeTime(NOW - 5_000, NOW)).toBe("just now");
    expect(relativeTime(NOW - 5 * 60_000, NOW)).toBe("5 min ago");
    expect(relativeTime(NOW - 3 * 3_600_000, NOW)).toBe("3 hr ago");
    expect(relativeTime(NOW - 2 * 86_400_000, NOW)).toBe("2 days ago");
    expect(relativeTime(NOW - 86_400_000, NOW)).toBe("1 day ago");
    expect(relativeTime(NOW - 60 * 86_400_000, NOW)).toBe("2 months ago");
  });

  it("never says 'in the future' when the Mac's clock is ahead of the phone's", () => {
    expect(relativeTime(NOW + 60_000, NOW)).toBe("just now");
  });

  it("returns a dash for nothing", () => {
    expect(relativeTime(null)).toBe(DASH);
    expect(relativeTime(undefined)).toBe(DASH);
  });
});

describe("relativeTimeFromEpochSeconds", () => {
  it("reads the seconds-based created_at that list_sessions sends", () => {
    const NOW = 1_700_000_000_000;
    expect(relativeTimeFromEpochSeconds(NOW / 1000 - 300, NOW)).toBe("5 min ago");
    expect(relativeTimeFromEpochSeconds(null)).toBe(DASH);
  });
});

describe("small formatters", () => {
  it("shortens a hash without pretending the digest means anything", () => {
    expect(shortHash("a1b2c3d4e5f6")).toBe("a1b2c3d");
    expect(shortHash(null)).toBe(DASH);
    expect(shortHash("")).toBe(DASH);
  });

  it("writes dimensions with a multiplication sign", () => {
    expect(dimensions(1920, 1080)).toBe("1920×1080");
    expect(dimensions(0, 1080)).toBe(DASH);
  });

  it("takes a basename with either separator", () => {
    expect(basename("/Users/sam/Movies/take 1.mov")).toBe("take 1.mov");
    expect(basename("C:\\clips\\take1.mp4")).toBe("take1.mp4");
    expect(basename("take1.mp4")).toBe("take1.mp4");
    // A trailing slash must not produce an empty label.
    expect(basename("/Users/sam/")).toBe("/Users/sam/");
  });

  it("counts", () => {
    expect(formatCount(0)).toBe("0");
    expect(formatCount(1234)).toBe(Number(1234).toLocaleString());
    expect(formatCount(Number.NaN)).toBe(DASH);
  });

  it("respells navigation paths for VoiceOver", () => {
    expect(spoken("Settings › Privacy › Local Network")).toBe("Settings, then Privacy, then Local Network");
    expect(spoken("Video AI Editor → Phone")).toBe("Video AI Editor, then Phone");
  });
});
