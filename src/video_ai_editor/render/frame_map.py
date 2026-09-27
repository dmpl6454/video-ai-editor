"""The reference PROGRAM MAP: which source frame (and which source audio
sample) every output frame of a render uses (Wave D, instant preview §6 R4,
R9, R14; §8).

WHY THIS MODULE EXISTS. The instant-preview engine draws the timeline in the
browser from per-source proxy frames: for output frame ``k`` it appends the
proxy sample that ``programMap[k]`` names. If that choice differs from the one
the ffmpeg compositor makes by a single frame, the preview shows a frame the
export never contains. The compositor's choice is not written down anywhere
as arithmetic — it EMERGES from an input seek, ``setpts``, the ``fps`` filter,
``tpad``/``trim``, the reversed-intermediate recipe and ``xfade`` — so this
module re-derives it, step by step, in exact integer arithmetic on the same
numbers the compositor prints into its ffmpeg command lines:

* the input seek ``-ss`` lands HALF A PROJECT FRAME before ``in``
  (``timebase.seek_preroll``) and is printed ``%.6f``, so ffmpeg parses an
  exact microsecond value; the demuxer shifts timestamps by it rounded into
  the stream time base, and the accurate-seek trim keeps every frame whose
  shifted pts is >= 0;
* ``-to`` (``-t`` for the reversed intermediate) becomes a trim DURATION
  measured from the first kept frame;
* ``setpts=PTS-STARTPTS`` rebases to 0, ``setpts=PTS/speed`` divides in
  double and truncates (``D2TS``);
* a speed CURVE runs on the file's clock anchored at ``in`` (wave D3,
  ``edl/speed_curve.py`` "the v1 chain's clock"): a seek on a 1/5 s grid
  0.5 s early, the demuxer's shift added back, ``settb`` refining the time
  base so a project frame is whole ticks, the closed-form ``setpts`` of
  ``T = PTS·TB − in`` (``speed_curve.anchored_ticks``, the same doubles)
  and ``fps=R:start_time=0`` — so a split piece shows its parent's frames;
* a FREEZE (``Clip.freeze``) is the first output frame of a 1x chain opened
  at ``in`` (``compositor.freeze_input_span``), cloned for the whole hold;
* ``fps=R`` (round=near) rounds every pts half-away-from-zero into output
  slots and shows, in slot ``s``, the latest frame whose slot is ``<= s``;
  at EOF it flushes up to the rounded end timestamp;
* ``tpad=stop_mode=clone`` + ``trim=end_frame=n`` repeat the last frame to
  exactly ``clip_frames`` (the "freeze" a source that runs short shows);
* a reversed clip plays a cached intermediate built segment by segment
  (``render/reverse.py``) — modelled segment by segment here too;
* v1 is assembled by ``_v1_frame_plan`` (gaps, legacy overlaps) and folded
  through ``xfade`` at the seams ``seam_table_for`` charges.

Every one of those steps is pinned by a GOLDEN TABLE rendered through the
real compositor from bar-coded sources and decoded frame by frame
(``tests/goldens/frame_map/*.json``, ``tests/test_frame_map_golden.py``). The
browser port (``frontend/src/lib/preview/timeline/``) is pinned to the same
JSON. Change a compositor rule without regenerating the goldens and both
sides fail.

SPEED (wave D3). The selection is integer and vectorised arithmetic
(``render/frame_map_vec.py``), the fold writes the per-frame arrays
directly, the RLE finds its runs with array compares, and
``clip_frame_list`` is memoised per clip: 300 clips / 21,600 frames in
~11 ms cold (was ~140 ms), ~7 ms after an edit. ``tests/frame_map_reference.py``
keeps the per-frame ``Fraction`` reading; ``tests/test_frame_map_perf.py``
holds this module to it byte for byte (goldens and fuzz).

WHAT IT DOES NOT DO. Pixels (geometry, colour, effects) are not modelled —
only frame SELECTION. The plan functions (``_v1_frame_plan``,
``clip_frames``, ``seam_table_for``) are the compositor's own, imported, not
copied.
"""
from __future__ import annotations

import dataclasses
import functools
import math
import threading
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Callable, Mapping, Sequence

import numpy as np

from ..edl import speed_curve as _sc
from ..edl import timebase as _tb
from ..edl.schema import EDL, Clip, seam_matching, seam_table_for
from .frame_map_vec import (  # noqa: F401 — re-exported (tests, the fast path)
    _I64_SAFE, _ArithIndex, _decode_varints, _encode_seq_np, _i8, _i64, _out_seconds_vec, _rescale_int,
    _rescale_vec, _select_scan, _trunc_i64, _varints, _zigzag,
)

#: MSE timescale the client writes its fragments in (spec §3.1): an integer
#: number of ticks per frame for every standard rate.
MSE_TIMESCALE = 240000

#: Frame-map format version. Bump when the JSON SHAPE changes (the goldens
#: record it in every case's model). A selection-rule change rides on
#: ``RENDER_BEHAVIOR_VERSION`` — every map is served for a ``render_hash``,
#: which it salts — and on regenerated goldens (wave D3: the in-anchored
#: curve clock, version 20, left this at 1 so the constant-speed goldens
#: stay byte-identical).
FRAME_MAP_VERSION = 1

#: ``kind`` codes (shared with ``programMap.ts``).
KIND_CLIP, KIND_GAP, KIND_BLEND = 0, 1, 2

_US = Fraction(1, 1_000_000)
#: Frames of decode slack past ``out`` (compositor._DECODE_SLACK_FRAMES).
_DECODE_SLACK_FRAMES = 2
#: Frames of decode slack past a reversed segment (reverse._SLACK_FRAMES).
_REV_SLACK_FRAMES = 2
_SAMPLE_RATE = 48000


# ---------------------------------------------------------------- arithmetic

def round_half_away(x: Fraction) -> int:
    """libavutil's ``AV_ROUND_NEAR_INF``: nearest, ties away from zero."""
    if x >= 0:
        return math.floor(x + Fraction(1, 2))
    return -math.floor(-x + Fraction(1, 2))


def rescale(a: int, src_tb: Fraction, dst_tb: Fraction) -> int:
    """``av_rescale_q(a, src_tb, dst_tb)`` (round to nearest, ties away)."""
    return round_half_away(Fraction(a) * src_tb / dst_tb)


