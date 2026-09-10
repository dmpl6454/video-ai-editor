"""The brain ladder (spec §3.4) and the honest `brains_report()` (§0.2).

    recipes (instant) ──confidence ≥ 0.75──▶ answered
        │ else, within a 12 s planning budget
        ▼
    apple_intelligence ─▶ local_model ─▶ claude (key + VAI_PROMPT_CLOUD≠0)
        each: brain.plan → validate_plan → confidence ≥ 0.5 → answered
        │ all fell through
        ▼
    recipes ≥ 0.4 → run with "I read that as: …"   else → clarify{key:"intent"}

Then the content pass (§3.7): if the winning plan carries a hook whose text
is the transcript heuristic, a content brain is asked for words and the SAME
brain re-expands with `BrainRequest.hook_text` set (from its cached draft —
no second generation). The plan's `content_brain` then names the writer,
which is how the badge shows "Recipes · text by Apple Intelligence".

Every attempt becomes one `brain{status}` SSE event through `emit` and one
`Attempt` in `RoutedPlan.attempts`, so the badge can show the ladder and the
benchmark can count per brain. `VAI_BRAIN=recipes|fm|mlx|cloud|auto` pins
one rung (the recipes fallback stays — it is a safety net, not a choice).

WHY the availability probes run concurrently with the grammar: a cold
`fm-planner probe` is a process spawn and `models.status()` is a directory
walk; launching them on a daemon thread as the turn starts means the common
path (recipes confident) never waits for them, and the rare path (LLM rung)
finds the answer already cached (60 s TTL inside each brain).

`validate_plan` (P) and `planner` (P) are imported lazily; a test injects
stand-ins and the router degrades honestly when they are not on the tree.
"""
from __future__ import annotations

import logging
import os
import platform
import sys
import threading
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, Mapping

from ..facts import TimelineFacts
from ..recipes import RECIPE_BY_NAME, RECIPE_NAMES, cards as recipe_cards
from ..schema import PLAN_DENY, NeedsInput, NeedsInputOption, Plan
from . import content
from .base import BRAIN_IDS, BRAIN_LABELS, Availability, Brain, BrainRequest, BrainResult

__all__ = ["Attempt", "RoutedPlan", "RECIPES_CONFIDENT", "RECIPES_FALLBACK", "LLM_MIN_CONFIDENCE",
           "PLANNING_BUDGET_S", "HOOK_TEXT_TIMEOUT_S", "BRAIN_PIN_ENV", "PIN_ALIASES", "resolve_order",
           "default_brains", "build_request", "prefetch_availability", "plan", "brains_report", "ValidateFn"]

_log = logging.getLogger(__name__)

RECIPES_CONFIDENT = 0.75
RECIPES_FALLBACK = 0.4
LLM_MIN_CONFIDENCE = 0.5
PLANNING_BUDGET_S = 12.0
MIN_ATTEMPT_S = 1.5             # below this an LLM rung is skipped, not started
FM_ATTEMPT_CAP_S = 8.0
MLX_ATTEMPT_CAP_S = 12.0
CLOUD_TIMEOUT_S = 20.0
#: The hook-text pass (§3.7) has its own allowance, outside the planning
#: budget: it only runs when the plan already carries a heuristic hook, and
#: a 6 s wait for words in the speaker's own voice beats canned clickbait.
HOOK_TEXT_TIMEOUT_S = 6.0
BRAIN_PIN_ENV = "VAI_BRAIN"
PIN_ALIASES: dict[str, str] = {"recipes": "recipes", "fm": "apple_intelligence",
                               "apple_intelligence": "apple_intelligence", "mlx": "local_model",
                               "local_model": "local_model", "cloud": "claude", "claude": "claude"}
ON_DEVICE = ("apple_intelligence", "local_model")

ValidateFn = Callable[[Plan, TimelineFacts], Plan]


@dataclass(frozen=True)
class Attempt:
    brain: str
    status: str                 # "answered" | "failed" | "skipped"
    reason: str = ""
    latency_ms: int = 0
    model: str = ""
    detail: str = ""

    @property
    def label(self) -> str:
        return BRAIN_LABELS.get(self.brain, self.brain)

    def as_event(self) -> dict[str, Any]:
        ev: dict[str, Any] = {"type": "brain", "status": self.status, "brain": self.brain, "label": self.label}
        if self.model:
            ev["model"] = self.model
        if self.detail or self.reason:
            ev["detail"] = self.detail or self.reason
        if self.latency_ms:
            ev["latency_ms"] = self.latency_ms
        return ev


