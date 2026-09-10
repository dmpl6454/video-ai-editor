"""Content tasks — where a language model adds value over a grammar (§3.7).

Routing is the grammar's job; WORDS are not. The baseline runs showed the
content-blind hook heuristic rotating canned clickbait ("THEY LIED ABOUT HOW
THIS ACTUALLY WORKS") over a camera review (findings 3 and 9). This module
asks the brains for text and, when none can answer, falls back to a
transcript-DERIVED line — the first real claim in the speaker's own words,
fillers stripped, capped at seven words — never a canned template.

Two tasks:
  hook_candidates(transcript_head, n=3, max_words=7) → [str]
  rank_windows(windows=[{start,end,text}], k)        → [int] window indices

Everything a model returns is sanitised before a recipe sees it:
strings only, one line, ≤ `max_words`, ≤ `HOOK_MAX_CHARS` (the
`apply_hook_stack.text` bound in §1.3 rule 8), deduplicated; indices must be
in range and unique. `plan.content_brain` records which brain wrote the text
so the badge can say "Recipes · text by Apple Intelligence".
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable

from ..facts import TimelineFacts
from ..recipes import FILLERS_STRICT, heuristic_hook
from ..schema import IntentDraft, Plan
from .base import BRAIN_LABELS, Brain, TextResult, TextTask
from .jsonfix import JsonRepairFailed, repair

__all__ = ["HOOK_MAX_CHARS", "HOOK_MAX_WORDS", "HOOK_TOOL", "HOOK_RECIPE", "HookText", "RankResult",
           "hook_candidates_task", "rank_windows_task", "sanitize_hook_items", "sanitize_ranking",
           "needs_hook_text", "strip_model_hook_text", "parse_text_items", "text_task_prompts",
           "heuristic_hook_candidates", "hook_text", "rank_windows", "HEURISTIC_SOURCE"]

HOOK_TOOL = "apply_hook_stack"
HOOK_RECIPE = "hook"


def strip_model_hook_text(draft: IntentDraft, prompt: str) -> IntentDraft:
    """Drop a `hook.text` slot the MODEL wrote — one that is not the user's
    own words (a quoted or literal substring of the prompt).

    WHY (real 7B run, 2026-09-10): asked for "a hook at the start", Qwen
    filled `text` with "Get ready for an amazing story!" — the planning
    prompt carries no transcript words, so a line written there can only be
    generic, exactly the canned clickbait findings 3 and 9 caught. Without
    the slot the recipe falls back to the heuristic, `needs_hook_text` is
    true, and the router's content pass asks the same brain WITH the
    transcript head — words grounded in what the speaker says, attributed to
    the brain that wrote them."""
    haystack = re.sub(r"\s+", " ", prompt or "").lower()
    items = []
    changed = False
    for it in draft.intents:
        text = it.slots.get("text") if it.recipe == HOOK_RECIPE else None
        if isinstance(text, str) and text.strip() and _one_line(text).lower() not in haystack:
            items.append(it.model_copy(update={"slots": {k: v for k, v in it.slots.items() if k != "text"}}))
            changed = True
        else:
            items.append(it)
    return draft.model_copy(update={"intents": items}) if changed else draft


def needs_hook_text(plan: Plan, facts: TimelineFacts) -> bool:
    """True when the plan carries a hook whose text is exactly the recipe
    table's transcript heuristic — the one case a content brain improves on.

    WHY compare against `recipes.heuristic_hook` instead of reading the
    recipe's reply note: the note is prose P may reword; the text the step
    carries is the contract. A hook the user typed (`"add a hook that says
    'X'"`) never equals the heuristic, a hook a brain already wrote sets
    `content_brain`, and with no transcript there are no words for any
    brain to ground a line in — so all three answer False."""
    if plan.content_brain is not None or not (facts.transcript_head or "").strip():
        return False
    guess = heuristic_hook(facts.transcript_head)
    if not guess:
        return False
    return any(s.tool == HOOK_TOOL and str(s.args.get("text", "")).strip() == guess for s in plan.steps)

HOOK_MAX_CHARS = 60          # apply_hook_stack.text ≤ 60 (§1.3 rule 8)
HOOK_MAX_WORDS = 7
HEURISTIC_SOURCE = "heuristic (no local model available)"

#: Words that open a sentence without saying anything; stripped before a
#: sentence is judged or shown.
_OPENERS = frozenset({"so", "okay", "ok", "well", "hi", "hey", "hello", "today", "now", "alright",
                      "right", "yeah", "basically", "actually", "welcome", "guys", "everyone", "and",
                      "but", "um", "uh", "like"})
_FILLERS = frozenset(FILLERS_STRICT)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_WORD = re.compile(r"[A-Za-z0-9'ऀ-ॿ][A-Za-z0-9'\-ऀ-ॿ]*")


def hook_candidates_task(transcript_head: str, *, n: int = 3, max_words: int = HOOK_MAX_WORDS) -> TextTask:
    return TextTask(kind="hook_candidates",
                    payload={"transcript_head": (transcript_head or "")[:1500], "n": n, "max_words": max_words})


def rank_windows_task(windows: list[dict[str, Any]], k: int) -> TextTask:
    clean = [{"start": float(w.get("start", 0.0)), "end": float(w.get("end", 0.0)),
              "text": str(w.get("text", ""))[:300]} for w in windows]
    return TextTask(kind="rank_windows", payload={"windows": clean, "k": int(k)})


# --- sanitising model output ---------------------------------------------------

#: Discourse connectors a model copies from the transcript's list structure
#: ("Second, the battery…"): meaningless on a title card, so they go.
_LEADING_CONNECTOR = re.compile(r"^(?:first(?:ly)?|second(?:ly)?|third(?:ly)?|next|also|and|so|then|finally)[,:]?\s+",
                                re.I)


def _one_line(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip().strip("\"'`“”‘’")
    text = re.sub(r"^\d+[.)]\s*", "", text)          # "1. HOOK" → "HOOK"
    text = _LEADING_CONNECTOR.sub("", text)
    return text.strip()


def _cap_words(text: str, max_words: int) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]).rstrip(",;:—-")


def sanitize_hook_items(items: Iterable[Any], *, max_words: int = HOOK_MAX_WORDS,
                        max_chars: int = HOOK_MAX_CHARS) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items or ():
        if not isinstance(item, str):
            continue
        line = _cap_words(_one_line(item), max_words)
        if len(line) > max_chars:
            line = line[:max_chars].rsplit(" ", 1)[0].rstrip(",;:—-") or line[:max_chars]
        key = line.lower()
        if len(line) < 3 or key in seen:
            continue
        seen.add(key)
        out.append(line)
    return out


def sanitize_ranking(items: Iterable[Any], *, n_windows: int, k: int) -> list[int]:
    out: list[int] = []
    for item in items or ():
        if isinstance(item, bool) or not isinstance(item, (int, float, str)):
            continue
        try:
            idx = int(item)
        except (TypeError, ValueError):
            continue
        if 0 <= idx < n_windows and idx not in out:
            out.append(idx)
        if len(out) >= k:
            break
    return out


# --- what a free-text model is asked, and how its answer is read ----------------

def text_task_prompts(task: TextTask) -> tuple[str, str] | None:
    """`(system, user)` for the brains that prompt in free text (MLX, Claude).
    The FM helper has its own typed `@Generable` shapes. None for a task the
    brain cannot serve (empty payload or unknown kind)."""
    if task.kind == "hook_candidates":
        head = str(task.payload.get("transcript_head") or "").strip()
        if not head:
            return None
        n = max(1, min(int(task.payload.get("n") or 3), 6))
        words = max(3, min(int(task.payload.get("max_words") or HOOK_MAX_WORDS), 12))
        # WHY the literal example: with only a schema-ish hint the real 7B
        # answered `{"items": ["a"], ["b"], ["c"]}` (each line in its own
        # list) — unparseable. A worked example fixes the shape; the
        # quoted-string fallback in `parse_text_items` covers the rest.
        example = json.dumps({"items": [f"hook line {i + 1}" for i in range(n)]})
        system = (f"You write short-form video hooks. Propose {n} hook lines, each at most {words} words, "
                  "grounded in what the speaker actually says — a claim, a number, a surprise from the "
                  "transcript. No generic filler like 'wait for it' or 'get ready'. "
                  f"Reply with ONE JSON object exactly like {example} and nothing else.")
        return system, f"Transcript start:\n{head}"
    if task.kind == "rank_windows":
        windows = task.payload.get("windows") or []
        if not windows:
            return None
        k = max(1, min(int(task.payload.get("k") or len(windows)), len(windows)))
        listing = "\n".join(f"[{i}] {w.get('text', '')}" for i, w in enumerate(windows))
        system = ("You pick the most self-contained, engaging moments of a talk for short clips. "
                  f"Return the {k} best window indices, best first, each index at most once. "
                  'Reply with ONE JSON object exactly like {"items": [0, 3, 1]} and nothing else.')
        return system, f"Windows:\n{listing}"
    return None


_QUOTED = re.compile(r'"((?:[^"\\\n]|\\.){3,120})"')
_INDEX_LIST = re.compile(r"\[([\d\s,]+)\]")


def parse_text_items(out: str, kind: str) -> list[Any] | None:
    """The `items` of a free-text answer. `jsonfix.repair` first; when even
    that fails, the strings (or the integers) are still recoverable from a
    mis-bracketed answer, and `sanitize_*` vets every one of them."""
    try:
        items = repair(out).get("items")
        if isinstance(items, list):
            return items
    except JsonRepairFailed:
        pass
    if kind == "hook_candidates":
        strings = [m.group(1) for m in _QUOTED.finditer(out or "")]
        return [s for s in strings if s.strip().lower() not in ("items",)] or None
    if kind == "rank_windows":
        m = _INDEX_LIST.search(out or "")
        return [int(x) for x in m.group(1).replace(",", " ").split()] if m else None
    return None


# --- the transcript-derived fallback -------------------------------------------

def _content_words(sentence: str) -> list[str]:
    words = [w for w in _WORD.findall(sentence)]
    while words and words[0].lower().strip("'") in _OPENERS:
        words = words[1:]
    return [w for w in words if w.lower().strip("'") not in _FILLERS]


def _sentence_score(words: list[str]) -> float:
    """Prefer a sentence with a claim in it: a number, a question word, a
    comparative, or an imperative — over a pleasantry."""
    if len(words) < 3:
        return -1.0
    text = " ".join(words).lower()
    score = min(len(words), 8) / 8.0
    if re.search(r"\d", text):
        score += 1.0
    if re.search(r"\b(why|how|what|never|always|secret|mistake|wrong|best|worst|only|stop)\b", text):
        score += 0.8
    if re.search(r"\b(than|most|every|nobody|everyone)\b", text):
        score += 0.4
    if re.search(r"\b(thanks? for|subscribe|my name is|in this video|welcome back)\b", text):
        score -= 1.5
    return score


def heuristic_hook_candidates(transcript_head: str, *, n: int = 3,
                              max_words: int = HOOK_MAX_WORDS) -> list[str]:
    """Hook lines from the speaker's own words: the strongest early sentence
    first, then the first sentence, then the strongest sentence trimmed to
    its first clause. Upper-cased like `generate_hook`'s heuristic, but with
    fillers and throat-clearing removed. Empty transcript → []."""
    sentences = [s for s in _SENTENCE_SPLIT.split(transcript_head or "") if s.strip()][:12]
    scored = []
    for order, sentence in enumerate(sentences):
        words = _content_words(sentence)
        score = _sentence_score(words) - order * 0.05
        if words:
            scored.append((score, order, words))
    if not scored:
        return []
    best = max(scored, key=lambda t: t[0])
    first = min(scored, key=lambda t: t[1])
    clause = re.split(r"[,;:—]| but | because | which ", " ".join(best[2]), maxsplit=1)[0].split()
    raw = [" ".join(best[2]), " ".join(first[2]), " ".join(clause)]
    raw += [" ".join(w) for s, o, w in sorted(scored, reverse=True) if (s, o, w) not in (best, first)]
    return sanitize_hook_items((line.upper() for line in raw), max_words=max_words)[:n]


# --- asking the brains -------------------------------------------------------------

@dataclass(frozen=True)
class HookText:
    text: str
    brain: str            # BRAIN id that wrote it; "recipes" for the heuristic
    source: str           # human note for the reply, e.g. "hook text: Apple Intelligence"
    candidates: tuple[str, ...] = ()

    @property
    def content_brain(self) -> str | None:
        """What goes in `plan.content_brain`: None when no model wrote it."""
        return None if self.brain == "recipes" else self.brain


@dataclass(frozen=True)
class RankResult:
    order: tuple[int, ...]
    brain: str


def _ask(brains: Iterable[Brain], task: TextTask, *, timeout_s: float) -> TextResult | None:
    for brain in brains:
        try:
            if not brain.availability().get("available"):
                continue
            result = brain.text(task, timeout_s=timeout_s)
        except Exception:      # a content task must never take the plan down
            continue
        if result is not None and result.items:
            return result
    return None


def hook_text(brains: Iterable[Brain], transcript_head: str, *, n: int = 3,
              max_words: int = HOOK_MAX_WORDS, timeout_s: float = 6.0) -> HookText:
    """The hook line for `apply_hook_stack(text=…)`: the first sanitised
    candidate from the first brain that answers, else the transcript
    heuristic. Never raises; never returns an empty string."""
    task = hook_candidates_task(transcript_head, n=n, max_words=max_words)
    result = _ask(brains, task, timeout_s=timeout_s)
    if result is not None:
        items = sanitize_hook_items(result.items, max_words=max_words)
        if items:
            label = BRAIN_LABELS.get(result.brain, result.brain)
            return HookText(text=items[0], brain=result.brain, source=f"hook text: {label}",
                            candidates=tuple(items))
    items = heuristic_hook_candidates(transcript_head, n=n, max_words=max_words)
    text = items[0] if items else "WATCH THIS"
    return HookText(text=text, brain="recipes", source=f"hook text: {HEURISTIC_SOURCE}",
                    candidates=tuple(items))


def rank_windows(brains: Iterable[Brain], windows: list[dict[str, Any]], k: int, *,
                 timeout_s: float = 6.0) -> RankResult:
    """Ordered window indices, best first. Fallback = the windows' own order
    (what `make_shorts` would do anyway), attributed to `recipes`."""
    k = max(1, min(int(k), len(windows))) if windows else 0
    if not windows:
        return RankResult(order=(), brain="recipes")
    result = _ask(brains, rank_windows_task(windows, k), timeout_s=timeout_s)
    if result is not None:
        order = sanitize_ranking(result.items, n_windows=len(windows), k=k)
        if order:
            return RankResult(order=tuple(order), brain=result.brain)
    return RankResult(order=tuple(range(k)), brain="recipes")