def ffmpeg_us(x: float) -> int:
    """The microseconds ffmpeg parses from ``f"{x:.6f}"`` — the only form in
    which the compositor hands it a time (``-ss``, ``-to``, ``-t``)."""
    text = f"{x:.6f}"
    neg = text.startswith("-")
    whole, frac = text.lstrip("-").split(".")
    us = int(whole) * 1_000_000 + int(frac)
    return -us if neg else us


def ticks_per_frame(fps) -> int | None:
    """``T`` of spec R1: MSE ticks (timescale 240000) per project frame, or
    None when the rate does not divide evenly (the engine then refuses)."""
    r = _tb.rate_of(fps)
    t = Fraction(MSE_TIMESCALE) / r
    return t.numerator if t.denominator == 1 else None


def default_time_base(rate) -> Fraction:
    """The stream time base ffmpeg's mp4/mov muxer gives a CFR encode at
    ``rate`` (movenc: the encoder's 1/rate denominator, doubled until it is
    at least 10000). 30 → 1/15360, 29.97 → 1/30000, 25 → 1/12800."""
    r = _tb.rate_of(rate)
    scale = r.numerator
    while scale < 10000:
        scale *= 2
    return Fraction(1, scale)


# ---------------------------------------------------------------- sources

@dataclass(frozen=True)
class SourceInfo:
    """What the program map needs to know about one source (a master).

    ``time_base`` and ``start_ticks`` matter only at exact rounding ties, but
    they DO decide those ties, so they are part of the contract: the client
    gets them from the same media row / proxy index the server built this
    from. ``start_ticks`` is the first video frame's pts minus the file's
    ``start_time``, in ``time_base`` ticks (0 for every ffmpeg-written mp4).
    ``width``/``height`` decide the reversed intermediate's segment length.
    """
    rate: Fraction
    time_base: Fraction
    frames: int
    start_ticks: int = 0
    width: int = 1920
    height: int = 1080
    #: The master's real pts table (ticks, presentation order, relative to
    #: the file start) when it is NOT constant-rate. Python-only: the client
    #: models CFR, and the structural check (R14) BAKEs any disagreement.
    pts_table: tuple[int, ...] | None = None

    @property
    def frame_ticks(self) -> Fraction:
        return 1 / (self.rate * self.time_base)

    @functools.cached_property
    def frame_ticks_ratio(self) -> tuple[int, int]:
        """``frame_ticks`` as ``(num, den)`` integers (computed once)."""
        num = self.time_base.denominator * self.rate.denominator
        den = self.time_base.numerator * self.rate.numerator
        g = math.gcd(num, den)
        return num // g, den // g

    def pts(self, i: int) -> int:
        """pts (ticks) of decoded frame ``i`` — CFR arithmetic, or the real
        table; frames past the end are extrapolated (the EOF timestamp is the
        next frame's)."""
        t = self.pts_table
        if t:
            if 0 <= i < len(t):
                return t[i]
            step = t[-1] - t[-2] if len(t) > 1 else round_half_away(self.frame_ticks)
            return t[-1] + (i - len(t) + 1) * step
        num, den = self.frame_ticks_ratio
        if den == 1:
            return self.start_ticks + i * num
        return self.start_ticks + _rescale_int(i, num, den)

    @classmethod
    def from_proxy(cls, info) -> "SourceInfo":
        """From ``ingest.proxy.SourceInfo`` (its pts table and stream facts).

        The file's ``start_time`` is the earliest stream start, so when the
        sound starts before the picture (``audio_start < 0``) every video pts
        sits that much after the file start — which is what the demuxer's
        seek shift is measured from. A table that is not an arithmetic
        progression is kept verbatim (``pts_table``). ``width``/``height``
        are the proxy probe's DISPLAY size; the reversed intermediate's
        segment length reads the coded size (they differ only for
        non-square-pixel sources)."""
        tb_ = Fraction(info.time_base)
        pts = [int(p) for p in info.pts]
        lead = max(0.0, -float(getattr(info, "audio_start", 0.0) or 0.0))
        base = round_half_away(Fraction(lead) / tb_)
        rel = [p - pts[0] + base for p in pts] if pts else []
        cfr_step = 1 / (Fraction(info.rate) * tb_)
        exact = cfr_step.denominator == 1 and all(
            rel[i] == base + i * cfr_step.numerator for i in range(len(rel)))
        return cls(rate=Fraction(info.rate), time_base=tb_, frames=len(pts), start_ticks=base,
                   width=int(info.width), height=int(info.height),
                   pts_table=None if exact else tuple(rel))

    @classmethod
    def cfr(cls, rate, frames: int, *, time_base=None, start_ticks: int = 0,
            width: int = 1920, height: int = 1080) -> "SourceInfo":
        r = _tb.rate_of(rate) if not isinstance(rate, Fraction) else rate
        tb = Fraction(time_base) if time_base is not None else default_time_base(r)
        return cls(rate=r, time_base=tb, frames=int(frames), start_ticks=int(start_ticks),
                   width=int(width), height=int(height))

    def to_json(self) -> dict:
        return {"rate": [self.rate.numerator, self.rate.denominator],
                "tb": [self.time_base.numerator, self.time_base.denominator],
                "frames": self.frames, "start_ticks": self.start_ticks,
                "w": self.width, "h": self.height}

    @classmethod
    def from_json(cls, d: Mapping) -> "SourceInfo":
        return cls(rate=Fraction(*d["rate"]), time_base=Fraction(*d["tb"]),
                   frames=int(d["frames"]), start_ticks=int(d.get("start_ticks", 0)),
                   width=int(d.get("w", 1920)), height=int(d.get("h", 1080)))


SourceLookup = Callable[[str], SourceInfo]


# ---------------------------------------------------------------- selection

def _speed_divisor(speed) -> float | None:
    """The double ``setpts=PTS/{speed}`` divides by, or None when the chain
    has no CONSTANT retime (1x, unset, <= 0, or a curve dict — a curve is
    ``speed_curve_map``'s own setpts)."""
    if isinstance(speed, bool) or not isinstance(speed, (int, float)):
        return None
    if not speed or speed <= 0 or speed == 1.0:
        return None
    return float(speed)


