"""The audio curve golden (INSTANT_PREVIEW_SPEC §6 R10, §13 test_audio_map_golden):
``tests/goldens/audio_curve_cases.json`` is what REAL ffmpeg does with the
fades, cross-fades and gain envelopes the export emits, and the preview's
``audio/curves.ts`` is pinned to it (``curves.test.ts``).

* The checked-in file must equal a fresh render: a new ffmpeg, or an export
  chain that formats a fade differently, fails here (regenerate with
  ``tests/gen_audio_curve_goldens.py``) and then in vitest.
* The closed forms the TS port uses are checked against the same data, so the
  rule each shape follows is written down next to its evidence.
"""
from __future__ import annotations

import json
import math
from fractions import Fraction

import pytest

import gen_audio_curve_goldens as gen

DOC = json.loads(gen.GOLDEN.read_text())
SR = DOC["sample_rate"]


def test_golden_is_what_ffmpeg_does_today():
    fresh = gen.generate()
    stale = [k for k in ("curves", "emitted", "acrossfade", "gain_env") if fresh[k] != DOC[k]]
    assert stale == [], f"regenerate tests/goldens/audio_curve_cases.json: {stale} changed"
    assert fresh["ffmpeg"] == DOC["ffmpeg"]


def _fade_gain(curve: str, index: int, rng: int) -> float:
    """af_afade.c ``fade_gain`` (silence 0, unity 1) — the rule curves.ts ports."""
    g = min(1.0, max(0.0, index / rng)) if rng > 0 else 1.0
    a = 1.0 / (1.0 - 0.787) - 1
    return {
        "tri": lambda: g,
        "qsin": lambda: math.sin(g * math.pi / 2),
        "iqsin": lambda: 0.6366197723675814 * math.asin(g),
        "esin": lambda: 1 - math.cos(math.pi / 4 * ((2 * g - 1) ** 3 + 1)),
        "hsin": lambda: (1 - math.cos(g * math.pi)) / 2,
        "ihsin": lambda: 0.3183098861837907 * math.acos(1 - 2 * g),
        "exp": lambda: math.exp(-11.512925464970227 * (1 - g)),
        "log": lambda: min(1.0, max(0.0, 1 + 0.2 * math.log10(g))) if g > 0 else 0.0,
        "par": lambda: 1 - math.sqrt(1 - g),
        "ipar": lambda: 1 - (1 - g) * (1 - g),
        "qua": lambda: g * g,
        "cub": lambda: g ** 3,
        "squ": lambda: math.sqrt(g),
        "cbr": lambda: g ** (1 / 3),
        "dese": lambda: (2 * g) ** (1 / 3) / 2 if g <= 0.5 else 1 - (2 * (1 - g)) ** (1 / 3) / 2,
        "desi": lambda: (2 * g) ** 3 / 2 if g <= 0.5 else 1 - (2 * (1 - g)) ** 3 / 2,
        "losi": lambda: ((1 / (1 + math.exp(-((g - 0.5) * a * 2))) - 1 / (1 + math.exp(a)))
                         / (1 / (1 + math.exp(-a)) - 1 / (1 + math.exp(a)))),
        "sinc": lambda: 1.0 if g >= 1 else math.sin(math.pi * (1 - g)) / (math.pi * (1 - g)),
        "isinc": lambda: 0.0 if g <= 0 else 1 - math.sin(math.pi * g) / (math.pi * g),
        "quat": lambda: g ** 4,
        "quatr": lambda: g ** 0.25,
        "qsin2": lambda: math.sin(g * math.pi / 2) ** 2,
        "hsin2": lambda: ((1 - math.cos(g * math.pi)) / 2) ** 2,
        "nofade": lambda: 1.0,
    }[curve]()


@pytest.mark.parametrize("case", DOC["curves"], ids=lambda c: c["filter"])
def test_afade_closed_form(case):
    start, rng = case["start"], case["range"]
    assert start == round(float(case["st"]) * SR) and rng == round(float(case["d"]) * SR)
    for i, g in case["samples"]:
        k = i - start
        if case["curve"] == "nofade":
            exp = 1.0
        elif case["type"] == "in":
            exp = _fade_gain(case["curve"], k, rng)        # clipped: its floor before the start
        else:
            exp = 1.0 if k < 0 else _fade_gain(case["curve"], rng - k, rng)
        assert g == pytest.approx(exp, abs=1e-8), (i, g, exp)


def _us(text: str) -> int:
    whole, _, frac = text.partition(".")
    return int(whole) * 1_000_000 + int((frac + "000000")[:6])


@pytest.mark.parametrize("case", DOC["emitted"], ids=lambda c: f"{c['lane']}-{c['fade_in']}-{c['fade_out']}")
def test_emitted_fades_land_where_their_text_says(case):
    """ffmpeg puts a fade at av_rescale(st_us, 48000, 1e6) for `range`
    av_rescale(d_us, …) samples, st/d being the %.3f text the chain wrote."""
    for f in case["fades"]:
        assert f["start"] == round(Fraction(_us(f["st"]) * SR, 1_000_000))
        assert f["range"] == round(Fraction(_us(f["d"]) * SR, 1_000_000))


@pytest.mark.parametrize("case", DOC["acrossfade"], ids=lambda c: c["d"])
def test_acrossfade_closed_form(case):
    """Overlap m = av_rescale(d_us); outgoing (m−1−i)/m, incoming i/m."""
    m, s = case["m"], case["first"]
    assert m == round(Fraction(_us(case["d"]) * SR, 1_000_000))
    for (p, g0), (_p, g1) in zip(case["g0"], case["g1"]):
        i = p - s
        e0 = 1.0 if i < 0 else 0.0 if i >= m else (m - 1 - i) / m
        e1 = 0.0 if i < 0 else 1.0 if i >= m else i / m
        assert (g0, g1) == (pytest.approx(e0, abs=1e-9), pytest.approx(e1, abs=1e-9)), p


def test_the_export_emits_only_the_triangle_curve():
    """No emitted fade names a curve, so ffmpeg's default (tri) is the one the
    preview must reproduce exactly; the others are pinned for completeness."""
    assert all("curve" not in f for c in DOC["emitted"] for f in c["fades"])
    assert DOC["emitted"] and all(c["fades"] or c["fade_in"] < 0.001 for c in DOC["emitted"])
