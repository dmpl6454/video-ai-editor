"""Speed CURVES (CapCut "Curve" speed): the ONE definition of how a clip whose
``speed`` is ``{"curve": [[x, r], ...]}`` maps output time to source time.

THE MODEL. ``x`` is a position over the clip's OUTPUT (its span on the
timeline), normalised 0..1; ``r`` is the playback speed there (0.1-10x, the
CapCut range), and speed is piecewise LINEAR in ``x`` between the points.

Why output position, not source position:

* it is what an editor shows and what CapCut's curve editor is: the curve is
  drawn over the clip as it sits on the timeline, the playhead crosses it at a
  constant rate, and a point dragged left or right stays over the same place
  in the finished shot when the curve's shape changes the clip's length;
* the maths close with only +, -, ×, ÷ and ONE square root. Source seconds
  consumed by output second ``t`` is the curve's integral — a quadratic per
  piece — and the retime the compositor needs (output time AS A FUNCTION OF
  source time, which is what ``setpts`` computes) is that quadratic's root.
  IEEE 754 requires a correctly rounded ``sqrt`` in C, Python and JavaScript
  alike, so the ffmpeg expression, ``render/frame_map.py`` and the browser
  port (``frontend/src/lib/preview/timeline/speedCurve.ts``) compute the SAME
  double for every frame and truncate it the same way. A source-position
  curve would need ``log``/``exp``, which no standard pins to the last bit.

Footprint: source seconds ``S = out - in`` over output seconds ``D``:
``S = D · mean(r)``, so ``Clip.effective_duration = S / mean`` — the curve's
integral — and the clip occupies ``max(1, frame_of(D))`` frames exactly like a
constant speed (``compositor.clip_frames``, through ``edl/timebase``).

Retime (per piece ``i``, output seconds ``t_i``, source seconds ``s_i``,
speeds ``r_i`` → ``r_{i+1}``): for source seconds ``T`` in ``[s_i, s_{i+1})``
with ``q = T - s_i``::

    out(T) = t_i + 2·q / (r_i + sqrt(r_i² + k_i·q)),   k_i = 2·(r_{i+1} - r_i)/(t_{i+1} - t_i)

and past the end ``out(T) = D + (T - s_n)/r_n``. The compositor prints this
as ``setpts`` with every constant written in ``repr`` (so ffmpeg parses the
exact double), and the frame the export shows at output slot ``k`` is then
chosen by the SAME ``fps=R`` rule as a constant speed — which is why the
program map models a curve exactly (§6 R4).

Every function here is pure and deterministic; the float operations and
their order are part of the contract (the browser repeats them).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

#: CapCut's curve speed range.
CURVE_SPEED_RANGE = (0.1, 10.0)
#: A curve holds at most this many points (CapCut's editor shows ~5-10).
MAX_CURVE_POINTS = 32
#: A curve may carry a display name (a preset's), at most this long.
MAX_CURVE_NAME = 40

#: CapCut-LIKE presets (the shapes, not CapCut's exact numbers): the
#: frontend's curve menu and the tests use them. Each is a canonical point
#: list over output position.
CURVE_PRESETS: dict[str, list[list[float]]] = {
    "ramp_up": [[0.0, 0.5], [1.0, 3.0]],
    "ramp_down": [[0.0, 3.0], [1.0, 0.5]],
    "montage": [[0.0, 1.0], [0.25, 1.0], [0.35, 3.0], [0.5, 3.0], [0.6, 0.5],
                [0.75, 0.5], [0.85, 2.0], [1.0, 2.0]],
    "hero": [[0.0, 1.0], [0.3, 1.0], [0.42, 0.25], [0.58, 0.25], [0.7, 1.0], [1.0, 1.0]],
    "bullet": [[0.0, 3.0], [0.3, 3.0], [0.4, 0.3], [0.6, 0.3], [0.7, 3.0], [1.0, 3.0]],
    "jump_cut": [[0.0, 1.0], [0.4, 1.0], [0.45, 6.0], [0.55, 6.0], [0.6, 1.0], [1.0, 1.0]],
    "flash_in": [[0.0, 5.0], [0.3, 5.0], [0.5, 1.0], [1.0, 1.0]],
    "flash_out": [[0.0, 1.0], [0.5, 1.0], [0.7, 5.0], [1.0, 5.0]],
}


def _finite(v: Any, what: str) -> float:
    f = float(v)
    if not math.isfinite(f):
        raise ValueError(f"speed curve {what} must be a finite number, got {v!r}")
    return f


def normalize_curve(value: dict) -> dict | None:
    """The canonical form of a ``{"curve": [[x, r], ...]}`` speed, or None
    when the dict carries no usable curve (it then means 1x — which is what
    such a dict always rendered as).

    * ``x`` is clamped to [0, 1] and ``r`` to ``CURVE_SPEED_RANGE``; a
      non-finite value RAISES (the house rule: clamp out-of-range, never
      invent a value for NaN);
    * points are sorted by ``x``; of several at one ``x`` the LAST wins
      (a zero-width piece would divide by zero);
    * the ends are pinned: a curve that starts after 0 or ends before 1 is
      extended flat to them; a single point is a flat curve;
    * at most ``MAX_CURVE_POINTS`` points (extra points are dropped from the
      end, the last point is kept);
    * an optional ``name`` (a preset's) is kept, trimmed; every other key is
      dropped, so the render hash only moves with what renders.
    """
    raw = value.get("curve") if isinstance(value, dict) else None
    if not isinstance(raw, (list, tuple)):
        return None
    pts: dict[float, float] = {}
    order: list[float] = []
    for p in raw:
        if not isinstance(p, (list, tuple)) or len(p) < 2:
            continue
        if isinstance(p[0], bool) or isinstance(p[1], bool):
            continue
        try:
            x = _finite(p[0], "position")
            r = _finite(p[1], "speed")
        except (TypeError, ValueError) as e:
            if isinstance(e, ValueError) and "finite" in str(e):
                raise
            continue
        x = min(1.0, max(0.0, x))
        r = min(CURVE_SPEED_RANGE[1], max(CURVE_SPEED_RANGE[0], r))
        if x not in pts:
            order.append(x)
        pts[x] = r
    if not pts:
        return None
    xs = sorted(order)
    points = [[x, pts[x]] for x in xs]
    if points[0][0] > 0.0:
        points.insert(0, [0.0, points[0][1]])
    if points[-1][0] < 1.0:
        points.append([1.0, points[-1][1]])
    if len(points) > MAX_CURVE_POINTS:
        points = points[: MAX_CURVE_POINTS - 1] + [[1.0, points[-1][1]]]
    out: dict = {"curve": points}
    name = value.get("name")
    if isinstance(name, str) and name.strip():
        out["name"] = name.strip()[:MAX_CURVE_NAME]
    return out


def curve_points(speed: Any) -> list[tuple[float, float]] | None:
    """The points of a canonical curve speed, or None when ``speed`` is not
    a curve (a number, None, or a dict without one)."""
    if not isinstance(speed, dict):
        return None
    raw = speed.get("curve")
    if not isinstance(raw, (list, tuple)) or len(raw) < 2:
        return None
    return [(float(p[0]), float(p[1])) for p in raw]


def is_curve(speed: Any) -> bool:
    return curve_points(speed) is not None


def mean_speed(points: list[tuple[float, float]]) -> float:
    """The curve's mean speed over output position: ``Σ Δx · (r_a + r_b)/2``
    in point order (the order is part of the contract)."""
    m = 0.0
    for (x0, r0), (x1, r1) in zip(points, points[1:]):
        m = m + (x1 - x0) * (r0 + r1) / 2.0
    return m


@dataclass(frozen=True)
class CurveSeg:
    """One piece of a curve laid over a clip: source seconds ``[s0, s1)``
    map to output seconds from ``t0``, speed ``r`` at its start, ``rr = r·r``
    and ``k = 2·(r_next - r)/(t1 - t0)``."""
    s0: float
    s1: float
    t0: float
    t1: float
    r: float
    rr: float
    k: float
    r1: float


@dataclass(frozen=True)
class CurveMap:
    """A curve laid over ``S`` source seconds: output duration ``D`` and the
    pieces, plus the tail (past the last point: the last speed, linear)."""
    S: float
    D: float
    segs: tuple[CurveSeg, ...]
    s_end: float
    r_end: float


def curve_map(points: list[tuple[float, float]], S: float) -> CurveMap | None:
    """Lay ``points`` over a clip of ``S`` source seconds. None when there is
    nothing to retime (``S`` not positive)."""
    if not points or len(points) < 2 or not (S > 1e-9):
        return None
    m = mean_speed(points)
    D = S / m
    segs: list[CurveSeg] = []
    s = 0.0
    for (x0, r0), (x1, r1) in zip(points, points[1:]):
        t0 = D * x0
        t1 = D * x1
        if not (t1 > t0):
            continue
        s1 = s + (t1 - t0) * (r0 + r1) / 2.0
        k = 2.0 * (r1 - r0) / (t1 - t0)
        segs.append(CurveSeg(s0=s, s1=s1, t0=t0, t1=t1, r=r0, rr=r0 * r0, k=k, r1=r1))
        s = s1
    return CurveMap(S=S, D=D, segs=tuple(segs), s_end=s, r_end=points[-1][1])


def start_speed(cm: CurveMap) -> float:
    """The curve's speed at output 0 (the first piece's)."""
    return cm.segs[0].r if cm.segs else cm.r_end


def out_seconds(cm: CurveMap, T: float) -> float:
    """Output seconds at source seconds ``T`` — the ``setpts`` expression of
    ``setpts_expr`` evaluated with the same double operations in the same
    order (``render/frame_map.py`` truncates ``out / TB`` like ffmpeg's
    ``D2TS``). Before the clip's ``in`` (``T < 0``: the pre-roll frames an
    in-anchored chain decodes, see ``anchored_setpts_expr``) the curve is
    extended at its start speed — linear, so never the square root of a
    negative number."""
    if T < 0:
        return T / start_speed(cm)
    for g in cm.segs:
        if T < g.s1:
            q = T - g.s0
            return g.t0 + 2.0 * q / (g.r + math.sqrt(g.rr + g.k * q))
    return cm.D + (T - cm.s_end) / cm.r_end


def _num(x: float) -> str:
    """A double as ffmpeg's expression parser reads it back exactly (strtod
    of the shortest round-trip repr)."""
    s = repr(float(x))
    return f"({s})" if s.startswith("-") else s


def out_seconds_expr(cm: CurveMap, T: str) -> str:
    """``out_seconds`` as an ffmpeg expression of the source seconds ``T``
    (an expression string): nested ``if(lt(T,s1),E0,…)`` over the pieces,
    commas escaped for the filtergraph (``\\,``). ``setpts_expr`` is this at
    ``T = PTS·TB``; the compositor also evaluates keyframes at it (clip-local
    TIMELINE seconds of a source frame, review RD2)."""
    expr = f"({_num(cm.D)}+({T}-{_num(cm.s_end)})/{_num(cm.r_end)})"
    for g in reversed(cm.segs):
        q = f"({T}-{_num(g.s0)})"
        e = f"({_num(g.t0)}+2*{q}/({_num(g.r)}+sqrt({_num(g.rr)}+{_num(g.k)}*{q})))"
        expr = f"if(lt({T}\\,{_num(g.s1)})\\,{e}\\,{expr})"
    return f"if(lt({T}\\,0)\\,({T}/{_num(start_speed(cm))})\\,{expr})"


def setpts_expr(cm: CurveMap) -> str:
    """The ``setpts`` value (no ``setpts=``): ``out_seconds_expr`` at
    ``T = PTS·TB``, divided by ``TB`` into the stream's ticks."""
    return f"({out_seconds_expr(cm, '(PTS*TB)')})/TB"


