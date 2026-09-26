"""Speed presets and speed-argument validation: ONE table read by the editing
engine (`agent/dispatch.set_speed`), the agent tool schema (`agent/tools.py`),
the Prompt Editor's plan validator and the Inspector (`GET /api/speed/presets`,
`api/speed_routes.py` — the browser never keeps its own copy).

The SHAPES are `speed_curve.CURVE_PRESETS` (lane S1; the render goldens use
them). This module adds only what an editor shows: the CapCut menu order, a
label and a one-line hint per shape, and which ones the menu lists. Nothing
here restates a point.

A curve that is exactly a preset's points carries that preset's id as its
`name`; any other curve is named "custom". The name is derived from the
points on every write (`resolve_speed`), never trusted from the caller, so a
dragged Hero point can never still read "Hero".
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from . import speed_curve as _sc

#: Constant-speed range (the model's `SPEED_RANGE`; CapCut Normal: 0.1-100x).
CONSTANT_SPEED_RANGE: tuple[float, float] = (0.1, 100.0)
#: A curve's speed range (CapCut Curve: 0.1-10x).
CURVE_SPEED_RANGE: tuple[float, float] = _sc.CURVE_SPEED_RANGE
#: Fewest and most points a curve may carry.
MIN_CURVE_POINTS = 2
MAX_CURVE_POINTS = _sc.MAX_CURVE_POINTS
#: The name a curve carries when it is not exactly a preset.
CUSTOM = "custom"

#: Freeze frame: CapCut's default hold, and the hold range a request may ask.
FREEZE_DEFAULT_SECONDS = 3.0
FREEZE_RANGE: tuple[float, float] = (0.1, 60.0)


@dataclass(frozen=True)
class SpeedPreset:
    id: str
    label: str
    hint: str
    #: Listed in the Inspector's Curve menu (CapCut's six); the others are
    #: reachable by name from the agent and the Prompt bar.
    menu: bool

    @property
    def points(self) -> list[list[float]]:
        return [list(p) for p in _sc.CURVE_PRESETS[self.id]]


#: CapCut's Curve menu, in its order, then the two extra ramps.
PRESETS: tuple[SpeedPreset, ...] = (
    SpeedPreset("montage", "Montage", "Normal, a fast burst, a slow beat, then fast to the end", True),
    SpeedPreset("hero", "Hero", "Slows to a quarter speed through the middle, then back to normal", True),
    SpeedPreset("bullet", "Bullet", "Fast, a bullet-time slow-down in the middle, fast again", True),
    SpeedPreset("jump_cut", "Jump Cut", "Normal speed that jumps ahead at 6x in the middle", True),
    SpeedPreset("flash_in", "Flash In", "Starts at 5x and settles to normal speed", True),
    SpeedPreset("flash_out", "Flash Out", "Normal speed that accelerates to 5x at the end", True),
    SpeedPreset("ramp_up", "Ramp Up", "Accelerates steadily from 0.5x to 3x", False),
    SpeedPreset("ramp_down", "Ramp Down", "Decelerates steadily from 3x to 0.5x", False),
)
PRESET_BY_ID: dict[str, SpeedPreset] = {p.id: p for p in PRESETS}
PRESET_IDS: tuple[str, ...] = tuple(p.id for p in PRESETS)

# Every preset shape exists, and every shape has a label (import-time check:
# a preset added to speed_curve without a label here fails loudly).
assert set(PRESET_IDS) == set(_sc.CURVE_PRESETS), "speed presets and CURVE_PRESETS disagree"


def preset_id(name: Any) -> str | None:
    """The canonical preset id for a user/agent spelling ("Jump Cut",
    "jump-cut", "JUMP_CUT", "Flash in" → its id), or None."""
    if not isinstance(name, str):
        return None
    key = "_".join(name.strip().lower().replace("-", " ").replace("_", " ").split())
    return key if key in PRESET_BY_ID else None


def curve_name(points: list[list[float]] | list[tuple[float, float]]) -> str:
    """The preset id whose points are exactly `points`, else "custom"."""
    canon = [[float(x), float(r)] for x, r in points]
    for p in PRESETS:
        if canon == p.points:
            return p.id
    return CUSTOM


def _finite_number(v: Any, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise ValueError(f"{what} must be a number, got {v!r}")
    try:
        f = float(v)
    except ValueError:
        raise ValueError(f"{what} must be a number, got {v!r}") from None
    if not math.isfinite(f):
        raise ValueError(f"{what} must be a finite number, got {v!r}")
    return f


def validate_curve_points(raw: Any) -> list[list[float]]:
    """The points of a requested curve, checked (a crisp 400, never a silent
    clamp): 2-32 `[position, speed]` pairs, position 0-1, speed 0.1-10x, all
    finite. Order does not matter (the model sorts); duplicate positions do:
    one position cannot hold two speeds."""
    if isinstance(raw, dict):
        raw = raw.get("curve")
    if not isinstance(raw, (list, tuple)):
        raise ValueError("a speed curve is a list of [position, speed] points")
    if not MIN_CURVE_POINTS <= len(raw) <= MAX_CURVE_POINTS:
        raise ValueError(f"a speed curve has {MIN_CURVE_POINTS}-{MAX_CURVE_POINTS} points, got {len(raw)}")
    lo, hi = CURVE_SPEED_RANGE
    out: list[list[float]] = []
    seen: set[float] = set()
    for i, p in enumerate(raw):
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            raise ValueError(f"curve point {i} must be [position, speed], got {p!r}")
        x = _finite_number(p[0], f"curve point {i} position")
        r = _finite_number(p[1], f"curve point {i} speed")
        if not 0.0 <= x <= 1.0:
            raise ValueError(f"curve point {i} position {x:g} is outside 0-1 (a fraction of the clip)")
        if not lo <= r <= hi:
            raise ValueError(f"curve point {i} speed {r:g}x is outside {lo:g}-{hi:g}x")
        if x in seen:
            raise ValueError(f"two curve points sit at position {x:g}")
        seen.add(x)
        out.append([x, r])
    return out


def curve_speed(points: list[list[float]]) -> dict:
    """The stored speed value of a validated curve: the model's canonical
    form plus the derived name."""
    canon = _sc.normalize_curve({"curve": points})
    if canon is None:                       # unreachable after validation
        raise ValueError("the speed curve has no usable points")
    canon["name"] = curve_name(canon["curve"])
    return canon


def preset_speed(name: Any) -> dict:
    pid = preset_id(name)
    if pid is None:
        names = ", ".join(p.label for p in PRESETS)
        raise ValueError(f"unknown speed preset {name!r} — one of: {names}")
    return curve_speed(PRESET_BY_ID[pid].points)


def resolve_speed(*, factor: Any = None, curve: Any = None, preset: Any = None) -> float | dict:
    """What `set_speed` stores for its arguments: a constant factor, or a
    curve from explicit points or a preset name. Exactly one must be given."""
    given = [k for k, v in (("factor", factor), ("curve", curve), ("preset", preset)) if v is not None]
    if not given:
        raise ValueError("set_speed needs a factor, a curve or a preset")
    if len(given) > 1:
        raise ValueError(f"set_speed takes one of factor, curve or preset — got {' and '.join(given)}")
    if preset is not None:
        return preset_speed(preset)
    if curve is not None:
        return curve_speed(validate_curve_points(curve))
    f = _finite_number(factor, "speed factor")
    if f <= 0:
        raise ValueError("speed factor must be > 0")
    lo, hi = CONSTANT_SPEED_RANGE
    if not lo <= f <= hi:
        raise ValueError(f"speed factor {f:g}x is outside {lo:g}-{hi:g}x")
    return f


def describe(speed: Any) -> str:
    """Editor words for a stored speed: "2.00x", "Hero curve", "custom curve"."""
    if isinstance(speed, dict) and _sc.is_curve(speed):
        name = speed.get("name")
        p = PRESET_BY_ID.get(name) if isinstance(name, str) else None
        return f"{p.label} curve" if p else "custom curve"
    if isinstance(speed, (int, float)) and speed > 0:
        return f"{float(speed):.2f}x"
    return "1.00x"


def presets_payload() -> dict:
    """`GET /api/speed/presets`: everything the Inspector needs, from here."""
    return {
        "presets": [{"id": p.id, "label": p.label, "hint": p.hint, "menu": p.menu,
                     "points": p.points} for p in PRESETS],
        "custom": CUSTOM,
        "curve_range": list(CURVE_SPEED_RANGE),
        "constant_range": list(CONSTANT_SPEED_RANGE),
        "max_points": MAX_CURVE_POINTS,
        "freeze_default": FREEZE_DEFAULT_SECONDS,
        "freeze_range": list(FREEZE_RANGE),
    }


__all__ = [
    "CONSTANT_SPEED_RANGE", "CURVE_SPEED_RANGE", "MIN_CURVE_POINTS", "MAX_CURVE_POINTS", "CUSTOM",
    "FREEZE_DEFAULT_SECONDS", "FREEZE_RANGE", "SpeedPreset", "PRESETS", "PRESET_BY_ID", "PRESET_IDS",
    "preset_id", "curve_name", "validate_curve_points", "curve_speed", "preset_speed",
    "resolve_speed", "describe", "presets_payload",
]
