"""Golden cases for the project timebase (instant preview spec §6 R2).

``tests/goldens/timebase_cases.json`` records what ``edl/timebase.py`` returns
over a grid that includes exact frame multiples ± 1e-7/1e-9, exact half-frame
ties, large times and every standard rate plus off-standard and invalid
inputs. ``frontend/src/lib/preview/timeline/timebase.test.ts`` asserts the
TypeScript port reproduces 100 % of it. This test asserts the file still
matches the Python functions, so a change to timebase.py without regenerating
the goldens fails here (regenerate: ``VAI_REGEN_GOLDENS=1 pytest this file``).
"""
from __future__ import annotations

import json
import math
import os
import random
from fractions import Fraction
from pathlib import Path

from video_ai_editor.edl import timebase as tb
from video_ai_editor.render.frame_map import ffmpeg_us

GOLDEN = Path(__file__).resolve().parent / "goldens" / "timebase_cases.json"

FPS_INPUTS: list = (
    [float(r) for r in tb.STANDARD_RATES]
    + [24, 25, 30, 48, 50, 60, 29.97, 23.976, 59.94, 23.98, 29.970029, 30.0,
       12.5, 15, 90, 100, 120, 240, 29.5, 1000, 7.5, 0.5, 1, 0, -5, None, 1e-9, 59.9]
)


def _times(rng: random.Random) -> list[float]:
    out = [0.0, -1.0, -1e-9, 1e-9, 1e-7, 0.25, 0.5, 0.75, 1.25, 1.5, 2.5, 0.0125, 5.0125,
           1 / 30, 2 / 30, 0.1, 0.2, 0.3, 1 / 3, 2 / 3, 0.016683, 0.0078125, 7.5 / 30,
           3600.0, 3600.0 + 1 / 60, 21599.97, 43200.5, 0.041708333333333333]
    for r in tb.STANDARD_RATES:
        for k in (1, 3, 29, 1001, 107892):
            t = float(Fraction(k) / r)
            out += [t, t + 1e-7, t - 1e-7, t + 1e-9, t - 1e-9,
                    float((Fraction(k) + Fraction(1, 2)) / r)]
    out += [rng.uniform(0, 30) for _ in range(40)]
    out += [rng.uniform(0, 43200) for _ in range(12)]
    out += _near_ties()
    return out


def _near_ties() -> list[float]:
    """Times within a few ulp of a half-frame tie at the NTSC rates (review
    RD2): the float product t·num/den can land on the other side of k + 0.5
    from the exact one, which only the TS port's TIE_MARGIN escape to exact
    arithmetic gets right (a TIE_MARGIN of 0 used to pass every case)."""
    out: list[float] = []
    for r in (Fraction(30000, 1001), Fraction(60000, 1001), Fraction(24000, 1001)):
        for k in (0, 1, 2, 7, 29, 59, 1000, 1001, 4095, 30000, 107891, 2 ** 20 + 3):
            t = float((Fraction(k) + Fraction(1, 2)) / r)
            lo = hi = t
            for _ in range(4):
                lo = math.nextafter(lo, -math.inf)
                hi = math.nextafter(hi, math.inf)
                out += [lo, hi]
    return out


#: Rates the per-time grid is evaluated at (every standard rate as stored,
#: integers, decimal NTSC spellings, off-standard and invalid inputs).
GRID_FPS: list = ([float(r) for r in tb.STANDARD_RATES]
                  + [24, 30, 60, 29.97, 23.976, 59.94, 12.5, 120, 29.5, 0, None])


def _rate(fps) -> list[int]:
    r = tb.rate_of(fps)
    return [r.numerator, r.denominator]