def curve_retimer(curve: "_sc.CurveMap", time_base: Fraction) -> Callable[[int], int]:
    """The ``setpts`` of a speed curve on a stream in ``time_base``: rebased
    input ticks → output ticks, as ffmpeg computes it — ``T = PTS·TB`` in
    double (``TB = av_q2d(tb)``), the curve expression, ``/TB``, truncated
    (``D2TS``)."""
    TB = time_base.numerator / time_base.denominator

    def retime(x: int) -> int:
        return int(_sc.out_seconds(curve, float(x) * TB) / TB)
    return retime


def select_frames(src: SourceInfo, *, seek_us: int | None, dur_us: int,
                  n: int, fps, speed=None, curve: "_sc.CurveMap | None" = None,
                  anchor: tuple[float, float] | None = None) -> list[int]:
    """Source frame index shown in each of ``n`` output slots of one clip
    chain: ``[-ss seek] -t/-to … setpts=PTS-STARTPTS[,setpts=PTS/speed],
    fps=R,tpad=stop_mode=clone,trim=end_frame=n``.

    ``seek_us`` is the parsed ``-ss`` (None when the chain has no ``-ss``);
    ``dur_us`` is the recording time (``-t``, or ``-to`` minus ``-ss``).
    ``curve`` (a speed curve laid over the clip) replaces the constant
    ``speed`` retime with the curve's ``setpts``. With ``anchor = (seek,
    in)`` the curve runs on the v1 chain's IN-ANCHORED clock
    (``speed_curve.file_clock_expr``, ``curve_settb_expr``,
    ``anchored_setpts_expr``, then ``fps=R:start_time=0``): T is the
    frame's pts on the file clock times TB minus ``in``, and the output grid
    starts at T = 0, so the slots before it are dropped and slot 0 shows the
    latest frame rounding to <= 0."""
    if n <= 0:
        return []
    tb = src.time_base
    r = _tb.rate_of(fps)
    last = src.frames - 1
    if last < 0:
        return [0] * n
    # Accurate seek: timestamps shift by -seek (rounded into the stream tb)
    # and the inserted trim keeps pts >= 0.
    off = 0
    if seek_us is not None:
        off = _rescale_int(-seek_us, tb.denominator, 1_000_000 * tb.numerator)
        f0 = _first_at_or_after(src, -off)
    else:
        f0 = 0
    if f0 > last:
        # Seeked past the end: ffmpeg emits nothing and tpad has nothing to
        # clone. The compositor never builds such a chain from a valid EDL
        # (out is clamped to the source); hold the last frame.
        return [last] * n
    base = src.pts(f0)
    dur_tb = _rescale_int(dur_us, tb.denominator, 1_000_000 * tb.numerator)
    # Trim duration: frames with pts - first_kept >= duration end the stream
    # (a CFR pts rises with the index, so the first such frame bounds it).
    if src.pts_table:
        qlast = 0
        while f0 + qlast + 1 <= last and src.pts(f0 + qlast + 1) - base < dur_tb:
            qlast += 1
    else:
        qlast = max(0, min(last, _first_at_or_after(src, base + dur_tb) - 1) - f0)
    # One slot per kept frame, plus the frame after the last (its slot is
    # the EOF the fps filter flushes to): `_clip_ticks` is the chain's
    # retime, vectorised with the same double operations.
    ticks, out_tb_num, out_tb_den = _clip_ticks(src, f0, qlast + 2, base=base, off=off,
                                                seek_us=seek_us, r=r, speed=speed,
                                                curve=curve, anchor=anchor)
    # fps=R: each pts rounds (half away from zero) into an output slot.
    slots = _rescale_vec(ticks, out_tb_num, out_tb_den, monotone=curve is None)
    eof = int(slots[-1])
    slots = slots[:-1]
    # Slot s shows the latest frame whose slot is <= min(s, eof - 1), else
    # the first. Slots never decrease (pts rise, and a constant retime, the
    # truncation and the rounding are monotone; a curve's doubles are
    # checked), so that is a count of the slots[1..] at or under the limit.
    if slots.dtype == object or (curve is not None and slots.size > 1
                                 and not bool((slots[1:] >= slots[:-1]).all())):
        return _select_scan([int(v) for v in slots], eof, f0, qlast, n)
    lims = np.arange(n, dtype=np.int64)
    if eof - 1 < n - 1:
        lims = np.minimum(lims, eof - 1)
    q = slots[1:].searchsorted(lims, side="right")
    q += f0
    return q.tolist()


def _pts_vec(src: SourceInfo, i0: int, count: int) -> np.ndarray:
    """``src.pts(i)`` for ``i`` in ``[i0, i0 + count)``, as int64."""
    num, den = src.frame_ticks_ratio
    if src.pts_table or 2 * (abs(src.start_ticks) + (i0 + count) * num) + den >= _I64_SAFE:
        vals = [src.pts(i) for i in range(i0, i0 + count)]
        big = any(abs(v) >= _I64_SAFE for v in vals)
        return np.array(vals, dtype=object if big else np.int64)
    idx = np.arange(i0, i0 + count, dtype=np.int64)
    if den == 1:
        return src.start_ticks + idx * num
    return src.start_ticks + (2 * idx * num + den) // (2 * den)


