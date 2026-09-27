"""The project timebase: the ONE place that turns seconds into frames and back.

WHY THIS MODULE EXISTS. The 0.7.2 QA sweep (QA-002, QA-009) found that no
committed edit time was ever quantised to a frame: split, trim, cut_range and
remove_silences stored values like 5.0125 s, the renderer trimmed with float
``-ss/-to`` and rounded every clip up, and a pure split of a 600-frame clip
exported 602 frames with two duplicated. At the same time ingest forced every
source to integer 30 fps, so 23.976/25 sources juddered and 59.94 lost half its
frames, and ``Canvas.fps`` (an ``int``) could not even represent 29.97.

Both fixes need the same arithmetic, and a second copy of it anywhere is how
the preview, the export and the EDL came to disagree in the first place. So
every consumer (dispatch edit ops, ingest, the compositor, the verifier, the
frontend's mirror in ``frontend/src/lib/frameStep.ts``) goes through these
functions and nothing else rounds seconds to frames by hand.

THE MODEL. A project has one frame rate, ``canvas.fps``. It may be an integer
(30) or a broadcast NTSC rate stored as a float (29.97002997…). Every function
here snaps that float to the exact rational it stands for, so 29.97 and
30000/1001 are the same timebase and frame n of it is exactly n*1001/30000 s,
not an accumulating float drift.

FROZEN CONTRACT (wave A of the 0.7.3 fix pass): lanes may ADD functions here,
but must not change the signatures or semantics below — several lanes code
against them concurrently.
"""
from __future__ import annotations

import functools
from fractions import Fraction

#: The rates a real camera, phone or NLE produces. A measured or stored fps
#: within 0.1 % of one of these IS that rate (ffprobe reports 29.97002997 or
#: 29.97 or 30000/1001 for the same stream depending on the container).
STANDARD_RATES: tuple[Fraction, ...] = (
    Fraction(24000, 1001), Fraction(24), Fraction(25),
    Fraction(30000, 1001), Fraction(30),
    Fraction(48), Fraction(50),
    Fraction(60000, 1001), Fraction(60),
)

#: Relative tolerance for snapping to a standard rate (0.1 %).
_SNAP_TOLERANCE = 0.001

#: Fallback when a stored fps is missing, zero or nonsense — the historical
#: project default, so an old EDL keeps meaning what it always meant.
DEFAULT_RATE = Fraction(30)


def rate_of(fps: float | int | Fraction | None) -> Fraction:
    """The exact frame rate a stored ``fps`` stands for.

    Snaps to the nearest standard rate within 0.1 %; otherwise keeps a
    rational approximation with a denominator of at most 1001 (enough for any
    NTSC-style rate and exact for integers). Non-positive or missing values
    fall back to ``DEFAULT_RATE`` rather than raising: this is called on EDLs
    that already exist, and an unloadable timeline is worse than a 30 fps one.

    Memoised (a pure function of a hashable value; the program map calls it
    thousands of times per timeline — wave D3, frame_map_json at 300 clips).
    """
    try:
        return _rate_of_cached(fps)
    except TypeError:          # an unhashable value: the uncached rule
        return _rate_of(fps)


def _rate_of(fps) -> Fraction:
    if fps is None:
        return DEFAULT_RATE
    try:
        value = Fraction(fps).limit_denominator(1001) if not isinstance(fps, Fraction) else fps
    except (TypeError, ValueError, ZeroDivisionError):
        return DEFAULT_RATE
    if value <= 0:
        return DEFAULT_RATE
    # NEAREST standard within tolerance, not the first: 24 and 24000/1001 are
    # exactly 0.1 % apart, so a first-match loop snapped an exact 24 to 23.976
    # (lane A2 fix; only ties like that one change — every other rate had a
    # single candidate within tolerance).
    candidates = [std for std in STANDARD_RATES
                  if abs(float(value) - float(std)) <= float(std) * _SNAP_TOLERANCE]
    if candidates:
        return min(candidates, key=lambda std: abs(float(value) - float(std)))
    return value


_rate_of_cached = functools.lru_cache(maxsize=256)(_rate_of)


def fps_float(fps: float | int | Fraction | None) -> float:
    """The rate as a float for storage/display (29.97002997…, 30.0)."""
    return float(rate_of(fps))