# ---------------------------------------------------------------- the v1 chain's clock
#
# WHY. The v1 chain used to rebase a curve clip at its FIRST KEPT source
# frame (`setpts=PTS-STARTPTS`), so the curve's source clock started up to
# half a frame away from `in`, and it truncated the curve's output into the
# SOURCE's time base. At 1x neither shows, but a curve maps source time
# non-linearly: a split piece whose `in` falls mid-frame played its whole
# curve shifted by that sub-frame offset — measured on a Hero clip (20 s,
# 30 fps) split at 10 s: 103 of 759 later frames one source frame late, the
# cut frame held 1 output frame instead of 2. A split, a cut_range and a
# trim of a curve clip are now INVISIBLE (the pieces export the frames the
# whole clip did), because the chain measures the curve on clocks every
# piece of one clip shares:
#
#   * the input seek lands on a 1/5 s grid at least `CURVE_PREROLL_S` before
#     `in` (`curve_seek`): the frame just before `in` — which a slow curve
#     still shows in slot 0 — is always decoded, and the demuxer's timestamp
#     shift (the seek rounded into the stream time base) is never a rounding
#     tie for a 1/n time base (seek·n has a fractional part in fifths);
#   * the first `setpts` adds that shift back, `PTS + round(seek/TB)`
#     (`file_clock_expr`): every frame's pts is its pts on the FILE's clock,
#     whatever the seek;
#   * `settb` (`curve_settb_expr`) refines the time base by
#     k = R.num / gcd(R.num, n) — pts × k exactly — so a project frame is a
#     WHOLE number of ticks (1/15360 at 25 fps was 614.4 ticks a frame,
#     which put the truncation grid of a piece off its parent's);
#   * the curve's `setpts` takes T = PTS·TB − in (`anchored_setpts_expr`):
#     the same double for a given source frame in every piece of the clip,
#     minus `in`; the result gains `CURVE_TICK_BIAS` before the truncation
#     into ticks, so a value that is mathematically a whole tick (a 1x
#     stretch, an exact rounding tie of the fps filter) truncates to that
#     tick in every piece instead of by its last-ulp noise;
#   * `fps=R:start_time=0` anchors the output grid at T = 0 (`in`), not at
#     the first decoded frame: pre-roll slots before it are dropped, slot 0
#     shows the latest frame whose rounded slot is <= 0.
#
# `render/frame_map.py` and `speedCurve.ts` model these steps with the same
# double operations (`c_round` is C's `round`, half away from zero).

