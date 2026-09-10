"""The `claude` brain — the only one that may emit raw tool steps (spec §1.2).

Gated twice: `ANTHROPIC_API_KEY` must be set AND `VAI_PROMPT_CLOUD != "0"`.
Without a key it is a greyed-out rung with the fix "Add ANTHROPIC_API_KEY";
nothing else in the prompt flow ever needs it (a stated hard rule).

One `messages.create` with the forced tool `emit_plan`, whose `input_schema`
is `PLAN_JSON_SCHEMA` minus the fields the router/planner own
(`schema.cloud_plan_input_schema()`). The answer is a Plan the router still
runs through `validate_plan` — a cloud model is a model, not a trusted
caller. The `anthropic` SDK is imported lazily so the module compiles on a
machine without it configured.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Callable

from pydantic import ValidationError

from .... import config
from ..schema import CLOUD_PLAN_STRIPPED_FIELDS, PLAN_DENY, Plan, cloud_plan_input_schema
from .base import (Availability, BrainRequest, BrainResult, TextResult, TextTask, ToolCard, available,
                   unavailable)
from .content import parse_text_items, text_task_prompts
from .prompt_text import facts_to_prompt_block, user_prompt_with_answers, recipe_cards_block

__all__ = ["CloudBrain", "CLOUD_ENV", "EMIT_PLAN_TOOL", "cloud_allowed", "default_tool_cards"]

CLOUD_ENV = "VAI_PROMPT_CLOUD"
KEY_FIX = "Add ANTHROPIC_API_KEY to .env and restart to enable Claude"
DISABLED_FIX = f"Unset {CLOUD_ENV}=0 to allow Claude"

EMIT_PLAN_TOOL: dict[str, Any] = {
    "name": "emit_plan",
    "description": ("Emit the edit plan for the user's request. Prefer `recipe:<name>` steps "
                    "(they expand into verified tool sequences); use raw tools only for what no "
                    "recipe covers. Never put a file path in any argument."),
    "input_schema": cloud_plan_input_schema(),
}


def cloud_allowed(env: dict[str, str] | None = None) -> bool:
    return (env if env is not None else os.environ).get(CLOUD_ENV, "1") != "0"


def default_tool_cards() -> list[ToolCard]:
    """Every allow-listed tool as the `claude` brain sees it (name, one line,
    `input_schema`). Imported lazily: `tools.py` pulls in the whole dispatch
    surface, and on-device brains never need it."""
    from ... import tools as _tools
    cards = [ToolCard(name=t["name"], description=t["description"].split(". ")[0][:200],
                      input_schema=t["input_schema"])
             for t in _tools.list_tools() if t["name"] not in PLAN_DENY]
    try:
        from ..validate import EXTRA_TOOL_SCHEMAS       # P's (§1.3 rule 3)
    except ImportError:
        EXTRA_TOOL_SCHEMAS = {}
    for name, schema in EXTRA_TOOL_SCHEMAS.items():
        if name not in {c.name for c in cards}:
            cards.append(ToolCard(name=name, description="Composite hook: text + punch-in + audio boost",
                                  input_schema=schema))
    return cards


def _system_prompt(req: BrainRequest, tool_cards: list[ToolCard]) -> str:
    tools_block = "\n".join(f"- {c.name}: {c.description} args={json.dumps(list((c.input_schema.get('properties') or {}).keys()))}"
                            for c in tool_cards)
    return (
        "You plan edits for a local video editor. Answer ONLY by calling emit_plan.\n"
        "Prefer `recipe:<name>` steps — each expands into a verified tool sequence with "
        "prerequisites and postconditions. Raw tools are for what no recipe covers.\n"
        f"Recipes:\n{recipe_cards_block(list(req.recipes))}\n"
        f"Tools (raw steps):\n{tools_block or '- (none offered)'}\n"
        "Rules: never invent a tool or argument; never write a file path — ask with needs_input "
        "kind=path instead; tiktok/reels/shorts/story are 9:16; youtube is 16:9; put what the user "
        "said NOT to do in a `why` and leave it out; for `recipe:hook` write the `text` slot yourself "
        "from what the speaker says (at most seven words, no generic clickbait); confidence is 0..1; "
        "reply is one sentence."
    )


def _tool_cards_for(req: BrainRequest) -> list[ToolCard]:
    """The raw-tool cards this brain may use. WHY the fallback: the chat
    service builds `BrainRequest(prompt, facts, recipes)` without cards
    (only this rung reads them, and loading them pulls in the whole dispatch
    surface), so an empty list means "not supplied", not "offer nothing"."""
    return list(req.tool_cards) if req.tool_cards else default_tool_cards()


class CloudBrain:
    id = "claude"

    def __init__(self, *, api_key: str | None = None, allowed: bool | None = None,
                 model: str | None = None, client_factory: Callable[[str], Any] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self._key = config.ANTHROPIC_API_KEY if api_key is None else api_key
        self._allowed = cloud_allowed() if allowed is None else allowed
        self._model = model or config.CLAUDE_MODEL
        self._client_factory = client_factory or self._default_client
        self._clock = clock

    @staticmethod
    def _default_client(api_key: str) -> Any:
        from anthropic import Anthropic
        return Anthropic(api_key=api_key)

    def availability(self) -> Availability:
        if not self._key:
            return unavailable("no ANTHROPIC_API_KEY", KEY_FIX, action="add_key", model=self._model)
        if not self._allowed:
            return unavailable(f"disabled by {CLOUD_ENV}=0", DISABLED_FIX, model=self._model)
        return available(f"{self._model} via the Anthropic API (needs network)", model=self._model)

    def plan(self, req: BrainRequest, *, timeout_s: float) -> BrainResult:
        if not self.availability()["available"]:
            return BrainResult.failure(self.id, "unavailable", model=self._model)
        t0 = self._clock()
        user = (f"{user_prompt_with_answers(req.prompt, req.prior_clarification)}\n\n"
                f"Timeline: {facts_to_prompt_block(req.facts)}")
        try:
            client = self._client_factory(self._key)
            resp = client.messages.create(
                model=self._model, max_tokens=2048, system=_system_prompt(req, _tool_cards_for(req)),
                tools=[EMIT_PLAN_TOOL], tool_choice={"type": "tool", "name": "emit_plan"},
                messages=[{"role": "user", "content": user}], timeout=timeout_s)
        except Exception as e:
            return BrainResult.failure(self.id, _reason_for(e), latency_ms=_ms(self._clock(), t0),
                                       model=self._model)
        latency = _ms(self._clock(), t0)
        raw = next((dict(b.input) for b in getattr(resp, "content", []) or ()
                    if getattr(b, "type", "") == "tool_use" and getattr(b, "name", "") == "emit_plan"), None)
        if raw is None:
            return BrainResult.failure(self.id, "parse", latency_ms=latency, model=self._model)
        # The model must not author these; drop them rather than fail on them.
        cleaned = {k: v for k, v in raw.items() if k not in CLOUD_PLAN_STRIPPED_FIELDS}
        try:
            plan = Plan.model_validate({**cleaned, "version": 1, "id": Plan.new_id(), "brain": self.id})
        except ValidationError as e:
            first = e.errors()[0] if e.errors() else {}
            where = ".".join(str(p) for p in first.get("loc", ()))
            return BrainResult.failure(self.id, f"rejected:{where}: {first.get('msg', 'invalid')}"[:160],
                                       latency_ms=latency, model=self._model)
        return BrainResult(plan=plan, brain=self.id, ok=True, latency_ms=latency, model=self._model)

    def text(self, task: TextTask, *, timeout_s: float) -> TextResult | None:
        if not self.availability()["available"]:
            return None
        prompts = text_task_prompts(task)
        if prompts is None:
            return None
        system, user = prompts
        t0 = self._clock()
        try:
            client = self._client_factory(self._key)
            resp = client.messages.create(model=self._model, max_tokens=300, timeout=timeout_s, system=system,
                                          messages=[{"role": "user", "content": user}])
            out = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
        except Exception:
            return None
        items = parse_text_items(out, task.kind)
        if not items:
            return None
        return TextResult(items=items, brain=self.id, model=self._model, latency_ms=_ms(self._clock(), t0))


def _ms(now: float, t0: float) -> int:
    return int((now - t0) * 1000)


def _reason_for(e: Exception) -> str:
    """SDK exceptions → the `REASONS` vocabulary, without importing the SDK
    at module load (a class-name match is enough and survives SDK renames)."""
    name = type(e).__name__
    if "Timeout" in name:
        return "timeout"
    if "RateLimit" in name or "Overloaded" in name:
        return "busy"
    if any(k in name for k in ("Authentication", "PermissionDenied", "Connection", "APIStatus", "BadRequest")):
        return "unavailable"
    return "decode"
