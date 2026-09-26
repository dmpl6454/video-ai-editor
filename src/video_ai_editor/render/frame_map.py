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

WHAT IT DOES NOT DO. Pixels (geometry, colour, effects) are not modelled —
only frame SELECTION. The plan functions (``_v1_frame_plan``,
``clip_frames``, ``seam_table_for``) are the compositor's own, imported, not
copied.
"""
from __future__ import annotations

import base64
import dataclasses
import math
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Callable, Iterable, Mapping, Sequence

from ..edl import timebase as _tb
from ..edl.schema import EDL, Clip, seam_matching, seam_table_for

#: MSE timescale the client writes its fragments in (spec §3.1): an integer
#: number of ticks per frame for every standard rate.
MSE_TIMESCALE = 240000

#: Frame-map format version. Bump when the JSON shape or a selection rule
#: changes (the goldens record it).
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
        f = self.frame_ticks
        if f.denominator == 1:
            return self.start_ticks + i * f.numerator
        return self.start_ticks + round_half_away(i * f)

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
    has no retime (1x, unset, <= 0, or a curve dict — curves render at 1x;
    ``Clip.speed_factor``)."""
    if isinstance(speed, bool) or not isinstance(speed, (int, float)):
        return None
    if not speed or speed <= 0 or speed == 1.0:
        return None
    return float(speed)


def select_frames(src: SourceInfo, *, seek_us: int | None, dur_us: int,
                  n: int, fps, speed=None) -> list[int]:
    """Source frame index shown in each of ``n`` output slots of one clip
    chain: ``[-ss seek] -t/-to … setpts=PTS-STARTPTS[,setpts=PTS/speed],
    fps=R,tpad=stop_mode=clone,trim=end_frame=n``.

    ``seek_us`` is the parsed ``-ss`` (None when the chain has no ``-ss``);
    ``dur_us`` is the recording time (``-t``, or ``-to`` minus ``-ss``)."""
    if n <= 0:
        return []
    tb = src.time_base
    r = _tb.rate_of(fps)
    out_tb = 1 / r
    last = src.frames - 1
    if last < 0:
        return [0] * n
    # Accurate seek: timestamps shift by -seek (rounded into the stream tb)
    # and the inserted trim keeps pts >= 0.
    if seek_us is not None:
        off = rescale(-seek_us, _US, tb)
        f0 = _first_at_or_after(src, -off)
    else:
        f0 = 0
    if f0 > last:
        # Seeked past the end: ffmpeg emits nothing and tpad has nothing to
        # clone. The compositor never builds such a chain from a valid EDL
        # (out is clamped to the source); hold the last frame.
        return [last] * n
    base = src.pts(f0)
    dur_tb = rescale(dur_us, _US, tb)
    # Trim duration: frames with pts - first_kept >= duration end the stream.
    qlast = 0
    while f0 + qlast + 1 <= last and src.pts(f0 + qlast + 1) - base < dur_tb:
        qlast += 1
    div = _speed_divisor(speed)

    def retime(x: int) -> int:
        return int(x / div) if div is not None else x

    slots = [rescale(retime(src.pts(f0 + q) - base), tb, out_tb) for q in range(qlast + 1)]
    eof = rescale(retime(src.pts(f0 + qlast + 1) - base), tb, out_tb)
    out: list[int] = []
    q = 0
    for s in range(n):
        lim = min(s, eof - 1)
        while q < qlast and slots[q + 1] <= lim:
            q += 1
        out.append(f0 + q)
    return out


def _first_at_or_after(src: SourceInfo, ticks: int) -> int:
    """Smallest frame index whose pts >= ``ticks``."""
    f = src.frame_ticks
    i = max(0, math.floor((ticks - src.start_ticks) / f) - 1)
    while src.pts(i) < ticks:
        i += 1
    while i > 0 and src.pts(i - 1) >= ticks:
        i -= 1
    return i


def forward_clip_frames(src: SourceInfo, *, in_: float, out: float, speed,
                        n: int, fps) -> list[int]:
    """``_build_clip_video_chain`` opened with ``clip_input_args``."""
    pre = _tb.seek_preroll(in_, fps)
    seek = max(0.0, float(in_) - pre)
    end = float(out) + _DECODE_SLACK_FRAMES * _tb.frame_duration(fps)
    seek_us = ffmpeg_us(seek) if seek > 0 else None
    dur_us = ffmpeg_us(end) - (seek_us or 0)
    return select_frames(src, seek_us=seek_us, dur_us=dur_us, n=n, fps=fps, speed=speed)


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
    ORIGINAL clip, reversed or not)."""
    from .compositor import clip_frames
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
    computed from (in 0, out = the intermediate's length)."""
    m = reversed_frames(c, fps)
    return c.model_copy(deep=True, update={
        "in_": 0.0, "out": _tb.time_of(m, fps), "reverse": False})


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


@dataclass(frozen=True)
class _Leaf:
    kind: int
    clip: int
    frame: int


@dataclass(frozen=True)
class _Blend:
    a: object
    b: _Leaf
    j: int
    d: int