#: A curve clip's input seek lands at least this far before `in`: past the
#: previous source frame of any source above 2 fps.
CURVE_PREROLL_S = 0.5
#: ... on a grid of 1/CURVE_SEEK_GRID seconds (see the block above).
CURVE_SEEK_GRID = 5.0
#: Seconds added to a curve's output time before it is truncated into ticks
#: (see the block above): far above the double noise of a time under 6 h
#: (~1e-11 s), far below a tick (>= ~0.1 µs after `curve_settb_expr`).
CURVE_TICK_BIAS = 1e-8


def curve_seek(in_: float) -> float:
    """The input seek (seconds, 0 = no ``-ss``) of a curve clip at ``in_``:
    the latest multiple of 1/5 s at least ``CURVE_PREROLL_S`` before it."""
    k = math.floor((float(in_) - CURVE_PREROLL_S) * CURVE_SEEK_GRID)
    return k / CURVE_SEEK_GRID if k > 0 else 0.0


def c_round(x: float) -> float:
    """C's ``round()`` (ffmpeg's expression ``round``): nearest integer,
    halves away from zero, exact for every double."""
    a = abs(x)
    f = math.floor(a)
    r = f + 1.0 if a - f >= 0.5 else f
    return math.copysign(r, x)


def file_clock_expr(seek: float) -> str | None:
    """The chain's FIRST ``setpts`` value for a curve clip opened at
    ``seek``: the demuxer's shift added back (None without a seek — the
    pts already are on the file clock)."""
    return f"PTS+round({_num(seek)}/TB)" if seek > 0 else None


