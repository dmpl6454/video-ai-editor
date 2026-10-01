"""The model Gateway (spec §9.1), EB1 slice: ONE capability `llm_json`, ONE
provider `apple_intelligence` through `fm.py`'s existing text path, ONE task
`rank_moments` with a JSON schema, a per-layer budget of 60 s, answers
frozen with provenance by the semantic layer.

Why the helper's `rank_windows` command carries `rank_moments`: the built
`fm-planner` accepts exactly `hook_candidates` and `rank_windows`
(tools/fm-planner/Sources/fm-planner/main.swift); the `@Generable` shapes of
spec §9 are lane EB-9 (EB2+). The task's payload is already the windows
shape, its answer is an index order, and the schema below is checked on the
way back — so nothing is lost this wave and the Swift rebuild is not on the
path.

Honesty: a provider that does not answer, answers late or answers junk
leaves the sentences on heuristic scores and marks the layer `partial`; the
notes say why. Nothing here opens a socket — the FM helper is a local child
process that `fm.default_runner` spawns with a scrubbed environment.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

from .digest import MAX_SENTENCES_PER_CALL, assert_path_free, rank_moments_payload

CALL_TIMEOUT_S = 8.0          # FM_ATTEMPT_CAP_S: one helper call
SEMANTIC_BUDGET_S = 60.0      # the per-layer budget this wave
TASK = "rank_moments"
TASK_SCHEMA: dict = json.loads((Path(__file__).parent / "tasks" / "rank_moments.schema.json").read_text("utf-8"))


@runtime_checkable
class Provider(Protocol):
    id: str
    engine_version: str

    def probe(self) -> dict: ...

    def run(self, payload: dict, *, timeout_s: float) -> dict | None: ...


@dataclass
class RankResult:
    scores: dict[str, float] = field(default_factory=dict)      # sentence id → 0..1 by rank position
    provenance: dict = field(default_factory=lambda: {"by": "recipes"})
    calls: int = 0
    spent_s: float = 0.0
    partial: bool = False
    notes: list[str] = field(default_factory=list)


def validate_answer(answer: Any, *, n: int) -> list[int] | None:
    """The answer's `order` checked against the schema, then de-duplicated
    and range-filtered; None when it is not an answer at all."""
    if not isinstance(answer, dict):
        return None
    try:
        import jsonschema
        jsonschema.validate(answer, TASK_SCHEMA)
    except ImportError:
        if not isinstance(answer.get("order"), list) or not all(isinstance(i, int) for i in answer["order"]):
            return None
    except Exception:
        return None
    out: list[int] = []
    for i in answer["order"]:
        if isinstance(i, int) and 0 <= i < n and i not in out:
            out.append(i)
    return out


def rank_scores(order: list[int], n: int) -> dict[int, float]:
    """Rank position → 0..1 (best 1.0, last ranked 0.0; unranked 0.0)."""
    k = len(order)
    scores = {i: 0.0 for i in range(n)}
    for pos, i in enumerate(order):
        scores[i] = 1.0 if k == 1 else round(1.0 - pos / (k - 1), 4)
    return scores


class Gateway:
    def __init__(self, providers: list[Provider], *, call_timeout_s: float = CALL_TIMEOUT_S,
                 clock: Callable[[], float] = time.monotonic):
        self.providers = list(providers)
        self.call_timeout_s = call_timeout_s
        self._clock = clock

    @property
    def engine_version(self) -> str:
        return "+".join(f"{p.id}@{p.engine_version}" for p in self.providers) or "none"

    def prompt_hash(self, sentences: list[dict]) -> str:
        chunks = [rank_moments_payload(sentences[i: i + MAX_SENTENCES_PER_CALL])
                  for i in range(0, len(sentences), MAX_SENTENCES_PER_CALL)]
        blob = json.dumps({"task": TASK, "chunks": chunks}, sort_keys=True).encode("utf-8")
        return "sha256:" + hashlib.sha256(blob).hexdigest()[:16]

    def _provider(self) -> Provider | None:
        for p in self.providers:
            try:
                if p.probe().get("available"):
                    return p
            except Exception:
                continue
        return None

    def active_provider_id(self) -> str | None:
        """The id of the provider that would answer now (None when none does):
        part of the semantic layer's cache name, so a layer built while Apple
        Intelligence was off is rebuilt once it is on."""
        p = self._provider()
        return None if p is None else p.id

    def _call(self, provider: Provider, payload: dict, timeout_s: float, res: RankResult) -> list[int] | None:
        assert_path_free(payload)
        t0 = self._clock()
        try:
            answer = provider.run(payload, timeout_s=timeout_s)
        except Exception as e:      # a provider must never take the layer down
            answer = None
            res.notes.append(f"{provider.id}: {type(e).__name__}")
        took = self._clock() - t0
        res.calls += 1
        res.spent_s += took
        if took > timeout_s:
            res.notes.append(f"{provider.id}: timeout after {took:.1f}s (cap {timeout_s:.1f}s)")
            return None
        order = validate_answer(answer, n=len(payload["windows"]))
        if order is None:
            res.notes.append(f"{provider.id}: no valid answer")
            return None
        res.provenance = {"by": provider.id, "model": (answer or {}).get("model"), "task": TASK,
                          "engine_version": provider.engine_version}
        return order

    def rank_moments(self, sentences: list[dict], *, budget_s: float = SEMANTIC_BUDGET_S) -> RankResult:
        """Scores by rank for `sentences` (`{id, t0, t1, text, …}`), in chunks
        of ≤ 24, until the budget is spent; the rest stay heuristic."""
        res = RankResult()
        provider = self._provider()
        if provider is None or not sentences:
            # No provider is not a failure: the heuristic scores are complete
            # and the layer is `ok`. `partial` is for a model that started and ran out.
            res.notes.append("no provider available" if sentences else "nothing to rank")
            return res
        prompt_hash = self.prompt_hash(sentences)
        start = self._clock()
        for i in range(0, len(sentences), MAX_SENTENCES_PER_CALL):
            chunk = sentences[i: i + MAX_SENTENCES_PER_CALL]
            remaining = budget_s - (self._clock() - start)
            if remaining <= 0:
                res.notes.append(f"budget of {budget_s:.0f}s spent before sentence {chunk[0]['id']}")
                res.partial = True
                break
            order = self._call(provider, rank_moments_payload(chunk), min(self.call_timeout_s, remaining), res)
            if order is None:
                res.partial = True
                break
            for idx, score in rank_scores(order, len(chunk)).items():
                res.scores[chunk[idx]["id"]] = score
        if res.scores:
            res.provenance["prompt_hash"] = prompt_hash
        else:
            res.provenance = {"by": "recipes"}
            res.partial = True
        return res


class AppleIntelligenceProvider:
    """`fm.py`'s text path, lazily built (never imported in a process that
    does not rank)."""
    id = "apple_intelligence"
    engine_version = "fm-planner-text-v1"

    def __init__(self, brain: Any | None = None):
        self._brain = brain

    def _fm(self):
        if self._brain is None:
            from ..agent.prompt.brains.fm import FMBrain
            self._brain = FMBrain()
        return self._brain

    def probe(self) -> dict:
        try:
            return dict(self._fm().availability())
        except Exception as e:
            return {"available": False, "detail": f"{type(e).__name__}: {e}"}

    def run(self, payload: dict, *, timeout_s: float) -> dict | None:
        from ..agent.prompt.brains.base import TextTask
        result = self._fm().text(TextTask(kind="rank_windows", payload=payload), timeout_s=timeout_s)
        if result is None:
            return None
        return {"order": [int(i) for i in result.items if isinstance(i, int) or str(i).isdigit()],
                "model": result.model, "latency_ms": int(result.latency_ms)}


def default_gateway() -> Gateway:
    return Gateway([AppleIntelligenceProvider()])


__all__ = ["CALL_TIMEOUT_S", "SEMANTIC_BUDGET_S", "TASK", "TASK_SCHEMA", "Provider", "RankResult", "validate_answer",
           "rank_scores", "Gateway", "AppleIntelligenceProvider", "default_gateway"]
