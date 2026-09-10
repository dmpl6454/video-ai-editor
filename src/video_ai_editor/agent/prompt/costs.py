"""Wall-clock estimates per step (spec §1.3 rule 9): `estimated_seconds =
Σ step_cost`, which gates the `go` confirmation above `LONG_RUN_SECONDS`
and drives the synthetic progress bar for handlers that report none.

Seconds are for THIS machine class (an M-series Mac); they are estimates a
user sees as "about 3 minutes", never a contract — the only hard rule is
that a Whisper pass scales with the source length and its model size, so a
10-minute large-v3 caption run asks before it starts.
"""
from __future__ import annotations

from typing import Callable

from .facts import TimelineFacts
from .schema import Step


def _whisper_cost(model: str, duration: float) -> float:
    if model == "large-v3":
        return 0.9 * duration + 10
    if model == "large-v3-turbo":
        return 0.35 * duration + 6
    return 0.12 * duration + 3


RECIPE_COST: dict[str, Callable[[TimelineFacts], float]] = {
    "transcribe": lambda f: _whisper_cost("small", f.duration),
    "auto_caption": lambda f: _whisper_cost(f.whisper_cached_best(), f.duration) + 2,
    "add_caption_track": lambda f: 1.0,
    "translate_captions": lambda f: 25.0 + 0.05 * f.duration,
    "remove_silences": lambda f: 0.05 * f.duration + 1,
    "remove_fillers": lambda f: 1.0,
    "make_shorts": lambda f: 0.1 * f.duration + 3,
    "auto_reframe": lambda f: 0.6 * f.duration + 3,
    "set_clip_fit": lambda f: 0.2,
    "noise_reduce": lambda f: 0.3 * f.duration + 2,
    "stabilize": lambda f: 2.0 * f.duration,
    "upscale": lambda f: 6.0 * f.duration,
    "smooth_slow_motion": lambda f: 4.0 * f.duration,
    "tts_voiceover": lambda f: 3.0,
    "add_music": lambda f: 1.0,
    "auto_cut_to_beats": lambda f: 0.05 * f.duration + 2,
    "audit_aesthetic": lambda f: 0.5,
    "apply_hook_stack": lambda f: 0.5,
    "apply_brand_kit": lambda f: 0.5,
}
DEFAULT_STEP_COST = 0.3


def step_cost(s: Step, f: TimelineFacts) -> float:
    if s.tool in ("transcribe", "auto_caption"):
        model = str(s.args.get("model") or ("small" if s.tool == "transcribe" else f.whisper_cached_best()))
        return _whisper_cost(model, f.duration) + (2 if s.tool == "auto_caption" else 0)
    if s.tool == "auto_reframe" and not s.args.get("subject_track", True):
        return 1.0
    fn = RECIPE_COST.get(s.tool)
    return fn(f) if fn else DEFAULT_STEP_COST


def estimate_seconds(steps: list[Step] | tuple[Step, ...], f: TimelineFacts) -> float:
    return round(sum(step_cost(s, f) for s in steps), 1)


__all__ = ["RECIPE_COST", "DEFAULT_STEP_COST", "step_cost", "estimate_seconds"]