def _clip_ticks(src: SourceInfo, f0: int, count: int, *, base: int, off: int,
                seek_us: int | None, r: Fraction, speed, curve, anchor
                ) -> tuple[np.ndarray, int, int]:
    """Output ticks (after the chain's retime) of source frames ``f0 …
    f0+count-1``, and the ``(num, den)`` that rescale those ticks into
    project slots (``av_rescale_q`` into ``1/R``)."""
    tb = src.time_base
    pts = _pts_vec(src, f0, count)
    if curve is not None and anchor is not None:
        TB = tb.numerator / tb.denominator
        a_seek, a_in = float(anchor[0]), float(anchor[1])
        # graph pts = file pts + off (the demuxer's shift); the chain adds
        # round(seek/TB) back — the same integer for a 1/n time base — then
        # settb multiplies by k, exactly.
        back = (off if seek_us is not None else 0) + (
            int(_sc.c_round(a_seek / TB)) if a_seek > 0 else 0)
        ctb = _sc.curve_time_base(tb, r.numerator)
        k = int(tb / ctb)
        TBc = ctb.numerator / ctb.denominator
        pf = (pts + back) * k
        T = pf.astype(np.float64) * TBc - a_in
        ticks = _trunc_i64((_out_seconds_vec(curve, T) + _sc.CURVE_TICK_BIAS) / TBc)
        tb = ctb
    elif curve is not None:
        TB = tb.numerator / tb.denominator
        T = (pts - base).astype(np.float64) * TB
        ticks = _trunc_i64(_out_seconds_vec(curve, T) / TB)
    else:
        div = _speed_divisor(speed)
        x = pts - base
        ticks = _trunc_i64(x.astype(np.float64) / div) if div is not None else x
    # a · tb / (1/R) = a · tb.num · R.num / (tb.den · R.den)
    return ticks, tb.numerator * r.numerator, tb.denominator * r.denominator


