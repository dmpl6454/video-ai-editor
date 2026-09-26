"""Shared rules for asserting on a PLAYING trace in real WKWebView.

Dropped frames are a smoothness measurement, not a correctness one. The
properties these tests exist to prove (the frame on screen is exactly frame k,
mediaTime sits on the k/R grid, the clock is monotonic and runs at R) are about
the frames that ARE presented, and every test asserts those exactly.

Under heavy load from OTHER processes WebKit drops a few presented frames,
mostly as playback starts: the wave D1 gate measured [2, 3, 4, 22] at a load
average near 10 while two unrelated projects were busy. A fixed "at most 2"
failed those runs even though every presented frame was right. The bound is
therefore proportional: it still fails systematic dropping (a codec, pacing or
scheduling bug drops far more than 5%), and smoothness budgets proper belong to
the Phase 1d soak tests, which measure them deliberately.
"""
from __future__ import annotations

#: At most this fraction of a trace's frames may be missing, never below 2.
MAX_DROP_FRACTION = 0.05


def max_drops(frames: int) -> int:
    return max(2, int(MAX_DROP_FRACTION * max(0, frames)))


def assert_drops_bounded(trace: dict) -> None:
    missing = trace.get("missingK") or []
    frames = int(trace.get("frames") or 0)
    assert len(missing) <= max_drops(frames), (
        f"{len(missing)} of {frames} presented frames missing (bound {max_drops(frames)}): {missing}")