def ffmpeg_rate(fps: float | int | Fraction | None) -> str:
    """The rate as ffmpeg wants it: ``30`` or ``30000/1001`` (never a
    rounded decimal, which would itself drift over a long timeline)."""
    r = rate_of(fps)
    return str(r.numerator) if r.denominator == 1 else f"{r.numerator}/{r.denominator}"


def frame_duration(fps: float | int | Fraction | None) -> float:
    """Seconds per frame (memoised, like ``rate_of``)."""
    try:
        return _frame_duration_cached(fps)
    except TypeError:          # an unhashable value
        return float(1 / rate_of(fps))


@functools.lru_cache(maxsize=256)
def _frame_duration_cached(fps) -> float:
    return float(1 / rate_of(fps))


def frame_of(t: float, fps: float | int | Fraction | None) -> int:
    """The frame index nearest to time ``t`` (round half away from zero is
    not needed: times are non-negative, and Python's round-half-even on an
    exact half-frame is irrelevant after quantisation).

    Negative times clamp to frame 0 — no edit point may precede the start.
    """
    if t is None or t <= 0:
        return 0
    return int(round(Fraction(t) * rate_of(fps)))


def time_of(frame: int, fps: float | int | Fraction | None) -> float:
    """The exact start time of frame ``frame``."""
    if frame <= 0:
        return 0.0
    return float(Fraction(frame) / rate_of(fps))


def quantize(t: float, fps: float | int | Fraction | None) -> float:
    """``t`` snapped to the nearest frame boundary of the project timebase.

    This is the function every committed edit time passes through (split,
    trim, move, cut_range, remove_silences/fillers, vo_record start, …). It is
    idempotent — quantising an already-quantised time returns the same float —
    which the regression tests pin, because a second pass that moved a cut by
    one ULP would re-open the drift this module exists to close.
    """
    return time_of(frame_of(t, fps), fps)


def frames_between(start: float, end: float, fps: float | int | Fraction | None) -> int:
    """Whole frames in ``[start, end)`` on the project timebase: the frame
    count a renderer must emit for a clip occupying that span. Never negative."""
    return max(0, frame_of(end, fps) - frame_of(start, fps))


def _fraction_or_none(fps: float | int | Fraction | str | None) -> Fraction | None:
    """``fps`` as a positive Fraction, or None when missing/invalid. Accepts
    ffprobe's ``"30000/1001"`` strings as well as numbers."""
    if fps is None:
        return None
    try:
        if isinstance(fps, str):
            text = fps.strip()
            if "/" in text:
                num, den = text.split("/", 1)
                if float(den) == 0:
                    return None
                value = Fraction(num) / Fraction(den)
            else:
                value = Fraction(text)
        else:
            value = Fraction(fps)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return value if value > 0 else None


def _standard_or_none(value: Fraction | None) -> Fraction | None:
    if value is None:
        return None
    snapped = rate_of(value)
    return snapped if snapped in STANDARD_RATES else None


def source_rate(avg_fps: float | int | Fraction | str | None,
                nominal_fps: float | int | Fraction | str | None = None) -> Fraction:
    """The CFR rate a SOURCE should be normalised to (QA-009, added by lane A2).

    ``avg_fps`` is ffprobe's ``avg_frame_rate`` (frames / duration) and
    ``nominal_fps`` its ``r_frame_rate`` (the container's base rate). Rules, in
    order:

    1. Both snap to standard rates that are within 0.2 % of each other (the
       29.97-vs-30 ambiguity a VFR phone clip averaging 29.98 creates): the
       NOMINAL rate wins, because it is what the camera was set to.
    2. The average snaps to a standard rate: that rate (an ordinary CFR file).
    3. A constant but non-standard rate (120, 90, 100 fps — average and
       nominal agree): kept as-is, so slow-motion sources keep their frames.
    4. Variable frame rate whose nominal rate is standard and not below the
       average: the nominal rate.
    5. Otherwise the standard rate nearest the average.

    Missing/invalid input falls back to ``DEFAULT_RATE``, never raises.
    """
    avg = _fraction_or_none(avg_fps)
    nominal = _fraction_or_none(nominal_fps)
    if avg is None and nominal is None:
        return DEFAULT_RATE
    if avg is None:
        avg = nominal
    assert avg is not None
    avg_std = _standard_or_none(avg)
    nom_std = _standard_or_none(nominal)
    if avg_std is not None and nom_std is not None and \
            abs(float(avg_std) - float(nom_std)) <= float(nom_std) * 0.002:
        return nom_std
    if avg_std is not None:
        return avg_std
    if nominal is not None and 1 <= float(avg) <= 240 and \
            abs(float(avg) - float(nominal)) <= float(nominal) * _SNAP_TOLERANCE:
        return rate_of(avg)
    if nom_std is not None and float(avg) <= float(nom_std) * (1 + _SNAP_TOLERANCE):
        return nom_std
    return min(STANDARD_RATES, key=lambda std: abs(float(std) - float(avg)))