def _leaf_a(e) -> _Leaf:
    while isinstance(e, _Blend):
        e = e.a
    return e


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

    # Per-segment leaves.
    segs: list[list] = []
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
            seg_of_clip[ci] = len(segs)
            segs.append([_Leaf(KIND_CLIP, ci, frames[j]) for j in range(nfr)])
        else:
            segs.append([_Leaf(KIND_GAP, -1, -1)] * nfr)

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

    # Fold segments exactly like the xfade/concat chain.
    starts: dict[int, int] = {}
    lens: dict[int, int] = {}
    cur: list = []
    for i, seg in enumerate(segs):
        d = seg_trans.get(i - 1, 0) if i > 0 else 0
        if d > 0:
            offset = max(0, len(cur) - d)
            blended = [_Blend(cur[offset + j], seg[j], j, d) for j in range(d)]
            start_new = offset
            cur = cur[:offset] + blended + seg[d:]
        else:
            start_new = len(cur)
            cur = cur + seg
        ci = next((c for c, s in seg_of_clip.items() if s == i), None)
        if ci is not None:
            starts[ci] = start_new
            lens[ci] = len(seg)

    pm = ProgramMap(fps=r, total=len(cur), clips=originals, seams=seam_rows)
    for e in cur:
        if isinstance(e, _Blend):
            a = _leaf_a(e.a)
            pm.kind.append(KIND_BLEND)
            pm.clip.append(a.clip)
            pm.frame.append(a.frame)
            pm.b_clip.append(e.b.clip)
            pm.b_frame.append(e.b.frame)
            # P = 1 − j/d, kept UNREDUCED (d = the seam's frames) so a whole
            # seam is one RLE run.
            pm.p_num.append(e.d - e.j)
            pm.p_den.append(e.d)
            pm.nested.append(isinstance(e.a, _Blend))
        else:
            pm.kind.append(e.kind)
            pm.clip.append(e.clip)
            pm.frame.append(e.frame)
            pm.b_clip.append(-1)
            pm.b_frame.append(-1)
            pm.p_num.append(0)
            pm.p_den.append(0)
            pm.nested.append(False)
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
    ``"tempo"`` (atempo / WSOLA: approximate, same nominal mapping)."""
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
    sr = Fraction(1, _SAMPLE_RATE)
    j0 = rescale(ffmpeg_us(seek), _US, sr) if seek > 0 else 0
    return j0 + (rescale(ffmpeg_us(pre), _US, sr) if pre > 1e-9 else 0)


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
        m = _tb.samples_for_frames(nfr, fps)
        ov = rescale(ffmpeg_us(cost), _US, Fraction(1, _SAMPLE_RATE)) if cost > 0 else 0
        start = cursor - ov
        if ov and last is not None:
            out[last] = dataclasses.replace(out[last], fade_out=ov)
        if kind == "clip":
            c = pm.clips[ci]
            div = _speed_divisor(c.speed)
            rate = div or 1.0
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
                runs = _reversed_runs(c, src, fps, m) if div is None else ()
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
        ov = rescale(ffmpeg_us(cost), _US, Fraction(1, _SAMPLE_RATE)) if cost > 0 else 0
        cursor += _tb.samples_for_frames(nfr, fps) - ov
    return cursor


# ---------------------------------------------------------------- RLE

def _zigzag(v: int) -> int:
    return (v << 1) if v >= 0 else ((-v << 1) - 1)


def _varints(vals: Iterable[int]) -> str:
    buf = bytearray()
    for v in vals:
        z = _zigzag(v)
        while True:
            b = z & 0x7F
            z >>= 7
            if z:
                buf.append(b | 0x80)
            else:
                buf.append(b)
                break
    return base64.b64encode(bytes(buf)).decode("ascii")


def _decode_varints(text: str) -> list[int]:
    out, z, shift = [], 0, 0
    for b in base64.b64decode(text):
        z |= (b & 0x7F) << shift
        if b & 0x80:
            shift += 7
            continue
        out.append((z >> 1) if not (z & 1) else -((z + 1) >> 1))
        z, shift = 0, 0
    return out


def _encode_seq(vals: Sequence[int]) -> dict:
    """A frame sequence as ``{"f0", "step"}`` when arithmetic with an integer
    step, else ``{"f0", "d"}`` — base64 zigzag varints of the deltas."""
    f0 = vals[0]
    if len(vals) == 1:
        return {"f0": f0, "step": 1}
    step = vals[1] - vals[0]
    if all(vals[i + 1] - vals[i] == step for i in range(len(vals) - 1)):
        return {"f0": f0, "step": step}
    return {"f0": f0, "d": _varints(vals[i + 1] - vals[i] for i in range(len(vals) - 1))}


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
    encoded frame sequences (``_encode_seq``). Blend progress of frame
    ``k0+i`` is ``1 - (p_j0 + i)/p_den``."""
    key = src_key or (lambda c: c.src)
    runs: list[dict] = []
    k = 0
    while k < pm.total:
        kind, ci, bi, nest = pm.kind[k], pm.clip[k], pm.b_clip[k], pm.nested[k]
        e = k + 1
        while e < pm.total and pm.kind[e] == kind and pm.clip[e] == ci \
                and pm.b_clip[e] == bi and pm.nested[e] == nest:
            if kind == KIND_BLEND and (pm.p_den[e] != pm.p_den[k]
                                       or pm.p_num[e] != pm.p_num[e - 1] - 1):
                break
            e += 1
        run: dict = {"k0": k, "n": e - k, "kind": kind}
        if kind != KIND_GAP:
            c = pm.clips[ci]
            run.update(clip_id=c.id, src=key(c), a=_encode_seq(pm.frame[k:e]))
        if kind == KIND_BLEND:
            b = pm.clips[bi]
            den = pm.p_den[k]
            j0 = den - pm.p_num[k] if den else 0
            run.update(b_clip_id=b.id, b_src=key(b), b=_encode_seq(pm.b_frame[k:e]),
                       p_den=den, p_j0=j0, nested=nest)
        runs.append(run)
        k = e
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
    "build_program_map", "audio_placements", "audio_total_samples", "to_rle", "rle_frames",
    "frame_map_json",
]
