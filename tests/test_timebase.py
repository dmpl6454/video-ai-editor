"""The project timebase contract (edl/timebase.py).

Pinned because several fix lanes code against it concurrently and because the
0.7.2 QA sweep traced two CRITICAL defects (QA-002 sub-frame edit points that
duplicated frames on export, QA-009 integer-only 30 fps) to the absence of a
single seconds<->frames rule.
"""
from __future__ import annotations

from fractions import Fraction

import pytest

from video_ai_editor.edl import timebase as tb


@pytest.mark.parametrize("stored, expected", [
    (30, Fraction(30)),
    (30.0, Fraction(30)),
    (29.97, Fraction(30000, 1001)),
    (29.97002997, Fraction(30000, 1001)),
    (Fraction(30000, 1001), Fraction(30000, 1001)),
    (23.976, Fraction(24000, 1001)),
    (59.94, Fraction(60000, 1001)),
    (25, Fraction(25)),
    (50, Fraction(50)),
    (60, Fraction(60)),
])
def test_stored_rates_snap_to_the_exact_rational(stored, expected):
    assert tb.rate_of(stored) == expected


@pytest.mark.parametrize("bad", [None, 0, -30, "thirty", float("nan")])
def test_nonsense_rates_fall_back_to_30_rather_than_raise(bad):
    # An EDL that already exists must stay loadable.
    assert tb.rate_of(bad) == Fraction(30)


def test_a_nonstandard_rate_is_kept_not_forced():
    assert tb.rate_of(12) == Fraction(12)
    assert tb.rate_of(15) == Fraction(15)


def test_ffmpeg_rate_is_never_a_rounded_decimal():
    assert tb.ffmpeg_rate(30) == "30"
    assert tb.ffmpeg_rate(29.97) == "30000/1001"
    assert tb.ffmpeg_rate(23.976) == "24000/1001"


def test_quantize_lands_on_frame_boundaries_the_qa_split_case():
    # QA-002: a split at 5.0125 s on a 30 fps project was stored verbatim.
    assert tb.quantize(5.0125, 30) == pytest.approx(150 / 30)
    assert tb.frame_of(5.0125, 30) == 150
    # 3 frames later
    assert tb.quantize(5.0125 + 3 / 30, 30) == pytest.approx(153 / 30)


@pytest.mark.parametrize("fps", [30, 29.97, 23.976, 25, 59.94, 60])
@pytest.mark.parametrize("t", [0.0, 0.001, 1 / 3, 5.0125, 16.776666, 71.8622, 3600.017])
def test_quantize_is_idempotent_and_exact(fps, t):
    q = tb.quantize(t, fps)
    assert tb.quantize(q, fps) == q, "a second pass must not move a cut"
    # q is an exact frame boundary of the rational rate
    n = tb.frame_of(q, fps)
    assert Fraction(q).limit_denominator(10**7) == pytest.approx(float(Fraction(n) / tb.rate_of(fps)))


def test_long_timelines_do_not_accumulate_float_drift_at_ntsc_rates():
    # Frame 107892 at 29.97 is 3600.0 s of NTSC time (one hour of drop-frame
    # content is 107892 frames); summing float frame durations drifts, the
    # rational does not.
    assert tb.time_of(107892, 29.97) == pytest.approx(107892 * 1001 / 30000, abs=1e-9)
    assert tb.frame_of(tb.time_of(107892, 29.97), 29.97) == 107892


def test_negative_times_clamp_to_the_first_frame():
    assert tb.frame_of(-0.5, 30) == 0
    assert tb.quantize(-2.0, 30) == 0.0


def test_frames_between_counts_what_a_renderer_must_emit():
    assert tb.frames_between(0.0, 20.0, 30) == 600
    assert tb.frames_between(5.0, 5.1, 30) == 3
    assert tb.frames_between(2.0, 1.0, 30) == 0
    assert tb.frames_between(0.0, 10.0, 29.97) == 300  # 299.7 rounds to 300


def test_frame_duration_and_float_view():
    assert tb.frame_duration(30) == pytest.approx(1 / 30)
    assert tb.frame_duration(29.97) == pytest.approx(1001 / 30000)
    assert tb.fps_float(29.97) == pytest.approx(30000 / 1001)
