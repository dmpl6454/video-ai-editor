"""The `recipes` brain: grammar → intents → recipe table → Plan (spec §1.2).

Always available, instant, and the router's first and last resort: it
answers outright at confidence ≥ 0.75, and when every language model falls
through it is what runs at ≥ 0.4 with "I read that as: …" (§3.4). It has no
language model, so `text()` returns None and the `hook` recipe falls back to
the transcript heuristic (`content.py`).

`planner.plan(prompt, facts)` is P's; it is imported lazily so this module
compiles on Day-0 and so a test can hand in a stand-in planner.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from ..facts import TimelineFacts
from ..recipes import RECIPE_NAMES
from ..schema import Plan
from .base import Availability, BrainRequest, BrainResult, TextResult, TextTask, available

__all__ = ["RecipesBrain", "PlannerFn"]

PlannerFn = Callable[..., Plan]      # (prompt, facts, *, hook_text=None) -> Plan


def _default_planner(prompt: str, facts: TimelineFacts, *, hook_text: tuple[str, str] | None = None) -> Plan:
    from .. import planner    # P's module (§2); ImportError → unavailable below
    return planner.plan(prompt, facts, hook_text=hook_text)


class RecipesBrain:
    id = "recipes"

    def __init__(self, planner: PlannerFn | None = None):
        self._planner = planner or _default_planner

    def availability(self) -> Availability:
        return available(f"grammar + {len(RECIPE_NAMES)} recipes; instant, offline", model="grammar")

    def plan(self, req: BrainRequest, *, timeout_s: float) -> BrainResult:
        t0 = time.monotonic()
        try:
            # WHY the keyword is passed only when set: a stand-in planner in a
            # test may take just (prompt, facts); the real one takes hook_text.
            if req.hook_text is not None:
                plan = self._planner(req.prompt, req.facts, hook_text=req.hook_text)
            else:
                plan = self._planner(req.prompt, req.facts)
        except (ImportError, NotImplementedError):
            # Day-0 honesty: the grammar is not on this tree yet.
            return BrainResult.failure(self.id, "unavailable", latency_ms=_ms(t0), model="grammar")
        except Exception as e:      # PlanRejected / ValidationError / a grammar bug
            return BrainResult.failure(self.id, f"rejected:{_why(e)}", latency_ms=_ms(t0), model="grammar")
        if plan.brain != self.id:
            plan = plan.with_(brain=self.id)
        return BrainResult(plan=plan, brain=self.id, ok=True, latency_ms=_ms(t0), model="grammar")

    def text(self, task: TextTask, *, timeout_s: float) -> TextResult | None:
        return None


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def _why(e: Any) -> str:
    reasons = getattr(e, "reasons", None)
    if reasons:
        return "; ".join(str(r) for r in reasons)[:160]
    return (str(e) or type(e).__name__)[:160]