@dataclass(frozen=True)
class RoutedPlan:
    plan: Plan | None
    brain: str | None
    attempts: tuple[Attempt, ...] = ()
    clarify: NeedsInput | None = None
    note: str = ""
    #: Set when the plan carried a hook and the content pass ran (§3.7):
    #: which brain wrote the words, or the heuristic with its honest source.
    hook_text: content.HookText | None = None

    @property
    def answered(self) -> bool:
        return self.plan is not None

    @property
    def label(self) -> str:
        return BRAIN_LABELS.get(self.brain or "recipes", "Recipes")


# --- brains + request ----------------------------------------------------------------

_DEFAULT_BRAINS: dict[str, Brain] | None = None
_DEFAULT_LOCK = threading.Lock()


def default_brains() -> dict[str, Brain]:
    """Process-wide singletons: the MLX brain keeps its model loaded and the
    FM brain its probe cache, so they must not be rebuilt per turn."""
    global _DEFAULT_BRAINS
    with _DEFAULT_LOCK:
        if _DEFAULT_BRAINS is None:
            from .cloud_plan import CloudBrain
            from .fm import FMBrain
            from .mlx_brain import MLXBrain
            from .recipes_brain import RecipesBrain
            _DEFAULT_BRAINS = {"recipes": RecipesBrain(), "apple_intelligence": FMBrain(),
                               "local_model": MLXBrain(), "claude": CloudBrain()}
        return _DEFAULT_BRAINS


def build_request(prompt: str, facts: TimelineFacts, *, prior_clarification: dict | None = None,
                  with_tool_cards: bool = True) -> BrainRequest:
    """What every brain receives: recipe cards for all, tool cards for the
    `claude` rung only (`BrainRequest.tool_cards` is read by nothing else)."""
    tool_cards = []
    if with_tool_cards:
        from .cloud_plan import default_tool_cards
        tool_cards = default_tool_cards()
    return BrainRequest(prompt=prompt, facts=facts, recipes=recipe_cards(),
                        tool_cards=tool_cards, prior_clarification=prior_clarification)