def _first_at_or_after(src: SourceInfo, ticks: int) -> int:
    """Smallest frame index whose pts >= ``ticks``."""
    num, den = src.frame_ticks_ratio
    i = max(0, ((ticks - src.start_ticks) * den) // num - 1)
    while src.pts(i) < ticks:
        i += 1
    while i > 0 and src.pts(i - 1) >= ticks:
        i -= 1
    return i


def speed_curve_map(speed, in_: float, out: float) -> "_sc.CurveMap | None":
    """The curve a clip chain retimes by (``compositor._curve_setpts``):
    the speed's points laid over ``out - in`` source seconds; None for a
    constant speed."""
    pts = _sc.curve_points(speed)
    if pts is None:
        return None
    return _sc.curve_map(pts, max(0.0, float(out) - float(in_)))


def forward_clip_frames(src: SourceInfo, *, in_: float, out: float, speed,
                        n: int, fps) -> list[int]:
    """``_build_clip_video_chain`` opened with ``clip_input_args`` (a speed
    CURVE: the in-anchored chain, seeking at ``speed_curve.curve_seek``)."""
    curve = speed_curve_map(speed, in_, out)
    if curve is not None:
        seek = _sc.curve_seek(in_)
    else:
        pre = _tb.seek_preroll(in_, fps)
        seek = max(0.0, float(in_) - pre)
    end = float(out) + _DECODE_SLACK_FRAMES * _tb.frame_duration(fps)
    seek_us = ffmpeg_us(seek) if seek > 0 else None
    dur_us = ffmpeg_us(end) - (seek_us or 0)
    return select_frames(src, seek_us=seek_us, dur_us=dur_us, n=n, fps=fps, speed=speed,
                         curve=curve, anchor=(seek, float(in_)) if curve is not None else None)


def freeze_frame(src: SourceInfo, *, in_: float, fps) -> int:
    """The source frame a FREEZE clip holds (``Clip.freeze``): the first
    output frame of a 1x chain opened at ``in`` (``compositor.
    freeze_input_span`` — the same seek, a decode of a few frames)."""
    from .compositor import freeze_input_span
    seek, end = freeze_input_span(in_, fps)
    seek_us = ffmpeg_us(seek) if seek > 0 else None
    dur_us = ffmpeg_us(end) - (seek_us or 0)
    return select_frames(src, seek_us=seek_us, dur_us=dur_us, n=1, fps=fps)[0]


def freeze_in_for(src: SourceInfo, frame: int, fps) -> float:
    """An ``in`` whose freeze holds source frame ``frame`` exactly: what a
    freeze-frame op writes (lane S2), so the still is the frame under the
    playhead. Tries the frame's own start, then nudges within it; every
    candidate is checked with ``freeze_frame``, never assumed. A frame no 1x
    chain ever shows (frame 0 of a source over twice the project rate: slot
    0 already holds frame 1) cannot be under a playhead either; its
    neighbour is held."""
    frame = max(0, min(int(frame), max(0, src.frames - 1)))
    base = float((src.pts(frame) - src.start_ticks) * src.time_base)
    fd = float(1 / src.rate)
    for frac in (0.0, 0.25, 0.5, 0.75, -0.25, 0.1, 0.9):
        t = max(0.0, base + frac * fd)
        t = round(t, 6)
        if freeze_frame(src, in_=t, fps=fps) == frame:
            return t
    return round(max(0.0, base), 6)


def reversed_frames(c: Clip, fps) -> int:
    """Frames the reversed intermediate holds (``reverse.reversed_frames``)."""
    return max(1, _tb.frame_of(float(c.out) - float(c.in_), fps))


def reverse_segment_frames(w: int, h: int, fps) -> int:
    from .reverse import _segment_frames
    return _segment_frames(w, h, fps)


def reversed_intermediate(src: SourceInfo, *, in_: float, out: float, fps) -> list[int]:
    """Source frame of each frame of the reversed intermediate
    (``render/reverse.py``): the clip's whole range on the project grid,
    decoded in segments (each with its own seek and ``-t``), each reversed,
    joined last-first."""
    m = max(1, _tb.frame_of(float(out) - float(in_), fps))
    seg = reverse_segment_frames(src.width, src.height, fps)
    forward: list[int] = []
    for j0 in range(0, m, seg):
        n = min(seg, m - j0)
        t0 = float(in_) + _tb.time_of(j0, fps)
        pre = _tb.seek_preroll(t0, fps)
        seek = max(0.0, t0 - pre)
        span = pre + _tb.time_of(n + _REV_SLACK_FRAMES, fps)
        seek_us = ffmpeg_us(seek) if seek > 0 else None
        forward += select_frames(src, seek_us=seek_us, dur_us=ffmpeg_us(span), n=n, fps=fps)
    return forward[::-1]


def intermediate_source(m: int, fps) -> SourceInfo:
    """The reversed intermediate as a source: the project rate, the muxer's
    default time base for it, ``m`` frames starting at 0."""
    r = _tb.rate_of(fps)
    return SourceInfo(rate=r, time_base=default_time_base(r), frames=m)


def clip_frame_list(c: Clip, src: SourceInfo, fps) -> list[int]:
    """Source frame for each clip-local output frame of v1 clip ``c`` (the
    ORIGINAL clip, reversed or not, a freeze or not).

    Memoised on exactly what decides it (the source's facts, in/out, speed,
    reverse, freeze and the rate): an edit re-maps only the clips it
    touched. A variable-rate source (a pts table, hashed element by element)
    is not memoised."""
    if src.pts_table:
        return _clip_frame_list(c, src, fps)
    key = (src, float(c.in_), float(c.out), _speed_key(c.speed), bool(getattr(c, "reverse", False)),
           getattr(c, "freeze", None), fps if isinstance(fps, (int, float, Fraction)) else str(fps))
    with _CLIP_FRAMES_LOCK:
        got = _CLIP_FRAMES.get(key)
    if got is None:
        got = tuple(_clip_frame_list(c, src, fps))
        _remember_clip_frames(key, got)
    return list(got)


#: `clip_frame_list` memo (insertion-ordered; the oldest entries go first),
#: bounded by the frames it holds (~36 bytes each), not by entries.
_CLIP_FRAMES: dict[tuple, tuple[int, ...]] = {}
_CLIP_FRAMES_MAX_FRAMES = 500_000
_CLIP_FRAMES_LOCK = threading.Lock()
_clip_frames_held = 0


def _remember_clip_frames(key: tuple, frames: tuple[int, ...]) -> None:
    global _clip_frames_held
    if len(frames) > _CLIP_FRAMES_MAX_FRAMES:
        return
    with _CLIP_FRAMES_LOCK:
        old = _CLIP_FRAMES.pop(key, None)
        if old is not None:
            _clip_frames_held -= len(old)
        while _CLIP_FRAMES and _clip_frames_held + len(frames) > _CLIP_FRAMES_MAX_FRAMES:
            _clip_frames_held -= len(_CLIP_FRAMES.pop(next(iter(_CLIP_FRAMES))))
        _CLIP_FRAMES[key] = frames
        _clip_frames_held += len(frames)


def clear_clip_frame_cache() -> None:
    """Forget every memoised clip frame list (tests, benchmarks)."""
    global _clip_frames_held
    with _CLIP_FRAMES_LOCK:
        _CLIP_FRAMES.clear()
        _clip_frames_held = 0


def _speed_key(speed):
    """A hashable form of a clip's ``speed`` (a curve's points; its display
    name does not change a frame)."""
    pts = _sc.curve_points(speed)
    if pts is not None:
        return ("curve", tuple(pts))
    return speed if not isinstance(speed, (dict, list)) else repr(speed)


def _clip_frame_list(c: Clip, src: SourceInfo, fps) -> list[int]:
    from .compositor import clip_frames
    if getattr(c, "freeze", None) is not None:
        return [freeze_frame(src, in_=c.in_, fps=fps)] * clip_frames(c, fps)
    if getattr(c, "reverse", False):
        inter = reversed_intermediate(src, in_=c.in_, out=c.out, fps=fps)
        view = _reversed_view(c, fps)
        n = clip_frames(view, fps)
        idx = forward_clip_frames(intermediate_source(len(inter), fps), in_=view.in_,
                                  out=view.out, speed=view.speed, n=n, fps=fps)
        return [inter[i] for i in idx]
    return forward_clip_frames(src, in_=c.in_, out=c.out, speed=c.speed,
                               n=clip_frames(c, fps), fps=fps)


def _reversed_view(c: Clip, fps) -> Clip:
    """``reverse.reversed_view`` without building the file: what the plan is
    computed from (in 0, out = the clip's own span, ``reverse.view_out``)."""
    from .reverse import view_out
    return c.model_copy(deep=True, update={
        "in_": 0.0, "out": view_out(c), "reverse": False})


# ---------------------------------------------------------------- the map

@dataclass
class ProgramMap:
    """Per-output-frame struct of arrays (appendix A of the spec)."""
    fps: Fraction
    total: int
    clips: list[Clip]                      # v1 clips in render order (original fields)
    kind: list[int] = field(default_factory=list)
    clip: list[int] = field(default_factory=list)      # index into clips, -1 gap
    frame: list[int] = field(default_factory=list)     # source frame, -1 gap
    b_clip: list[int] = field(default_factory=list)    # blend: incoming clip, else -1
    b_frame: list[int] = field(default_factory=list)
    p_num: list[int] = field(default_factory=list)     # blend progress = p_num/p_den
    p_den: list[int] = field(default_factory=list)     # (xfade P: 1 → 0; unreduced, den =
    #                                                    the seam's frames), 0 = none
    nested: list[bool] = field(default_factory=list)   # blend over a blend (3 clips)
    clip_start: list[int] = field(default_factory=list)  # per clip: first output frame
    clip_len: list[int] = field(default_factory=list)    # per clip: frames in its segment
    seams: list[dict] = field(default_factory=list)
    #: The v1 assembly: (kind, clip index or -1, frames, seconds the seam
    #: BEFORE this segment overlaps it — the acrossfade/xfade cost, else 0).
    segments: list[tuple[str, int, int, float]] = field(default_factory=list)

    def entry(self, k: int) -> dict:
        d = {"kind": self.kind[k], "clip": self.clip[k], "frame": self.frame[k]}
        if self.kind[k] == KIND_BLEND:
            d.update(b_clip=self.b_clip[k], b_frame=self.b_frame[k],
                     p=[self.p_num[k], self.p_den[k]], nested=self.nested[k])
        return d


def render_clips(edl: EDL) -> list[Clip]:
    """v1 media clips in the order the compositor assembles them."""
    from .compositor import _video_clips
    return _video_clips(edl)


def _plan_view(edl: EDL, fps) -> tuple[EDL, list[Clip], list[Clip]]:
    """(the EDL the compositor plans from, its v1 clips, the ORIGINAL v1
    clips in the same order). Reversed clips are swapped for their
    intermediate's timing exactly as ``with_reversed_sources`` does."""
    originals = render_clips(edl)
    if not any(getattr(c, "reverse", False) for c in originals):
        return edl, originals, originals
    view = edl.model_copy(deep=True)
    for t in view.tracks:
        t.clips = [_reversed_view(c, fps) if isinstance(c, Clip) and getattr(c, "reverse", False)
                   else c for c in t.clips]
    planned = render_clips(view)
    by_id = {c.id: c for c in originals}
    return view, planned, [by_id[c.id] for c in planned]


def build_program_map(edl: EDL, sources: SourceLookup | Mapping[str, SourceInfo],
                      fps=None) -> ProgramMap:
    """The program map of ``edl`` as the compositor renders it at ``fps``
    (default: the canvas rate — what ``render_preview`` and export use)."""
    from .compositor import _v1_frame_plan
    lookup = sources.__getitem__ if isinstance(sources, Mapping) else sources
    fps = edl.canvas.fps if fps is None else fps
    r = _tb.rate_of(fps)
    view, planned, originals = _plan_view(edl, fps)
    v1 = view.get_track("v1")
    transitions = list((v1.transitions if v1 else []) or [])
    seams = seam_table_for(planned, transitions, fps=fps) if transitions else []
    total_duration = max(0.0, view.duration + sum(d for _s, d in seams))
    if not planned:
        total_duration = max(1.0, total_duration)
    plan = _v1_frame_plan(planned, total_duration, fps)

    # Per-segment source frames (a gap: None).
    segs: list[tuple[int, list[int] | None, int]] = []     # (clip index or -1, frames, n)
    seg_of_clip: dict[int, int] = {}
    per_clip: dict[int, list[int]] = {}
    for kind, ci, nfr in plan:
        if kind == "clip":
            assert ci is not None
            c = originals[ci]
            frames = per_clip.get(ci)
            if frames is None:
                frames = clip_frame_list(c, lookup(c.src), fps)
                per_clip[ci] = frames
            if len(frames) < nfr:
                raise IndexError(f"clip {c.id}: {len(frames)} frames for a {nfr}-frame segment")
            seg_of_clip[ci] = len(segs)
            segs.append((ci, frames[:nfr], nfr))
        else:
            segs.append((-1, None, nfr))
    clip_of_seg = {si: ci for ci, si in seg_of_clip.items()}

    # Seam → (segment index of the left clip, cost in frames), as the
    # compositor decides it (adjacent segments, cost from the table, the
    # record only for the look).
    seg_trans: dict[int, int] = {}
    seg_cost: dict[int, float] = {}
    seam_rows: list[dict] = []
    for idx, c in enumerate(planned[:-1]):
        si = seg_of_clip.get(idx)
        if si is None or seg_of_clip.get(idx + 1) != si + 1:
            continue
        boundary = c.start + c.effective_duration
        cost = next((d for seam, d in seams if abs(seam - boundary) < 0.001), 0.0)
        record = seam_matching(transitions, boundary)
        if cost > 0.0 and record is not None:
            d = _tb.frame_of(cost, fps)
            seg_trans[si] = d
            seg_cost[si] = cost
            seam_rows.append({"left": idx, "right": idx + 1, "frames": d,
                              "type": record.type})

    # Fold segments exactly like the xfade/concat chain, straight into the
    # per-frame arrays: a seam of d frames turns the last d entries into
    # blends with the incoming segment's first d (an entry that already is a
    # blend keeps its outgoing leaf and becomes `nested`), the rest appends.
    pm = ProgramMap(fps=r, total=0, clips=originals, seams=seam_rows)
    kind_, clip_, frame_ = pm.kind, pm.clip, pm.frame
    b_clip_, b_frame_, p_num_, p_den_, nested_ = pm.b_clip, pm.b_frame, pm.p_num, pm.p_den, pm.nested
    starts: dict[int, int] = {}
    lens: dict[int, int] = {}
    for i, (ci, frames, nfr) in enumerate(segs):
        d = seg_trans.get(i - 1, 0) if i > 0 else 0
        seg_kind = KIND_CLIP if frames is not None else KIND_GAP
        seg_frames = frames if frames is not None else [-1] * nfr
        start_new = len(kind_)
        if d > 0:
            offset = max(0, len(kind_) - d)
            if offset + d > len(kind_) or d > nfr:
                raise IndexError(f"a {d}-frame seam over a shorter segment")
            for j in range(d):
                x = offset + j
                nested_[x] = kind_[x] == KIND_BLEND
                kind_[x] = KIND_BLEND
                b_clip_[x] = ci
                b_frame_[x] = seg_frames[j]
                # P = 1 − j/d, kept UNREDUCED (d = the seam's frames) so a
                # whole seam is one RLE run.
                p_num_[x] = d - j
                p_den_[x] = d
            start_new = offset
        m = nfr - d
        kind_.extend([seg_kind] * m)
        clip_.extend([ci] * m)
        frame_.extend(seg_frames[d:])
        b_clip_.extend([-1] * m)
        b_frame_.extend([-1] * m)
        p_num_.extend([0] * m)
        p_den_.extend([0] * m)
        nested_.extend([False] * m)
        cj = clip_of_seg.get(i)
        if cj is not None:
            starts[cj] = start_new
            lens[cj] = nfr
    pm.total = len(kind_)
    pm.clip_start = [starts.get(i, -1) for i in range(len(originals))]
    pm.clip_len = [lens.get(i, 0) for i in range(len(originals))]
    pm.segments = [(kind, ci if ci is not None else -1, nfr, seg_cost.get(si - 1, 0.0) if si else 0.0)
                   for si, (kind, ci, nfr) in enumerate(plan)]
    return pm


# ---------------------------------------------------------------- audio

@dataclass(frozen=True)
class AudioPlacement:
    """Where one v1 clip's sound lands (R9), at 48 kHz.

    The clip's sound occupies output samples ``[out0, out0 + n)``. ``runs``
    say which SOURCE sample (the source's own 48 kHz timeline, decoded from
    its start — the FLAC sidecar's index space) each output sample is:
    ``[offset, count, first, direction]`` → output ``out0 + offset + i`` is
    source ``first + direction·i`` (a reversed clip's intermediate is built
    in segments, so it is several descending runs). A source sample past the
    source's end is silence (``apad``). The first ``fade_in`` and last
    ``fade_out`` samples are mixed with the neighbour by ``acrossfade``.

    ``mode``: ``"exact"`` (1x), ``"reverse"`` (sample-exact reversed; a
    retimed reversed clip has no runs), ``"varispeed"`` (asetrate resample:
    output ``i`` is source ``src0 + i·rate`` within half a sample, no runs),
    ``"tempo"`` (atempo / WSOLA: approximate, same nominal mapping),
    ``"curve"`` (a speed curve: output ``i`` plays source ``src0 + 48000 ·
    speed_curve.source_seconds(i / 48000)``, resampled or time-stretched by
    ``render/speed_audio.py`` — no runs; ``rate`` is the mean speed),
    ``"silence"`` (a freeze: nothing; ``rate`` 0)."""
    clip: int
    out0: int
    n: int
    src0: int
    rate: float
    mode: str
    runs: tuple[tuple[int, int, int, int], ...] = ()
    fade_in: int = 0
    fade_out: int = 0


def _clip_sample0(t: float, fps) -> int:
    """First source sample a clip chain plays for edit point ``t``: the
    accurate seek to ``t − pre`` keeps samples from
    ``round(seek_us · 48 kHz)`` (the demuxer's timestamp shift, rounded into
    the 1/48000 stream time base), then ``atrim=start=pre`` drops
    ``round(pre_us · 48 kHz)`` more — two roundings, which is why this is not
    simply ``round(t · 48 kHz)`` (they differ by one sample about a third of
    the time; tests/test_audio_map_golden.py)."""
    pre = _tb.seek_preroll(t, fps)
    seek = max(0.0, float(t) - pre)
    j0 = _us_to_samples(ffmpeg_us(seek)) if seek > 0 else 0
    return j0 + (_us_to_samples(ffmpeg_us(pre)) if pre > 1e-9 else 0)


def _us_to_samples(us: int) -> int:
    """``rescale(us, 1/1e6, 1/48000)`` in integers."""
    return _rescale_int(us, _SAMPLE_RATE, 1_000_000)


#: ``timebase.samples_for_frames`` memoised on (frames, fps): a segment's
#: sound length, asked for per segment by the placement walk.
_samples_for_frames = functools.lru_cache(maxsize=4096)(_tb.samples_for_frames)


def _reversed_runs(c: Clip, src: SourceInfo | None, fps, n: int) -> tuple[tuple[int, int, int, int], ...]:
    """The reversed intermediate's sound (``reverse._render_segment``): per
    segment, ``samples_for_frames`` of the segment's frames from its own
    seek point, reversed; segments joined last-first."""
    m_frames = reversed_frames(c, fps)
    seg = reverse_segment_frames(src.width if src else 1920, src.height if src else 1080, fps)
    forward: list[tuple[int, int]] = []          # (first source sample, count) per segment
    for j0 in range(0, m_frames, seg):
        nf = min(seg, m_frames - j0)
        s0 = _tb.samples_for_frames(j0, fps)
        cnt = _tb.samples_for_frames(j0 + nf, fps) - s0
        t0 = float(c.in_) + _tb.time_of(j0, fps)
        forward.append((_clip_sample0(t0, fps), cnt))
    runs, off = [], 0
    for first, cnt in reversed(forward):
        take = min(cnt, n - off)
        if take <= 0:
            break
        runs.append((off, take, first + cnt - 1, -1))
        off += take
    return tuple(runs)


def audio_placements(edl: EDL, pm: ProgramMap, fps=None,
                     sources: SourceLookup | Mapping[str, SourceInfo] | None = None) -> list[AudioPlacement]:
    """Per-clip sound placement, walking the v1 assembly exactly as
    ``_assemble_v1_audio`` does: every segment (clip or gap filler) is
    ``samples_for_frames`` of ITS frames long (``apad``+``atrim=end_sample``
    per segment, so rounding accumulates per segment, not over the total),
    segments concatenate, and a seam's ``acrossfade`` overlaps the two by its
    cost in seconds, rounded to samples."""
    fps = edl.canvas.fps if fps is None else fps
    lookup = (sources.__getitem__ if isinstance(sources, Mapping) else sources) if sources else None
    out: list[AudioPlacement] = []
    cursor = 0
    last: int | None = None
    for kind, ci, nfr, cost in pm.segments:
        m = _samples_for_frames(nfr, fps)
        ov = _us_to_samples(ffmpeg_us(cost)) if cost > 0 else 0
        start = cursor - ov
        if ov and last is not None:
            out[last] = dataclasses.replace(out[last], fade_out=ov)
        if kind == "clip":
            c = pm.clips[ci]
            div = _speed_divisor(c.speed)
            rate = div or 1.0
            curve = _sc.curve_points(c.speed)
            if getattr(c, "freeze", None) is not None:
                out.append(AudioPlacement(clip=ci, out0=start, n=m, src0=_clip_sample0(c.in_, fps),
                                          rate=0.0, mode="silence", runs=(), fade_in=ov))
                last = len(out) - 1
                cursor = start + m
                continue
            if curve is not None:
                rate = _sc.mean_speed(curve)
            if getattr(c, "reverse", False):
                mode = "reverse"
                src = None
                if lookup is not None:
                    try:
                        src = lookup(c.src)
                    except KeyError:
                        src = None
                # A retimed reversed clip resamples the reversed intermediate:
                # no sample-exact runs (the client plays it approximately).
                runs = _reversed_runs(c, src, fps, m) if div is None and curve is None else ()
            elif curve is not None:
                mode, runs = "curve", ()
            else:
                mode = "exact" if div is None else (
                    "tempo" if getattr(c.audio, "keep_pitch", True) else "varispeed")
                # Retimed sound is a resample, not a sample copy: output i sits
                # at source src0 + i·rate (varispeed: within ±0.5 sample,
                # measured), so it carries no sample-exact runs.
                runs = ((0, m, _clip_sample0(c.in_, fps), 1),) if div is None else ()
            out.append(AudioPlacement(clip=ci, out0=start, n=m, src0=_clip_sample0(c.in_, fps),
                                      rate=rate, mode=mode, runs=runs, fade_in=ov))
            last = len(out) - 1
        else:
            last = None
        cursor = start + m
    return out


def audio_total_samples(edl: EDL, pm: ProgramMap, fps=None) -> int:
    """Length of the v1 sound in samples (the assembly's own sum)."""
    fps = edl.canvas.fps if fps is None else fps
    cursor = 0
    for _kind, _ci, nfr, cost in pm.segments:
        ov = _us_to_samples(ffmpeg_us(cost)) if cost > 0 else 0
        cursor += _samples_for_frames(nfr, fps) - ov
    return cursor


# ---------------------------------------------------------------- RLE

def _decode_seq(d: Mapping, n: int) -> list[int]:
    if "step" in d:
        return [d["f0"] + d["step"] * i for i in range(n)]
    deltas = _decode_varints(d["d"])
    out = [d["f0"]]
    for x in deltas[: n - 1]:
        out.append(out[-1] + x)
    return out


def to_rle(pm: ProgramMap, src_key: Callable[[Clip], str] | None = None) -> list[dict]:
    """Runs ``{k0, n, kind, clip_id, src, a, [b_clip_id, b_src, b, p_den, p_j0, nested]}``
    of maximal stretches with the same kind and clip(s); ``a``/``b`` are
    encoded frame sequences (``_encode_seq_np``: ``{f0, step}`` when
    arithmetic, else ``{f0, d}``, base64 zigzag varints of the deltas). Blend progress of frame
    ``k0+i`` is ``1 - (p_j0 + i)/p_den``.

    A run breaks where kind, clip, incoming clip or nesting change, and
    inside a blend where the seam (``p_den``) changes or the progress
    numerator does not step down by one — found with array compares, then
    each run's frames encoded at once (the output is the per-frame scan's,
    byte for byte: ``tests/test_frame_map_perf.py``)."""
    key = src_key or (lambda c: c.src)
    total = pm.total
    if total <= 0:
        return []
    kind = _i64(pm.kind)
    clip = _i64(pm.clip)
    brk = (kind[1:] != kind[:-1]) | (clip[1:] != clip[:-1])
    blends = KIND_BLEND in pm.kind
    if blends:
        # Outside a blend b_clip/nested/p are constant (-1/False/0).
        bcl, pden, pnum = _i64(pm.b_clip), _i64(pm.p_den), _i64(pm.p_num)
        nest = _i8(pm.nested)
        brk |= ((bcl[1:] != bcl[:-1]) | (nest[1:] != nest[:-1])
                | ((kind[1:] == KIND_BLEND)
                   & ((pden[1:] != pden[:-1]) | (pnum[1:] != pnum[:-1] - 1))))
    edges = [0] + (np.flatnonzero(brk) + 1).tolist() + [total]
    frame = _i64(pm.frame)
    b_frame = _i64(pm.b_frame) if blends else None
    # A run [k, e) is arithmetic iff its deltas d[k..e-2] never change: a
    # prefix count of the positions where the delta changes answers that per
    # run in O(1) (``_arith``).
    fa = _ArithIndex(frame)
    fb = _ArithIndex(b_frame) if blends else None
    clips, pk, pc, pbc, pden_l, pnum_l, pnest = (pm.clips, pm.kind, pm.clip, pm.b_clip,
                                                 pm.p_den, pm.p_num, pm.nested)
    runs: list[dict] = []
    for k, e in zip(edges[:-1], edges[1:]):
        kd = pk[k]
        run: dict = {"k0": k, "n": e - k, "kind": kd}
        if kd != KIND_GAP:
            c = clips[pc[k]]
            run.update(clip_id=c.id, src=key(c), a=fa.encode(k, e))
        if kd == KIND_BLEND:
            b = clips[pbc[k]]
            den = pden_l[k]
            j0 = den - pnum_l[k] if den else 0
            run.update(b_clip_id=b.id, b_src=key(b), b=fb.encode(k, e),
                       p_den=den, p_j0=j0, nested=pnest[k])
        runs.append(run)
    return runs


def rle_frames(runs: Sequence[Mapping]) -> list[dict]:
    """Expand runs back to one dict per output frame (tests, divergence)."""
    out: list[dict] = []
    for run in runs:
        n = run["n"]
        a = _decode_seq(run["a"], n) if "a" in run else [-1] * n
        b = _decode_seq(run["b"], n) if "b" in run else None
        for i in range(n):
            d = {"kind": run["kind"], "clip_id": run.get("clip_id"), "src": run.get("src"),
                 "frame": a[i]}
            if b is not None:
                j = run["p_j0"] + i
                d.update(b_clip_id=run["b_clip_id"], b_src=run["b_src"], b_frame=b[i],
                         p=[run["p_den"] - j, run["p_den"]], nested=run["nested"])
            out.append(d)
    return out


def frame_map_json(edl: EDL, sources: SourceLookup | Mapping[str, SourceInfo], *,
                   src_key: Callable[[Clip], str] | None = None) -> dict:
    """The ``GET /frame_map?h=`` body (spec §8): the RLE program map for the
    EDL's ``render_hash`` plus the per-clip sound placement."""
    pm = build_program_map(edl, sources)
    r = pm.fps
    key = src_key or (lambda c: c.src)
    return {
        "version": FRAME_MAP_VERSION,
        "render_hash": edl.render_hash(),
        "R": [r.numerator, r.denominator],
        "T": ticks_per_frame(r),
        "total": pm.total,
        "runs": to_rle(pm, key),
        "seams": pm.seams,
        "audio": [{"clip_id": pm.clips[a.clip].id, "src": key(pm.clips[a.clip]),
                   "out0": a.out0, "n": a.n, "src0": a.src0, "rate": a.rate,
                   "mode": a.mode, "runs": [list(r) for r in a.runs],
                   "fade_in": a.fade_in, "fade_out": a.fade_out}
                  for a in audio_placements(edl, pm, sources=sources)],
        "audio_total": audio_total_samples(edl, pm),
    }


__all__ = [
    "MSE_TIMESCALE", "FRAME_MAP_VERSION", "KIND_CLIP", "KIND_GAP", "KIND_BLEND",
    "SourceInfo", "ProgramMap", "AudioPlacement",
    "round_half_away", "rescale", "ffmpeg_us", "ticks_per_frame", "default_time_base",
    "select_frames", "forward_clip_frames", "reversed_intermediate", "clip_frame_list",
    "curve_retimer", "speed_curve_map", "freeze_frame", "freeze_in_for",
    "build_program_map", "audio_placements", "audio_total_samples", "to_rle", "rle_frames",
    "frame_map_json",
]
