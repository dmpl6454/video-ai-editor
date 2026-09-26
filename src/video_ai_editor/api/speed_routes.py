"""`GET /api/speed/presets` — the speed-curve presets and speed ranges, for
the Inspector's Speed section (wave D, lane S2).

The table is `edl/speed_presets.py` (the shapes are `edl/speed_curve.
CURVE_PRESETS`); `agent/dispatch.set_speed`, the agent tool schema and the
Prompt Editor's validator read the same module. The browser keeps no copy of
its own, so a preset added or reshaped server-side reaches the curve menu,
the handler and the agent together. Read-only and session-independent;
immutable for a given build, so the client fetches it once.
"""
from __future__ import annotations

from fastapi import APIRouter

from ..edl.speed_presets import presets_payload

router = APIRouter(tags=["speed"])


@router.get("/api/speed/presets")
def speed_presets() -> dict:
    """`{presets: [{id, label, hint, menu, points}], custom, curve_range,
    constant_range, max_points, freeze_default, freeze_range}`."""
    return presets_payload()