def curve_settb_expr(rate_num: int) -> str:
    """``settb`` value: the stream time base divided by
    ``rate_num / gcd(rate_num, 1/TB)`` (see the block above)."""
    n = int(rate_num)
    return f"intb*gcd({n}\\,round(1/intb))/{n}"


def curve_time_base(tb, rate_num: int):
    """The time base ``curve_settb_expr`` gives a stream in ``tb`` (a
    Fraction): the same integer steps (``round(1/TB)`` is the denominator
    of a 1/n time base)."""
    from fractions import Fraction
    tb = Fraction(tb)
    n = int(rate_num)
    den = int(c_round(1.0 / (tb.numerator / tb.denominator)))
    return tb * math.gcd(n, den) / n


def anchored_setpts_expr(cm: CurveMap, in_: float) -> str:
    """The curve ``setpts`` value on the file clock, anchored at ``in``."""
    T = f"(PTS*TB-{_num(in_)})"
    return f"({out_seconds_expr(cm, T)}+{_num(CURVE_TICK_BIAS)})/TB"


def anchored_ticks(pts_file: int, TB: float, in_: float, cm: CurveMap) -> int:
    """Output ticks ``anchored_setpts_expr`` gives the frame whose pts (on
    the file clock, in the refined time base ``TB``) is ``pts_file``: the
    same double operations, truncated like ``D2TS``."""
    T = float(pts_file) * TB - float(in_)
    return int((out_seconds(cm, T) + CURVE_TICK_BIAS) / TB)


