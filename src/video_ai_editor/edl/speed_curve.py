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


def out_seconds(cm: CurveMap, T: float) -> float:
    """Output seconds at source seconds ``T`` — the ``setpts`` expression of
    ``setpts_expr`` evaluated with the same double operations in the same
    order (``render/frame_map.py`` truncates ``out / TB`` like ffmpeg's
    ``D2TS``)."""
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
    return expr


def setpts_expr(cm: CurveMap) -> str:
    """The ``setpts`` value (no ``setpts=``): ``out_seconds_expr`` at
    ``T = PTS·TB``, divided by ``TB`` into the stream's ticks."""
    return f"({out_seconds_expr(cm, '(PTS*TB)')})/TB"


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
    "CurveSeg", "CurveMap", "curve_map", "out_seconds", "setpts_expr",
    "source_seconds", "speed_at", "split_curve", "integral_fraction",
]
