"""Keyframe utilities — sample a `Keyframe` (or scalar) at a given time, and
turn one into an ffmpeg expression for filter graphs that animate per frame.
"""
from __future__ import annotations
from typing import Iterable
from .schema import Keyframe


def sample(value: float | Keyframe | dict, t: float) -> float:
    """Evaluate a keyframed value at time `t` (clip-local seconds)."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        kfs = value.get("keyframes") or []
        interp = value.get("interp", "linear")
    else:  # Keyframe instance
        kfs = list(value.keyframes)
        interp = value.interp
    if not kfs:
        return 0.0
    pts = sorted([(float(p[0]), float(p[1])) for p in kfs], key=lambda p: p[0])
    if t <= pts[0][0]:
        return pts[0][1]
    if t >= pts[-1][0]:
        return pts[-1][1]
    for i in range(len(pts) - 1):
        t0, v0 = pts[i]
        t1, v1 = pts[i + 1]
        if t0 <= t <= t1:
            if t1 - t0 < 1e-9:
                return v1
            f = (t - t0) / (t1 - t0)
            if interp == "step":
                return v0
            if interp == "ease-in":
                f = f * f
            elif interp == "ease-out":
                f = 1 - (1 - f) ** 2
            elif interp == "ease-in-out":
                f = 3 * f * f - 2 * f * f * f
            elif interp == "back-out":
                f = 1 - (1 - f) ** 3
            return v0 + (v1 - v0) * f
    return pts[-1][1]


def is_keyframed(value: float | Keyframe | dict | None) -> bool:
    if value is None or isinstance(value, (int, float)):
        return False
    kfs = (value.get("keyframes") if isinstance(value, dict) else value.keyframes) or []
    return len(kfs) >= 2


def _segment_expr(t0: float, v0: float, t1: float, v1: float,
                  interp: str, time_var: str) -> str:
    """One segment's value expression, with the same easing math as sample().

    Commas inside function calls are escaped (`\\,`) because these
    expressions are embedded in filtergraph option values.
    """
    if interp == "step" or t1 - t0 < 1e-6:
        return f"{v0:.4f}" if interp == "step" else f"{v1:.4f}"
    f_raw = f"(({time_var}-{t0:.4f})/({t1 - t0:.6f}))"
    if interp == "ease-in":
        prog = f"pow({f_raw}\\,2)"
    elif interp == "ease-out":
        prog = f"(1-pow(1-{f_raw}\\,2))"
    elif interp == "ease-in-out":
        prog = f"({f_raw}*{f_raw}*(3-2*{f_raw}))"  # smoothstep, as in sample()
    elif interp == "back-out":
        prog = f"(1-pow(1-{f_raw}\\,3))"
    else:
        prog = f_raw
    return f"({v0:.4f}+{prog}*({v1 - v0:.4f}))"


def to_ffmpeg_expr(value: float | Keyframe | dict, *, time_var: str = "t",
                   start_offset: float = 0.0) -> str:
    """Build an ffmpeg filter expression for a keyframed scalar.

    `start_offset` shifts so that t=0 in the expression maps to clip-local 0.
    Easing mirrors sample() exactly — what the browser preview animates is
    what exports (they used to diverge: every mode exported as linear).

    Returns a numeric expression usable in filters that accept the `t` variable
    (e.g. overlay's x= and y=).
    """
    if isinstance(value, (int, float)):
        return f"{value:.4f}"
    if isinstance(value, dict):
        kfs = value.get("keyframes") or []
        interp = value.get("interp", "linear")
    else:
        kfs = value.keyframes or []
        interp = value.interp
    if not kfs:
        return "0"
    pts = sorted([(float(p[0]) - start_offset, float(p[1])) for p in kfs], key=lambda p: p[0])
    if len(pts) == 1:
        return f"{pts[0][1]:.4f}"

    # Build a chain of `if(lt(t, t1), interp(t0,t1,v0,v1), …)`
    # ffmpeg eval syntax: `lerp(a,b,f)` is `a*(1-f)+b*f`, `clip(x,min,max)`.
    # Wrap into a piecewise expression.
    expr = f"{pts[-1][1]:.4f}"  # default: hold last value past last keyframe
    for i in range(len(pts) - 1, 0, -1):
        t0, v0 = pts[i - 1]
        t1, v1 = pts[i]
        seg = _segment_expr(t0, v0, t1, v1, interp, time_var)
        expr = f"if(lt({time_var}\\,{t1:.4f})\\,{seg}\\,{expr})"
    # Hold first value before t0
    expr = f"if(lt({time_var}\\,{pts[0][0]:.4f})\\,{pts[0][1]:.4f}\\,{expr})"
    return expr
