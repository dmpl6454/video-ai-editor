"""What the language-model brains are told, and how their answer becomes an
`IntentDraft` (spec §3.1, §3.3).

Three rules, each with a baseline finding behind it:

  * **Recipe cards, never tool schemas.** Given raw tool names the 7B model
    invented every argument name (finding 12); given recipe cards with slot
    names it emitted zero unknown args (finding 13). The cards come from
    `recipes.cards()`; nothing here hard-codes a recipe.
  * **Facts as a ≤ 400-char block of basenames.** A path in the prompt is a
    path in the answer (finding 13: `/path/to/upbeat_music.mp3`), and the
    model never needs one — the `music` recipe resolves uploads by name.
  * **Tolerant normalisation, strict validation elsewhere.** `normalize_draft`
    folds the shapes a small model actually emits (flat slot keys, list
    values, string numbers) into `IntentDraft`; the security boundary is
    `validate_plan` on the expanded Plan, so being strict here would only
    turn a usable answer into a fall-through.
"""
from __future__ import annotations

import json
import re
from pathlib import PurePath
from typing import Any

from ..facts import TimelineFacts
from ..recipes import RecipeCard
from ..schema import IntentDraft

__all__ = ["FACTS_BLOCK_MAX_CHARS", "UNSTATED_CONFIDENCE", "DraftShapeError", "facts_to_prompt_block",
           "contains_devanagari", "recipe_cards_block", "INTENT_DRAFT_COMPACT_SCHEMA", "PLAN_RULES",
           "mlx_system_prompt", "user_prompt_with_answers", "normalize_draft", "flatten_fm_item", "draft_key"]


class DraftShapeError(TypeError):
    """The model's object is not even tolerably an IntentDraft (e.g. `intents`
    is a string). The adapters report `parse` — a fall-through, never a plan
    with zero steps that would read as "nothing to do"."""

FACTS_BLOCK_MAX_CHARS = 400

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")


def contains_devanagari(text: str) -> bool:
    return bool(_DEVANAGARI.search(text or ""))


def _basename(value: str) -> str:
    return PurePath(str(value)).name


def facts_to_prompt_block(facts: TimelineFacts) -> str:
    """One compact line about the timeline: durations, aspect, transcript
    state, what is already on it, and the uploads BY BASENAME ONLY. Always
    ≤ `FACTS_BLOCK_MAX_CHARS`; never a path."""
    parts: list[str] = [
        f"{facts.duration:.0f}s {facts.aspect} {facts.canvas_w}x{facts.canvas_h} {facts.fps}fps",
        f"{len(facts.v1_clip_ids)} clip{'s' if len(facts.v1_clip_ids) != 1 else ''} on v1",
    ]
    if facts.has_transcript:
        lang = facts.language or "?"
        parts.append(f"transcript {lang} {facts.words} words, speech {facts.speech_seconds:.0f}s, "
                     f"{facts.filler_count} fillers")
    elif facts.transcript_pending:
        parts.append("transcript pending")
    else:
        parts.append("no transcript")
    parts.append(("captions " + (facts.caption_style or "on")) if facts.has_captions else "no captions")
    parts.append(("music" + (" ducked" if facts.music_ducked else "")) if facts.has_music else "no music")
    if facts.brand_handle:
        parts.append(f"brand {facts.brand_handle}")
    if facts.selection:
        parts.append("a clip is selected")
    uploads = [_basename(u) for u in (*facts.uploads_audio, *facts.uploads_images)]
    if uploads:
        parts.append("uploads: " + ", ".join(uploads[:4]) + (" …" if len(uploads) > 4 else ""))
    block = "; ".join(parts)
    if len(block) > FACTS_BLOCK_MAX_CHARS:
        block = block[:FACTS_BLOCK_MAX_CHARS - 1].rstrip() + "…"
    return block