def build_cases() -> dict:
    """Column-oriented: ``grid.<fn>[i][j]`` is ``fn(times[j], fps[i])``."""
    rng = random.Random(7)
    times = _times(rng)
    frames = [-3, 0, 1, 2, 3, 29, 30, 1001, 5000, 1_295_999, 2_591_999]
    grid: dict = {"times": times, "fps": GRID_FPS, "frames": frames}
    for name, fn in (("frame_of", tb.frame_of), ("quantize", tb.quantize),
                     ("floor_to_frame", tb.floor_to_frame), ("ceil_to_frame", tb.ceil_to_frame),
                     ("seek_preroll", tb.seek_preroll)):
        grid[name] = [[fn(t, f) for t in times] for f in GRID_FPS]
    grid["time_of"] = [[tb.time_of(n, f) for n in frames] for f in GRID_FPS]
    grid["samples_for_frames"] = [[tb.samples_for_frames(n, f) for n in frames] for f in GRID_FPS]
    grid["samples_44100"] = [[tb.samples_for_frames(n, f, 44100) for n in frames] for f in GRID_FPS]
    cases: dict = {"grid": grid, "rate_of": [], "scalars": [], "frames_between": [],
                   "enable_window": [], "source_rate": [], "ffmpeg_us": []}
    for f in FPS_INPUTS:
        cases["rate_of"].append([f, _rate(f)])
        cases["scalars"].append([f, tb.fps_float(f), tb.ffmpeg_rate(f), tb.frame_duration(f)])
    for f in GRID_FPS:
        for _ in range(12):
            a, b = rng.uniform(0, 60), rng.uniform(0, 60)
            cases["frames_between"].append([a, b, f, tb.frames_between(a, b, f)])
            cases["enable_window"].append([a, b, f, list(tb.enable_window(a, b, f))])
    for avg, nom in [(29.97, "30000/1001"), ("30000/1001", "30000/1001"), (29.98, 30), (29.98, "30000/1001"),
                     (25, 25), (120, 120), (90.0, 90), (100, "100/1"), (23.976, None), (None, None),
                     (None, 25), ("24000/1001", "24/1"), (59.94, 60), (47.95, 48), (12.5, 12.5),
                     (0, 30), ("0/0", "30/1"), ("abc", 30), (29.5, 30), (58.3, 60), (300, 300),
                     (14.99, 15), (24.5, 25), ("1/0", None), (-5, 30), (60.0, "60000/1001"),
                     (30.0, 29.97), (50.04, 50), (1000, 1000), (0.5, 0.5)]:
        cases["source_rate"].append([avg, nom, _rate(tb.source_rate(avg, nom))])
    for x in [0.0, 1e-7, 4e-7, 5e-7, 6e-7, 0.0078125, 0.0234375, 1 / 3, 2 / 3, 13.596917,
              14.37436, 1.4666666666666666, 3600.0000005, 0.1 + 0.2, 123.4567895,
              0.0000005, 2.5e-6, 7.8125e-3] + [rng.uniform(0, 100) for _ in range(60)]:
        cases["ffmpeg_us"].append([x, ffmpeg_us(x)])
    # R9's one audio start rule, over every grid time plus the µs ties
    # and the µs values nearest a half sample on either side (x·48000 is
    # never a tie once printed: us·6/125 has a fraction in 1/125ths)
    near = [us / 1e6 for k in (0, 1, 7, 2401, 510910, 10**7)
            for us in ((2 * k + 1) * 125 // 12, (2 * k + 1) * 125 // 12 + 1)]
    cases["edit_sample"] = [[x, tb.edit_sample(x)]
                            for x in times + [x for x, _ in cases["ffmpeg_us"]] + near]
    return cases


def _canonical(doc) -> str:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), allow_nan=False)


def test_timebase_golden_matches_python():
    cases = build_cases()
    if os.environ.get("VAI_REGEN_GOLDENS") == "1" or not GOLDEN.exists():
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(_canonical(cases) + "\n")
    stored = json.loads(GOLDEN.read_text())
    assert _canonical(stored) == _canonical(cases), (
        "tests/goldens/timebase_cases.json is stale — edl/timebase.py changed; "
        "regenerate with VAI_REGEN_GOLDENS=1 and re-run the vitest parity suite")


def test_golden_covers_ties_and_the_whole_rate_list():
    cases = json.loads(GOLDEN.read_text())
    rates = {tuple(r) for _f, r in cases["rate_of"]}
    for r in tb.STANDARD_RATES:
        assert (r.numerator, r.denominator) in rates
    # Exact half-frame ties are present and resolve to EVEN (Python round).
    grid = cases["grid"]
    col = grid["fps"].index(30)
    ties = [(t, n) for t, n in zip(grid["times"], grid["frame_of"][col])
            if t > 0 and (Fraction(t) * 30).denominator == 2]
    assert ties and all(n % 2 == 0 for _t, n in ties)
    assert len(grid["times"]) * len(grid["fps"]) > 5000
    # The %.6f parse includes exact half-microsecond ties (0.0078125 s).
    assert [x for x, _ in cases["ffmpeg_us"] if (Fraction(x) * 10**6).denominator == 2]
    # edit_sample (R9) is the NEAREST sample to the printed µs value, and the
    # table holds values within one µs step (0.048 sample) either side of a half.
    fr = {x: Fraction(ffmpeg_us(x), 10**6) * 48000 for x, _ in cases["edit_sample"] if x > 0}
    assert all(s == round(fr[x]) for x, s in cases["edit_sample"] if x > 0)
    fracs = {v - math.floor(v) for v in fr.values()}
    assert any(Fraction(9, 20) < f < Fraction(1, 2) for f in fracs)
    assert any(Fraction(1, 2) < f < Fraction(11, 20) for f in fracs)
    assert all(math.isfinite(v) for row in grid["quantize"] for v in row)