def resolve_order(pin: str | None = None, *, env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """The ladder, or the single pinned rung. Unknown pins fall back to auto."""
    pin = (pin if pin is not None else (env if env is not None else os.environ).get(BRAIN_PIN_ENV, "auto")) or "auto"
    pinned = PIN_ALIASES.get(pin.strip().lower())
    return (pinned,) if pinned else tuple(BRAIN_IDS)


def prefetch_availability(brains: Mapping[str, Brain] | None = None) -> threading.Thread:
    """Warm the on-device brains' availability caches on a daemon thread."""
    brains = brains or default_brains()

    def _warm() -> None:
        for bid in ON_DEVICE:
            brain = brains.get(bid)
            if brain is None:
                continue
            try:
                brain.availability()
            except Exception:      # a probe failure is reported by the brain itself later
                pass

    t = threading.Thread(target=_warm, name="vai-brain-probe", daemon=True)
    t.start()
    return t


def _default_validate() -> ValidateFn | None:
    try:
        from ..validate import validate_plan          # P's (§1.3)
    except ImportError:
        return None
    return validate_plan


# --- the ladder ---------------------------------------------------------------------

def plan(req: BrainRequest, *, order: Iterable[str] | None = None,
         emit: Callable[[dict[str, Any]], None] | None = None,
         brains: Mapping[str, Brain] | None = None, validate: ValidateFn | None = None,
         budget_s: float = PLANNING_BUDGET_S, clock: Callable[[], float] = time.monotonic,
         prefetch: bool = True) -> RoutedPlan:
    brains = brains or default_brains()
    order = tuple(order) if order is not None else resolve_order()
    validate = validate or _default_validate()
    emit = emit or (lambda _e: None)
    attempts: list[Attempt] = []

    def record(a: Attempt) -> None:
        attempts.append(a)
        emit(a.as_event())

    def trying(bid: str, model: str = "") -> None:
        emit({"type": "brain", "status": "trying", "brain": bid, "label": BRAIN_LABELS[bid],
              **({"model": model} if model else {})})

    if prefetch and any(b in order for b in ON_DEVICE):
        prefetch_availability(brains)
    start = clock()

    # 1. recipes — instant.
    recipes_result: BrainResult | None = None
    if "recipes" in order and "recipes" in brains:
        trying("recipes")
        recipes_result = brains["recipes"].plan(req, timeout_s=budget_s)
        if recipes_result.ok and recipes_result.plan is not None:
            conf = recipes_result.plan.confidence
            if conf >= RECIPES_CONFIDENT:
                record(Attempt("recipes", "answered", latency_ms=recipes_result.latency_ms,
                               model=recipes_result.model, detail=f"confidence {conf:.2f}"))
                return _finish(RoutedPlan(recipes_result.plan, "recipes", tuple(attempts)), req,
                               brains=brains, order=order, validate=validate)
            record(Attempt("recipes", "failed", reason=f"rejected:low confidence {conf:.2f}",
                           latency_ms=recipes_result.latency_ms, model=recipes_result.model))
        else:
            record(Attempt("recipes", "failed", reason=recipes_result.reason,
                           latency_ms=recipes_result.latency_ms, model=recipes_result.model))

    # 2. on-device rungs within the budget; 3. cloud.
    for bid in order:
        if bid == "recipes" or bid not in brains:
            continue
        brain = brains[bid]
        if bid in ON_DEVICE:
            remaining = budget_s - (clock() - start)
            if remaining < MIN_ATTEMPT_S:
                record(Attempt(bid, "skipped", reason="timeout", detail="planning budget exhausted"))
                continue
            cap = FM_ATTEMPT_CAP_S if bid == "apple_intelligence" else MLX_ATTEMPT_CAP_S
            timeout_s = min(remaining, cap)
        else:
            timeout_s = CLOUD_TIMEOUT_S
        av = _safe_availability(brain)
        if not av["available"]:
            record(Attempt(bid, "failed", reason="unavailable", model=av.get("model") or "",
                           detail=av["detail"] + (f" — {av['fix']}" if av.get("fix") else "")))
            continue
        trying(bid, av.get("model") or "")
        try:
            result = brain.plan(req, timeout_s=timeout_s)
        except Exception as e:      # a brain bug is a fall-through, never a failed turn
            result = BrainResult.failure(bid, f"rejected:{(str(e) or type(e).__name__)[:140]}")
        if not result.ok or result.plan is None:
            record(Attempt(bid, "failed", reason=result.reason or "decode",
                           latency_ms=result.latency_ms, model=result.model))
            continue
        validated, why = _validate(result.plan, req.facts, validate)
        if validated is None:
            record(Attempt(bid, "failed", reason=f"rejected:{why}", latency_ms=result.latency_ms,
                           model=result.model))
            continue
        if validated.confidence < LLM_MIN_CONFIDENCE:
            record(Attempt(bid, "failed", reason=f"rejected:low confidence {validated.confidence:.2f}",
                           latency_ms=result.latency_ms, model=result.model))
            continue
        if validated.brain != bid:
            validated = validated.with_(brain=bid)
        record(Attempt(bid, "answered", latency_ms=result.latency_ms, model=result.model,
                       detail=f"confidence {validated.confidence:.2f}"))
        return _finish(RoutedPlan(validated, bid, tuple(attempts)), req,
                       brains=brains, order=order, validate=validate)

    # 4. fallback. A pinned LLM rung (VAI_BRAIN=mlx) skips recipes above, but
    # the safety net is not a brain choice: run it now so a failed pin still
    # ends in "I read that as: …" or an honest clarify, never an error.
    if recipes_result is None and "recipes" in brains:
        trying("recipes")
        recipes_result = brains["recipes"].plan(req, timeout_s=budget_s)
        if not (recipes_result.ok and recipes_result.plan is not None):
            record(Attempt("recipes", "failed", reason=recipes_result.reason,
                           latency_ms=recipes_result.latency_ms, model=recipes_result.model))
    if recipes_result is not None and recipes_result.ok and recipes_result.plan is not None:
        rp = recipes_result.plan
        if rp.confidence >= RECIPES_FALLBACK:
            reading = rp.title or rp.intent or "that"
            note = f"I read that as: {reading}"
            record(Attempt("recipes", "answered", latency_ms=recipes_result.latency_ms,
                           model=recipes_result.model, detail=f"fallback · confidence {rp.confidence:.2f}"))
            return _finish(RoutedPlan(rp.with_(reply=_fallback_reply(rp, note)), "recipes", tuple(attempts),
                                      note=note), req, brains=brains, order=order, validate=validate)
        clarify = _clarify_from(rp)
    else:
        clarify = _clarify_from(None)
    return RoutedPlan(None, None, tuple(attempts), clarify=clarify, note="clarify_intent")


def _fallback_reply(rp: Plan, note: str) -> str:
    if rp.reply and not rp.reply.startswith("I read that as"):
        return f"{note}. {rp.reply}"[:400]
    return (rp.reply or note)[:400]


# --- the content pass (§3.7) ----------------------------------------------------------

def _content_writers(answering: str, order: Iterable[str], brains: Mapping[str, Brain]) -> list[Brain]:
    """Brains asked for hook text, in order: the one that planned (it has
    the request in context), then the rest of the ladder. Only rungs in
    `order` — `VAI_BRAIN=recipes` means "no model", words included — and
    never `recipes`, which has no language model (`text()` → None)."""
    ids = [answering] + [b for b in order if b != answering]
    return [brains[b] for b in ids if b in brains and b != "recipes"]


def _finish(routed: RoutedPlan, req: BrainRequest, *, brains: Mapping[str, Brain], order: Iterable[str],
            validate: ValidateFn | None) -> RoutedPlan:
    """Second pass: when the plan carries the transcript-heuristic hook, ask
    a content brain for words and re-expand THROUGH THE SAME BRAIN with
    `req.hook_text` set — the recipe then records `content_brain` and its
    reply says who wrote the line ("Recipes · text by Local model").

    Every failure keeps the first-pass plan: the heuristic hook is a
    correct, honest plan (its reply says "no local model available"), and a
    content task must never turn an answered turn into a failed one."""
    plan = routed.plan
    if plan is None or routed.brain is None or not content.needs_hook_text(plan, req.facts):
        return routed
    writers = _content_writers(routed.brain, tuple(order), brains)
    if not writers:
        return routed       # VAI_BRAIN=recipes: no model, words included — the recipe's reply says so
    ht = content.hook_text(writers, req.facts.transcript_head, timeout_s=HOOK_TEXT_TIMEOUT_S)
    if ht.content_brain is None:
        return replace(routed, hook_text=ht)
    brain = brains.get(routed.brain)
    if brain is None:
        return replace(routed, hook_text=ht)
    try:
        result = brain.plan(req.with_(hook_text=(ht.text, ht.brain)), timeout_s=HOOK_TEXT_TIMEOUT_S)
    except Exception as e:      # a re-expansion bug is logged, never surfaced as a failed turn
        _log.warning("hook-text re-expansion via %s failed: %s", routed.brain, e)
        return replace(routed, hook_text=ht)
    if not result.ok or result.plan is None:
        return replace(routed, hook_text=ht)
    new: Plan | None = result.plan
    if routed.brain != "recipes":
        new, _why = _validate(new, req.facts, validate)
        if new is None:
            return replace(routed, hook_text=ht)
    if new.brain != routed.brain:
        new = new.with_(brain=routed.brain)
    if new.content_brain != ht.brain:
        # The recipe kept the user's own words or the hook fell away — the
        # model's line is not in the plan, so do not claim it is.
        return replace(routed, hook_text=ht)
    if routed.note.startswith("I read that as"):
        new = new.with_(reply=_fallback_reply(new, routed.note))
    return replace(routed, plan=new, hook_text=ht)


def _safe_availability(brain: Brain) -> Availability:
    try:
        return brain.availability()
    except Exception as e:
        return {"available": False, "detail": f"probe failed: {e}", "fix": None, "action": "none", "model": None}


def _validate(candidate: Plan, facts: TimelineFacts, validate: ValidateFn | None) -> tuple[Plan | None, str]:
    if validate is None:
        return None, "validator unavailable (agent/prompt/validate.py missing)"
    try:
        return validate(candidate, facts), ""
    except Exception as e:
        reasons = getattr(e, "reasons", None)
        why = "; ".join(str(r) for r in reasons) if reasons else (str(e) or type(e).__name__)
        return None, why[:140]


_CLARIFY_DEFAULTS = ("captions", "tighten", "auto_edit")


def _clarify_from(low: Plan | None) -> NeedsInput:
    """The `clarify{key:"intent"}` question (§2.7). If the recipes planner
    already attached its top-3 guesses as a `needs_input` keyed `intent`,
    reuse them; else guess from its intent plus the three commonest edits."""
    if low is not None:
        for q in low.needs_input:
            if q.key == "intent":
                return q
    guesses: list[str] = []
    if low is not None and low.intent in RECIPE_BY_NAME:
        guesses.append(low.intent)
    guesses += [g for g in _CLARIFY_DEFAULTS if g not in guesses]
    options = [NeedsInputOption(value=g, label=RECIPE_BY_NAME[g].description.rstrip("."))
               for g in guesses[:3] if g in RECIPE_NAMES]
    return NeedsInput(key="intent", question="I couldn't tell what to do. Did you mean one of these?",
                      kind="choice", options=options, required=True)


# --- brains_report --------------------------------------------------------------------

_REPORT_CACHE: dict[str, Any] = {}
_REPORT_TTL_S = 60.0


def _plan_tool_count() -> int:
    try:
        from ..validate import PLAN_TOOLS               # P's single definition (§1.3)
        return len(PLAN_TOOLS)
    except ImportError:
        from ... import tools as _tools
        return len(({t["name"] for t in _tools.list_tools()} | {"apply_hook_stack"}) - PLAN_DENY)


def brains_report(*, refresh: bool = False, brains: Mapping[str, Brain] | None = None,
                  env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """What `/api/prompt/brains` returns and the badge popover shows (§0.2 —
    this docstring is the payload contract for X, D and K):

        {"ladder": [brain ids in the order plan() tries them],
         "pinned": brain id | None,            # from VAI_BRAIN
         "best_available": brain id,           # highest available rung in the ladder, "recipes" at least
         "brains": [{"id", "label", "order", "in_ladder", "available": bool,
                     "detail": str,            # the real state ("appleIntelligenceNotEnabled",
                                               #   "not installed (pip)", "installed; model cached at ~/…")
                     "fix": str | None,        # the user action, ALWAYS set when unavailable
                     "action": "none|enable_in_settings|install|download|add_key",
                     "model": str | None}],
         "cloud_allowed": bool,                # VAI_PROMPT_CLOUD != "0"
         "machine": {"os", "arch", "ram_gb", "frozen"},
         "generated_at": epoch seconds}

    Honesty rules as `/api/features`: a `fix` is emitted only for an
    unavailable rung and names a real remedy for THIS build (a frozen app
    never gets a pip command). Memoised 60 s; `refresh=True` re-probes."""
    now = time.monotonic()
    if not refresh and brains is None and _REPORT_CACHE and now - _REPORT_CACHE["_at"] < _REPORT_TTL_S:
        return {k: v for k, v in _REPORT_CACHE.items() if k != "_at"}
    brains = brains or default_brains()
    env = env if env is not None else os.environ
    order = resolve_order(env=env)
    rows = []
    for rank, bid in enumerate(BRAIN_IDS):
        brain = brains.get(bid)
        if brain is None:
            continue
        if refresh and hasattr(brain, "invalidate"):
            brain.invalidate()
        av = _safe_availability(brain)
        detail = av["detail"]
        if bid == "recipes":
            detail = f"grammar + {len(recipe_cards(exclude=frozenset()))} recipes · {_plan_tool_count()} tools"
        rows.append({"id": bid, "label": BRAIN_LABELS[bid], "order": rank, "in_ladder": bid in order,
                     "available": bool(av["available"]), "detail": detail, "fix": av.get("fix"),
                     "action": av.get("action", "none"), "model": av.get("model")})
    best = next((r["id"] for r in rows if r["available"] and r["in_ladder"] and r["id"] != "recipes"), None)
    from .cloud_plan import cloud_allowed
    from .. import models as _models
    report = {
        "ladder": list(order),
        "pinned": PIN_ALIASES.get((env.get(BRAIN_PIN_ENV) or "").strip().lower()),
        "best_available": best or "recipes",
        "brains": rows,
        "cloud_allowed": cloud_allowed(env),
        "machine": {"os": platform.mac_ver()[0] or platform.platform(), "arch": platform.machine(),
                    "ram_gb": round(_models.total_ram_bytes() / _models.GB, 1),
                    "frozen": bool(getattr(sys, "frozen", False))},
        "generated_at": time.time(),
    }
    if brains is default_brains():
        _REPORT_CACHE.clear()
        _REPORT_CACHE.update(report, _at=now)
    return report