def recipe_cards_block(cards: list[RecipeCard]) -> str:
    lines = []
    for card in cards:
        slots = []
        for slot, kind in card.slots.items():
            values = card.slot_values.get(slot)
            slots.append(f"{slot}={'|'.join(values)}" if values else f"{slot}:{kind}")
        suffix = f" (slots: {', '.join(slots)})" if slots else ""
        lines.append(f"- {card.name}: {card.description}{suffix}")
    return "\n".join(lines)


INTENT_DRAFT_COMPACT_SCHEMA = (
    '{"intents":[{"recipe":"<name>","slots":{"<slot>":"<value>"}}],'
    '"exclusions":["<thing not to do>"],'
    '"needs_input":[{"key":"<slot>","question":"<one line>","options":["<a>","<b>"],"default":null}],'
    '"confidence":0.0,"reply":"<one sentence>"}'
)

PLAN_RULES = (
    "Rules:\n"
    "- Use ONLY the recipe names listed; never invent one.\n"
    "- Order intents the way the edits should happen.\n"
    "- Set a slot only when the user said it; otherwise leave it out.\n"
    "- tiktok, reels, shorts and story are 9:16 platforms; youtube is 16:9.\n"
    "- Anything the user said NOT to do goes in exclusions.\n"
    "- Ask a needs_input question only when a required value is missing.\n"
    "- confidence is how sure you are, 0 to 1 — 0.9 when the request is clear.\n"
    "- Answer with the JSON object only — no prose, no code fence."
)


def mlx_system_prompt(cards: list[RecipeCard], facts_block: str) -> str:
    return (
        "You turn one video-editing request into an ordered list of recipes.\n"
        f"Recipes:\n{recipe_cards_block(cards)}\n"
        f"Timeline: {facts_block}\n"
        f"{PLAN_RULES}\n"
        f"Reply with exactly this JSON shape: {INTENT_DRAFT_COMPACT_SCHEMA}"
    )


def user_prompt_with_answers(prompt: str, prior_clarification: dict | None) -> str:
    if not prior_clarification:
        return prompt
    answered = ", ".join(f"{k}={v}" for k, v in prior_clarification.items())
    return f"{prompt}\n(Already answered: {answered})"


def draft_key(prompt: str, prior_clarification: dict | None) -> str:
    """Identity of one planning request for the adapters' one-entry draft
    cache: the router's hook-text pass (§3.7) re-asks the SAME brain with
    `BrainRequest.hook_text` set, and the answer must come from the draft it
    already produced — not from a second generation."""
    return json.dumps([prompt, prior_clarification or {}], sort_keys=True, ensure_ascii=False, default=str)


# --- normalisation -----------------------------------------------------------

_NON_SLOT_KEYS = frozenset({"recipe", "slots", "why", "reason", "name_of_recipe"})
_FM_SLOT_FIELDS = ("style", "target", "ratio", "platform", "mood", "look", "count", "duration_s",
                   "factor", "text", "handle", "name", "lufs", "words")


def _scalar(value: Any) -> str | float | int | bool | None:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return ", ".join(str(v) for v in value if v is not None) or None
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def flatten_fm_item(item: dict[str, Any]) -> dict[str, Any]:
    """The FM helper's typed `IntentItem` (non-nil fields) → `{recipe, slots}`."""
    slots = {k: _scalar(item[k]) for k in _FM_SLOT_FIELDS if item.get(k) is not None}
    return {"recipe": item.get("recipe"), "slots": slots}


def _normalize_item(item: Any) -> dict[str, Any] | None:
    if isinstance(item, str):
        return {"recipe": item.strip().lower(), "slots": {}}
    if not isinstance(item, dict):
        return None
    recipe = item.get("recipe") or item.get("name") or item.get("tool")
    if not isinstance(recipe, str):
        return None
    raw_slots = item.get("slots") if isinstance(item.get("slots"), dict) else {}
    # Flat keys next to `recipe` are slots the model forgot to nest.
    flat = {k: v for k, v in item.items() if k not in _NON_SLOT_KEYS and k not in ("name", "tool")}
    if "slots" not in item and "name" in item and recipe != item.get("name"):
        flat["name"] = item["name"]
    merged = {**flat, **raw_slots}
    slots = {str(k): _scalar(v) for k, v in merged.items() if v is not None and str(k).strip()}
    return {"recipe": recipe.strip().lower().replace("-", "_").replace(" ", "_"), "slots": slots}


