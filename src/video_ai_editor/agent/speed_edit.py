"""Cutting a RETIMED clip in two (lane S2): the pure arithmetic `split_at`,
`cut_range` and `freeze_frame` need for a speed-curve clip and a freeze.

Until wave D a clip's speed was one number, so every cut converted timeline
seconds to source seconds with `speed_factor` (a multiply). A CURVE clip's
source position is the curve's integral (`Clip.source_offset_at`), and each
piece must keep ITS part of the shape, re-normalised over its own span
(`speed_curve.split_curve`), or the halves play a different ramp than the
whole did. A FREEZE consumes no source at all: its pieces are two holds of
the same frame whose lengths add up.

Nothing here touches the store: `dispatch` quantises the cut, clones the
clip (`_clone_clip`) and ASSIGNS the speed values returned here, so the
model's validator normalises them (a `model_copy(update=…)` would not).
"""
from __future__ import annotations

from ..edl import speed_curve as _sc
from ..edl import speed_presets as _sp
from ..edl.schema import Clip


def is_retimed(c: Clip) -> bool:
    """Anything but a plain 1x clip: a constant speed != 1, a curve (whatever
    its mean) or a freeze. The audio lanes and `detach_audio` need a 1x clip;
    a curve whose MEAN is exactly 1 is still not one."""
    if getattr(c, "freeze", None) is not None or c.speed_curve is not None:
        return True
    return abs(c.speed_factor - 1.0) > 1e-9


def source_offset(c: Clip, local_t: float) -> float:
    """Source seconds past `in` shown at clip-local timeline seconds
    `local_t` (the curve's integral; 0 for a freeze; `t · speed` else)."""
    return c.source_offset_at(max(0.0, float(local_t)))


def source_cut(c: Clip, local_t: float, snap) -> float:
    """The SOURCE time a cut at clip-local timeline seconds `local_t` lands
    on. A constant speed snaps it to the project grid with `snap` (QA-002,
    `dispatch._q` — unchanged behaviour). A CURVE keeps the exact integral,
    to the last bit: snapping it would move the left piece's footprint off
    `local_t` by up to a frame · mean speed, and the two pieces would then
    occupy one frame more (or less) than the whole — measured on a 25 fps
    Montage split at 1.7 s: 145 frames for 144. It is not rounded to the
    microsecond either (it was, "to what ffmpeg prints"): the in-anchored
    chain prints `in` in full (`speed_curve.anchored_setpts_expr`), and a
    right piece whose `in` is off by half a µs plays its curve up to 5 µs
    off its parent's — enough to flip a frame sitting on a rounding tie."""
    if c.speed_curve is not None:
        return float(c.in_) + source_offset(c, local_t)
    # Review RE: the TIMELINE offset is quantised, not the absolute source
    # time — `in + speed · q(local_t)`. Snapping `in + speed · local_t` on the
    # source's absolute clock left the left piece 0.02 s short when `in` is
    # off the project grid (25 fps, in 0.5, split at 1.2: pieces 0.5-1.68 and
    # 1.7…, 100 frames became 101). The pieces now cover exactly the frames
    # the whole clip did (the chain is in-anchored, compositor.v1_const_
    # speed). A result within float noise of the grid is stored ON it, so a
    # 1x clip whose `in` is on the grid keeps every value quantised.
    v = float(c.in_) + source_offset(c, snap(float(local_t)))
    q = snap(v)
    return q if abs(q - v) < 1e-9 else v


def cut_point(c: Clip, local_t: float, snap) -> float:
    """The SOURCE time a cut at clip-local timeline seconds `local_t` lands
    on, for a clip that may play BACKWARDS (review RD3). A reversed clip
    shows source `out - offset` at offset `local_t` (`render/reverse.py`:
    its intermediate's frame j is the range's frame M-1-j), so the cut is
    mirrored from `out`; the curve still lives on the OUTPUT clock, so the
    offset is the same integral a forward clip uses. A reversed constant
    speed snaps the absolute cut (its intermediate is built on the range's own
    grid), a curve keeps the exact value."""
    if not getattr(c, "reverse", False):
        return source_cut(c, local_t, snap)
    v = float(c.out) - source_offset(c, local_t)
    return v if c.speed_curve is not None else snap(v)


def piece_range(c: Clip, t0: float, t1: float, snap) -> tuple[float, float]:
    """`(in, out)` of the piece of `c` that plays clip-local timeline seconds
    [t0, t1) — the part a split / cut_range / trim keeps. Forward: from `in`
    upwards. REVERSED: the piece that plays first holds the source's END
    (left `[cut, out]`, right `[in, cut]`), or the edited clip plays its
    source out of order (measured: a split at 40 % changed 180/180 frames).
    An edge the piece shares with the whole clip keeps the clip's value."""
    D = c.effective_duration
    lo = t0 <= 1e-12
    hi = t1 >= D - 1e-12
    if getattr(c, "reverse", False):
        return (float(c.in_) if hi else cut_point(c, t1, snap),
                float(c.out) if lo else cut_point(c, t0, snap))
    return (float(c.in_) if lo else cut_point(c, t0, snap),
            float(c.out) if hi else cut_point(c, t1, snap))


def _piece_points(points: list[tuple[float, float]], a: float, b: float) -> list[list[float]]:
    """The part of a curve over OUTPUT positions [a, b] (0 <= a < b <= 1),
    re-normalised to 0..1."""
    pts = [[float(x), float(r)] for x, r in points]
    if a > 0.0:
        pts = _sc.split_curve([(x, r) for x, r in pts], a)[1]
        b = (b - a) / (1.0 - a)
    if b < 1.0:
        pts = _sc.split_curve([(x, r) for x, r in pts], b)[0]
    return pts


def piece_speed(c: Clip, t0: float, t1: float):
    """The speed value of the piece of `c` that covers clip-local timeline
    seconds [t0, t1): the same number for a constant speed, the matching part
    of the curve (named "custom" unless it is still exactly a preset)."""
    pts = c.speed_curve
    if pts is None:
        return c.speed
    D = c.effective_duration
    if not D > 0:
        return c.speed
    a = min(1.0, max(0.0, t0 / D))
    b = min(1.0, max(0.0, t1 / D))
    if b <= a:
        return c.speed
    if a <= 0.0 and b >= 1.0:
        return c.speed
    return _sp.curve_speed(_piece_points(pts, a, b))


def freeze_piece(c: Clip, t0: float, t1: float) -> float | None:
    """A freeze piece's hold (timeline seconds), or None for a normal clip."""
    if getattr(c, "freeze", None) is None:
        return None
    return max(0.0, min(float(c.freeze), t1) - max(0.0, t0))


__all__ = ["is_retimed", "source_offset", "source_cut", "cut_point", "piece_range", "piece_speed",
           "freeze_piece"]