# ---- lane A1 (render timing, QA-002/030/038/039) — additive helpers --------

def floor_to_frame(t: float, fps: float | int | Fraction | None) -> float:
    """The last frame boundary at or before ``t`` (a 1 µs tolerance absorbs
    float noise, so 20.0 on a 30 fps grid stays 20.0, not 19.9667).

    For a media EXTENT rather than an edit point: a source whose picture is
    20.013 s long holds 600 whole frames, and rounding to nearest would claim
    a 601st that does not exist.
    """
    if t is None or t <= 0:
        return 0.0
    r = rate_of(fps)
    n = int((Fraction(t) * r + Fraction(1, 1_000_000)) // 1)
    return time_of(n, r)


def ceil_to_frame(t: float, fps: float | int | Fraction | None) -> float:
    """The first frame boundary at or after ``t`` (1 µs tolerance, so 5.0
    stays 5.0). With ``floor_to_frame`` this rounds a range OUTWARD — for a
    removal that must take all of what was asked for (a filler word, a
    silence), where nearest-rounding could leave half a frame of it behind."""
    if t is None or t <= 0:
        return 0.0
    r = rate_of(fps)
    x = Fraction(t) * r - Fraction(1, 1_000_000)
    n = -((-x) // 1)
    return time_of(int(n), r)


def samples_for_frames(frames: int, fps: float | int | Fraction | None,
                       sample_rate: int = 48000) -> int:
    """Audio samples that span exactly ``frames`` frames of the timebase
    (rounded to the nearest sample — 1601.6 per frame at 29.97, 1600 at 30)."""
    if frames <= 0:
        return 0
    return int(round(Fraction(frames) * sample_rate / rate_of(fps)))


def seek_preroll(t: float, fps: float | int | Fraction | None) -> float:
    """How far BEFORE edit point ``t`` an input-side seek should land: half a
    frame (never below 0). Seeking to exactly ``t`` lets float formatting or a
    container timebase put the seek a hair PAST the frame that starts at
    ``t``, which then gets dropped; half a frame early keeps it and still
    excludes the previous frame, which starts a whole frame early."""
    if t is None or t <= 0:
        return 0.0
    return min(float(t), frame_duration(fps) / 2.0)



__all__ = [
    "STANDARD_RATES", "DEFAULT_RATE",
    "rate_of", "fps_float", "ffmpeg_rate", "frame_duration",
    "frame_of", "time_of", "quantize", "frames_between",
    "source_rate",
]
__all__ += ["floor_to_frame", "ceil_to_frame", "samples_for_frames", "seek_preroll"]


def enable_window(start: float, end: float,
                  fps: float | int | Fraction | None) -> tuple[float, float]:
    """A half-open overlay window ``[start, end)`` as frame-safe gate bounds.

    Returns ``(lo, hi)`` such that the frame shown at time ``n / fps`` is
    inside the window exactly when ``frame_of(start) <= n < frame_of(end)``,
    i.e. test it as ``lo <= t < hi`` (ffmpeg: ``gte(t,lo)*lt(t,hi)``).

    WHY (QA-016). Overlay gates were ``between(t, start, end)`` — closed at
    BOTH ends — so on the frame where one caption ends exactly as the next
    begins (an SRT cue change at 3.000 s) both were drawn, overprinted. A
    plain ``gte(t,start)*lt(t,end)`` is still fragile: a stream clock that
    reads 2.99999999 for frame 90 flips the boundary frame. The bounds sit
    half a frame BEFORE each boundary frame, so float noise in either
    direction can never move a boundary, and a non-frame-aligned time lands on
    the nearest frame exactly like ``quantize`` would put it.
    """
    half = frame_duration(fps) / 2
    lo = time_of(frame_of(start, fps), fps) - half
    hi = time_of(frame_of(end, fps), fps) - half
    return lo, max(lo, hi)


__all__ += ["enable_window"]
