"""`GET /api/animations/presets` — the clip-animation presets (CapCut In /
Out / Combo) and their duration rules, for the Inspector's Animation section
(wave E, F1).

The table is `edl/clip_animations.py`; `agent/dispatch.set_animation`, the
agent tool schema, the Prompt Editor's validator and every renderer read the
same module (the browser's renderers through `lib/anim/clipAnimTable.json`,
generated from it and pinned equal by tests/test_clip_animations.py).
Read-only and session-independent; immutable for a given build, so the
client fetches it once.
"""
from __future__ import annotations

from fastapi import APIRouter

from ..edl.clip_animations import table_json

router = APIRouter(tags=["animations"])


@router.get("/api/animations/presets")
def animation_presets() -> dict:
    """`{in: [preset], out: [preset], combo: [preset], dur_default,
    dur_range, share, blur_sigma_frac}`; a preset is `{id, kind, label, hint,
    icon, channels, waves}`."""
    return table_json()
