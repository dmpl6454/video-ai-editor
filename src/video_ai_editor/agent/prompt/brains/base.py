"""Brain protocol — the one interface all four brains implement (spec §3.1).

Frozen contract (§0.2). Consumers: the router (B), the executor/service (X),
the brain badge (D) and the benchmark (K).

The four brains, in ladder order (§3.4): `recipes` (grammar + recipe table,
instant, always available), `apple_intelligence` (FoundationModels via the
`fm-planner` child process; availability-gated — on the build Mac
`SystemLanguageModel.default.availability` reported
`appleIntelligenceNotEnabled`, so the design never depends on it),
`local_model` (Qwen2.5-7B-Instruct-4bit through mlx-lm; `mlx_lm` is imported
lazily and its absence is a `fix`, never an ImportError at import time), and
`claude` (only with `ANTHROPIC_API_KEY` and `VAI_PROMPT_CLOUD != "0"`).

Honesty rules the protocol encodes:

  * `availability()` always returns a `fix` when unavailable — the UI must
    say which brain answered AND why a better one did not.
  * `BrainResult.reason` is one of `REASONS`; free text goes in the
    `rejected:<why>` suffix only, so the badge ladder can group failures.
  * On-device brains get recipe cards only and emit `IntentDraft`; tool
    cards reach the `claude` brain alone (§3.1). `BrainRequest.tool_cards`
    exists for that one consumer.
  * No brain ever receives a secret, an absolute path, or a network route
    that is not loopback-only (§3.5).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol, TypedDict, runtime_checkable

from ..facts import TimelineFacts
from ..recipes import RecipeCard
from ..schema import Plan

BRAIN_IDS: tuple[str, ...] = ("recipes", "apple_intelligence", "local_model", "claude")

#: Display labels. The first `text_delta` of every turn starts with
#: `"via <label> — "` so the phone shows which brain answered with zero
#: mobile changes (§4.1); D's badge uses the same strings.
BRAIN_LABELS: dict[str, str] = {
    "recipes": "Recipes",
    "apple_intelligence": "Apple Intelligence",
    "local_model": "Local model",
    "claude": "Claude",
}

#: `BrainResult.reason` vocabulary (§3.1). `rejected` carries a `:<why>` suffix.
REASONS: tuple[str, ...] = ("unavailable", "timeout", "guardrail", "refusal", "language",
                            "context", "busy", "decode", "parse", "rejected")

#: `availability()["action"]`: what the UI may offer for an unavailable brain.
AvailabilityAction = Literal["none", "enable_in_settings", "install", "download", "add_key"]


class Availability(TypedDict):
    available: bool
    detail: str
    fix: str | None
    action: AvailabilityAction
    model: str | None


def available(detail: str, *, model: str | None = None) -> Availability:
    return {"available": True, "detail": detail, "fix": None, "action": "none", "model": model}


def unavailable(detail: str, fix: str | None, *, action: AvailabilityAction = "none",
                model: str | None = None) -> Availability:
    return {"available": False, "detail": detail, "fix": fix, "action": action, "model": model}


class BrainUnavailable(RuntimeError):
    """Raised by a brain that cannot run at all right now (no helper binary,
    Apple Intelligence off, mlx_lm not importable). Carries the honest
    `reason` and the `fix` the UI shows."""

    def __init__(self, reason: str, fix: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.fix = fix


@dataclass(frozen=True)
class ToolCard:
    """A raw tool description for the `claude` brain only: name, one line,
    and its `input_schema` as `/api/tools` advertises it."""
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class BrainRequest:
    prompt: str
    facts: TimelineFacts
    recipes: list[RecipeCard]
    tool_cards: list[ToolCard] = field(default_factory=list)
    prior_clarification: dict | None = None
    #: `(text, brain_id)` written by a content brain (§3.7). Set by the
    #: router on its SECOND pass only: the first pass reveals whether the
    #: plan carries a heuristic hook at all, and only then is a model asked
    #: for words. Adapters expand with it (`planner.plan(hook_text=…)` /
    #: `recipes.from_intents(hook_text=…)`) and reuse their cached draft
    #: rather than generating twice — so the field never costs a second
    #: model call.
    hook_text: tuple[str, str] | None = None

    def with_(self, **changes: Any) -> "BrainRequest":
        return replace(self, **changes)


@dataclass(frozen=True)
class BrainResult:
    plan: Plan | None
    brain: str
    ok: bool
    reason: str = ""      # one of REASONS, or "rejected:<why>"
    latency_ms: int = 0
    model: str = ""

    @staticmethod
    def failure(brain: str, reason: str, *, latency_ms: int = 0, model: str = "") -> "BrainResult":
        base = reason.split(":", 1)[0]
        if base not in REASONS:
            raise ValueError(f"BrainResult.reason must start with one of {REASONS}, got {reason!r}")
        return BrainResult(plan=None, brain=brain, ok=False, reason=reason,
                           latency_ms=latency_ms, model=model)


#: The orchestrator's name for the same record.
BrainAnswer = BrainResult


# --- content tasks (§3.7) ---------------------------------------------------

TextTaskKind = Literal["hook_candidates", "rank_windows"]


@dataclass(frozen=True)
class TextTask:
    """A content task where an LLM adds value over a grammar: hook text
    candidates from the transcript head, or ranking candidate windows for
    shorts. `payload` is task-specific and path-free:
      hook_candidates → {"transcript_head": str, "n": 3, "max_words": 7}
      rank_windows    → {"windows": [{"start","end","text"}], "k": int}"""
    kind: TextTaskKind
    payload: dict[str, Any]


@dataclass(frozen=True)
class TextResult:
    items: list[Any]       # [str] for hook_candidates, [int] window indices for rank_windows
    brain: str
    model: str = ""
    latency_ms: int = 0


@runtime_checkable
class Brain(Protocol):
    id: str

    def availability(self) -> Availability: ...

    def plan(self, req: BrainRequest, *, timeout_s: float) -> BrainResult: ...

    def text(self, task: TextTask, *, timeout_s: float) -> TextResult | None: ...
    # WHY `None` is a valid answer: the recipes brain has no language model,
    # so the `hook` recipe falls back to the transcript heuristic and the
    # reply says "hook text: heuristic (no local model available)".


__all__ = ["BRAIN_IDS", "BRAIN_LABELS", "REASONS", "AvailabilityAction", "Availability",
           "available", "unavailable", "BrainUnavailable", "ToolCard", "BrainRequest",
           "BrainResult", "BrainAnswer", "TextTaskKind", "TextTask", "TextResult", "Brain"]