def source_seconds(cm: CurveMap, t: float) -> float:
    """Source seconds consumed after ``t`` output seconds (the forward map:
    the curve's integral; clamps below 0, extends linearly past ``D``)."""
    if t <= 0:
        return 0.0
    for g in cm.segs:
        if t < g.t1:
            tau = t - g.t0
            return g.s0 + g.r * tau + (g.k / 4.0) * tau * tau
    return cm.s_end + (t - cm.D) * cm.r_end


def speed_at(cm: CurveMap, t: float) -> float:
    """Speed at output seconds ``t``."""
    for g in cm.segs:
        if t < g.t1:
            return g.r + (g.k / 2.0) * (t - g.t0)
    return cm.r_end


def split_curve(points: list[tuple[float, float]], u: float
                ) -> tuple[list[list[float]], list[list[float]]]:
    """The two curves a split at output position ``u`` (0 < u < 1) leaves:
    each half keeps its part of the shape, re-normalised over its own span,
    so ``S_left / mean_left`` + ``S_right / mean_right`` = the original
    footprint (up to rounding). For ``dispatch`` split ops (lane S2)."""
    u = min(1.0, max(0.0, float(u)))
    r_u = points[-1][1]
    for (x0, r0), (x1, r1) in zip(points, points[1:]):
        if x0 <= u <= x1:
            r_u = r0 if x1 == x0 else r0 + (r1 - r0) * (u - x0) / (x1 - x0)
            break
    left = [[x, r] for x, r in points if x < u] + [[u, r_u]]
    right = [[u, r_u]] + [[x, r] for x, r in points if x > u]
    lnorm = [[x / u, r] for x, r in left] if u > 0 else [[0.0, r_u], [1.0, r_u]]
    rnorm = ([[(x - u) / (1.0 - u), r] for x, r in right] if u < 1 else [[0.0, r_u], [1.0, r_u]])
    return lnorm, rnorm


def integral_fraction(points: list[tuple[float, float]], u: float) -> float:
    """Fraction of the clip's SOURCE consumed by output position ``u`` —
    where a split at ``u`` cuts the source (``in + S·fraction``)."""
    m = mean_speed(points)
    if m <= 0:
        return u
    acc = 0.0
    for (x0, r0), (x1, r1) in zip(points, points[1:]):
        if u <= x0:
            break
        x = min(u, x1)
        r = r0 + (r1 - r0) * ((x - x0) / (x1 - x0)) if x1 > x0 else r0
        acc += (x - x0) * (r0 + r) / 2.0
    return min(1.0, max(0.0, acc / m))


__all__ = [
    "CURVE_SPEED_RANGE", "MAX_CURVE_POINTS", "CURVE_PRESETS",
    "normalize_curve", "curve_points", "is_curve", "mean_speed",
    "CurveSeg", "CurveMap", "curve_map", "out_seconds", "setpts_expr", "start_speed",
    "CURVE_PREROLL_S", "CURVE_SEEK_GRID", "CURVE_TICK_BIAS", "curve_seek", "c_round",
    "file_clock_expr", "curve_settb_expr", "curve_time_base", "anchored_setpts_expr",
    "anchored_ticks",
    "source_seconds", "speed_at", "split_curve", "integral_fraction",
]