def _normalize_question(q: Any) -> dict[str, Any] | None:
    if not isinstance(q, dict):
        return None
    key = q.get("key") or q.get("slot")
    question = q.get("question") or q.get("text")
    if not isinstance(key, str) or not isinstance(question, str):
        return None
    options = q.get("options") or []
    if not isinstance(options, list):
        options = [options]
    default = q.get("default", q.get("default_value"))
    return {"key": re.sub(r"[^a-z_]", "_", key.strip().lower())[:32] or "value",
            "question": question.strip()[:200],
            "options": [str(o) for o in options if o is not None][:12],
            "default": _scalar(default)}


#: Confidence assumed when the model produced intents but left `confidence`
#: absent or 0. WHY (pre-flight, 2026-09-08): Qwen2.5-7B-4bit returned
#: `confidence: 0.0` on 12 of the 24 benchmark prompts while the intents were
#: right (23/24 valid); the router's ≥ 0.5 rule would have thrown half of
#: them away for a field the model never reasons about. 0.7 clears the gate
#: without claiming the ≥ 0.75 "recipes were sure" level.
UNSTATED_CONFIDENCE = 0.7


def _confidence(raw: Any, *, has_intents: bool) -> float:
    try:
        value = float(raw) if raw is not None else 0.0
    except (TypeError, ValueError):
        value = 0.0
    if value <= 0.0:
        return UNSTATED_CONFIDENCE if has_intents else 0.0
    return min(1.0, value)


def normalize_draft(obj: dict[str, Any]) -> IntentDraft:
    """A model's JSON object → `IntentDraft`, tolerating the shapes small
    models emit. Raises `pydantic.ValidationError` when even the tolerant
    shape does not hold (no intents list at all)."""
    if not isinstance(obj, dict):
        raise DraftShapeError(f"draft must be an object, got {type(obj).__name__}")
    intents_raw = obj.get("intents")
    if intents_raw is None:
        intents_raw = obj.get("steps") or obj.get("recipes") or obj.get("plan") or []
    if isinstance(intents_raw, dict):
        intents_raw = [intents_raw]
    if not isinstance(intents_raw, list):
        # WHY not "no intents": iterating a string would yield one bogus
        # single-letter recipe per character, and an empty list would become
        # a valid "nothing to do" plan — both hide that the model failed.
        raise DraftShapeError(f"intents must be a list, got {type(intents_raw).__name__}")
    intents = [n for n in (_normalize_item(i) for i in intents_raw) if n]
    if intents_raw and not intents:
        # The model DID name edits, just in no readable shape — a fall-through,
        # not "nothing to do" (an empty `intents` list is the model's own
        # honest answer and passes).
        raise DraftShapeError("no readable intent in the model's list")
    # Dedupe identical intents — the small models repeat steps (baseline).
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for it in intents:
        sig = json.dumps(it, sort_keys=True, ensure_ascii=False)
        if sig not in seen:
            seen.add(sig)
            unique.append(it)
    exclusions = obj.get("exclusions") or []
    if isinstance(exclusions, str):
        exclusions = [exclusions]
    questions = [n for n in (_normalize_question(q) for q in (obj.get("needs_input") or [])) if n]
    confidence = _confidence(obj.get("confidence"), has_intents=bool(unique))
    reply = obj.get("reply") or obj.get("summary") or ""
    return IntentDraft.model_validate({
        "intents": unique,
        "exclusions": [str(e) for e in exclusions if e is not None][:8],
        "needs_input": questions[:4],
        "confidence": confidence,
        "reply": str(reply)[:400],
    })
