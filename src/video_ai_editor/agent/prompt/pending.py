"""Pending clarification: pause a plan on a required question, resume it on
an answer (spec §4.3).

A paused plan lives in `<session>/prompt_pending.json` with a token, the
plan, the prompt, a hash of the facts it was planned against and an expiry.
Three things can end it: an answer through `POST …/prompt/answer`, a
whole-message answer typed as the next chat/prompt message, or invalidation
— it expired, or another route committed an `op` (the facts hash no longer
matches, so the plan describes a timeline that is gone).

THE WHOLE-MESSAGE RULE. `try_parse_answer` matches the entire message after
normalisation (trim, strip punctuation, lower-case, Hinglish yes/no) against
an option's `value`, `label` or `synonyms`, an ordinal ("first", "2nd", "the
second one"), yes/no/go/skip/download for `confirm` kinds, or a number /
duration for `number` / `duration` kinds. Anything else DROPS the pending
plan and plans the new message fresh. A substring never counts: "hi" inside
"make it hi-res" is a new request, not the Hindi option — a paused question
must never hijack the next thing the user actually asked for.

HOW ANSWERS BIND TO THE PLAN. A question's `key` is a slot name; the
planners (P's recipes) either leave the corresponding step argument unset or
write the placeholder `"$ask:<key>"` into whichever args the answer fills
(`add_music.src ← music_src`, `cut_range.start/end ← range`,
`apply_export_preset.name ← platform`). So:
  * every `"$ask:<key>"` value anywhere in a step's args becomes the answer
    (a `range` answer is parsed through the slot extractor into start/end);
  * a step arg named `key` that is `None` (or missing where the tool takes
    it) receives the value;
  * `downloads` answered no/skip removes the steps whose tool is listed in
    `plan.downloads_needed` and notes it in `reply`;
  * `go` answered no cancels the run (an empty plan with a reply);
  * a `choice` answered `abort` cancels; `skip` drops the steps that would
    have consumed the key.
The resumed plan is re-validated by the executor before anything runs.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from .facts import TimelineFacts
from .recipes import ASK, consumes_answer, dropped_note, is_blank_answer, reask, reask_note, was_reasked
from .schema import NeedsInput, Plan
from .service import CLARIFY_TTL_S, PENDING_FILE

_ORDINALS: dict[str, int] = {
    "first": 0, "1st": 0, "one": 0, "second": 1, "2nd": 1, "two": 1, "third": 2, "3rd": 2,
    "three": 2, "fourth": 3, "4th": 3, "four": 3, "fifth": 4, "5th": 4, "five": 4,
    "sixth": 5, "6th": 5, "six": 5,
}
_YES = frozenset({"yes", "y", "yeah", "yep", "sure", "ok", "okay", "go", "start", "do it",
                  "download", "haan", "ha", "ji", "theek hai", "confirm", "proceed"})
_NO = frozenset({"no", "n", "nope", "skip", "cancel", "stop", "dont", "do not", "nahi", "nahin",
                 "na", "abort", "never mind", "nevermind"})
_DURATION_RE = re.compile(
    r"^(\d+(?:\.\d+)?)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes)?$")
# Punctuation to drop — except a `.` BETWEEN digits, which is a decimal
# point ("1.5m" must stay 1.5 minutes, not become 15 and overshoot `max`).
_STRIP_RE = re.compile(r"(?<!\d)\.|\.(?!\d)|[\"'“”‘’,!?;:()\[\]]+")
_WRAPPERS = ("the ", "option ", "go with ", "use ", "make it ", "i want ", "i'd like ", "lets do ",
             "let's do ", "pick ")
_TRAILERS = (" one", " please", " pls", " option")


def normalise_answer(message: str) -> str:
    text = _STRIP_RE.sub("", message or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    changed = True
    while changed:
        changed = False
        for w in _WRAPPERS:
            if text.startswith(w) and len(text) > len(w):
                text = text[len(w):].strip()
                changed = True
        for t in _TRAILERS:
            if text.endswith(t) and len(text) > len(t):
                text = text[: -len(t)].strip()
                changed = True
    return text


def facts_hash(facts: TimelineFacts) -> str:
    """What a pending plan was planned against: the parts of the facts that
    another route's `op` would change. Selection/playhead are deliberately
    excluded — moving the playhead must not drop a question."""
    payload = {
        "duration": round(facts.duration, 3), "canvas": [facts.canvas_w, facts.canvas_h],
        "clips": list(facts.clip_ids), "tracks": list(facts.track_ids),
        "captions": facts.has_captions, "music": facts.has_music, "transcript": facts.has_transcript,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def pending_path(session_dir: Path) -> Path:
    return Path(session_dir) / PENDING_FILE


def save_pending(session_dir: Path, *, plan: Plan, prompt: str, facts: TimelineFacts,
                 ui_state: dict | None = None) -> dict[str, Any]:
    now = time.time()
    record = {
        "token": f"q_{uuid4().hex[:10]}", "plan": plan.model_dump(), "prompt": prompt,
        "facts_hash": facts_hash(facts), "created": now, "expires": now + CLARIFY_TTL_S,
        "brain": plan.brain, "ui_state": ui_state or {},
    }
    pending_path(session_dir).write_text(json.dumps(record, indent=1, default=str), encoding="utf-8")
    return record


def load_pending(session_dir: Path) -> dict[str, Any] | None:
    p = pending_path(session_dir)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and "plan" in data else None


def clear_pending(session_dir: Path) -> None:
    pending_path(session_dir).unlink(missing_ok=True)


def pending_is_valid(record: dict[str, Any], facts: TimelineFacts | None, *,
                     now: float | None = None) -> tuple[bool, str | None]:
    """(valid, reason_if_not). A foreign `op` shows up as a facts-hash change."""
    now = time.time() if now is None else now
    if float(record.get("expires", 0)) < now:
        return False, "the question expired"
    if facts is not None and record.get("facts_hash") != facts_hash(facts):
        return False, "the timeline changed since the question was asked"
    return True, None


def pending_plan(record: dict[str, Any]) -> Plan:
    return Plan.model_validate(record["plan"])


def blocking_questions(record: dict[str, Any]) -> list[NeedsInput]:
    return pending_plan(record).blocking_questions


# --------------------------------------------------------------------------
# parsing a typed answer
# --------------------------------------------------------------------------

def _match_option(text: str, q: NeedsInput) -> Any | None:
    options = q.options or []
    for opt in options:
        candidates = {normalise_answer(str(opt.value)), normalise_answer(opt.label)}
        candidates |= {normalise_answer(s) for s in (opt.synonyms or [])}
        candidates.discard("")
        if text in candidates:
            return opt.value
    idx = _ORDINALS.get(text)
    if idx is not None and idx < len(options):
        return options[idx].value
    return None


def _parse_number(text: str, q: NeedsInput) -> float | None:
    m = _DURATION_RE.match(text)
    if not m:
        return None
    value = float(m.group(1))
    unit = m.group(2) or ""
    if q.kind == "duration" and unit.startswith("m"):
        value *= 60.0
    if q.min is not None and value < q.min:
        return None
    if q.max is not None and value > q.max:
        return None
    return value


def parse_answer_for(question: NeedsInput, message: str) -> Any | None:
    """The answer `message` gives to `question`, or None when the whole
    message is not an answer to it."""
    text = normalise_answer(message)
    if not text:
        return None
    if question.kind == "confirm":
        if text in _YES:
            return "yes"
        if text in _NO:
            return "no"
        return _match_option(text, question)
    if question.kind in ("number", "duration"):
        return _parse_number(text, question)
    if question.kind == "choice":
        return _match_option(text, question)
    if question.kind == "text":
        # A free-text answer is the whole message; options (if any) still win
        # so "skip"/"abort" keep their meaning.
        hit = _match_option(text, question)
        return hit if hit is not None else message.strip()
    if question.kind == "path":
        hit = _match_option(text, question)
        return hit
    return None


def try_parse_answer(message: str, record: dict[str, Any]) -> dict[str, Any] | None:
    """`{key: value}` for the FIRST unanswered blocking question the whole
    message answers, else None (→ the caller drops the pending plan)."""
    for q in blocking_questions(record):
        value = parse_answer_for(q, message)
        if value is not None:
            return {q.key: value}
    return None


# --------------------------------------------------------------------------
# binding answers into the plan
# --------------------------------------------------------------------------

def _is_no(value: Any) -> bool:
    return str(value).strip().lower() in _NO or value is False


def _fill_placeholders(value: Any, answers: dict[str, Any]) -> Any:
    """Every `"$ask:<key>"` inside `value` → `answers[key]` (nested dicts and
    lists included); an unanswered placeholder is left as it is so the
    executor refuses it loudly rather than dispatching the literal."""
    if isinstance(value, str) and value.startswith(ASK):
        return answers.get(value[len(ASK):], value)
    if isinstance(value, dict):
        return {k: _fill_placeholders(v, answers) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill_placeholders(v, answers) for v in value]
    return value


def _bind_range(step: dict[str, Any], answer: Any, duration: float | None) -> dict[str, Any] | None:
    """`cut_range` asks for `range` as text ("the first 5 seconds"); the answer
    becomes start/end through the slot extractor. None when it did not parse."""
    from . import slots as S
    if isinstance(answer, str):
        rng = S.extract(f"cut {answer}").range
    elif isinstance(answer, S.TimeRange):
        rng = answer
    else:
        rng = None
    if rng is None or duration is None:
        return None
    a, b = rng.resolve(duration)
    if b - a <= 0.05:
        return None
    return {**step, "args": {**step["args"], "start": round(a, 3), "end": round(b, 3)}}


def apply_answers(plan: Plan, answers: dict[str, Any], *,
                  duration: float | None = None) -> tuple[Plan, list[str]]:
    """A NEW plan with `answers` bound (see the module docstring) and the
    notes the reply should carry. Questions answered here are removed from
    `needs_input`; unanswered ones with a default take the default.
    `duration` (the timeline length) lets a `range` answer resolve to
    seconds; without it the question is kept and asked again."""
    notes: list[str] = []
    steps = [s.model_dump() for s in plan.steps]
    remaining: list[NeedsInput] = []
    cancel = False
    download_tools = {d.tool for d in plan.downloads_needed}

    for q in plan.needs_input:
        if q.key in answers and is_blank_answer(answers[q.key]) and q.pauses:
            # An empty answer to a blocking question: refused once, then the
            # step is dropped — never the same question a third time.
            if was_reasked(q):
                gone = [s for s in steps if consumes_answer(s["args"], q.key)]
                steps = [s for s in steps if s not in gone]
                notes.append(dropped_note(q, [s["why"] for s in gone]))
            else:
                remaining.append(reask(q))
                notes.append(reask_note(q))
            continue
        if q.key in answers and not is_blank_answer(answers[q.key]):
            value = answers[q.key]
        elif q.default is not None:
            value = q.default
        else:
            remaining.append(q)
            continue
        if q.key == "go":
            if _is_no(value):
                cancel = True
                notes.append("not started, as asked")
            continue
        if q.key == "downloads":
            if _is_no(value):
                dropped = [s["tool"] for s in steps if s["tool"] in download_tools]
                steps = [s for s in steps if s["tool"] not in download_tools]
                if dropped:
                    notes.append("skipped the steps that needed a download: " + ", ".join(dropped))
            continue
        if q.kind == "choice" and str(value).lower() == "abort":
            cancel = True
            notes.append("aborted, as asked")
            continue
        if q.kind == "choice" and str(value).lower() == "skip":
            before = len(steps)
            steps = [s for s in steps if q.key not in s["args"]]
            if len(steps) != before:
                notes.append(f"skipped the {q.key} step")
            continue
        bound = 0
        if q.key == "range":
            resolved: list[dict[str, Any]] = []
            unparsed = False
            for s in steps:
                if s["tool"] == "cut_range" and any(v == f"{ASK}range" for v in s["args"].values()):
                    fixed = _bind_range(s, value, duration)
                    if fixed is None:
                        unparsed = True
                        resolved.append(s)
                    else:
                        resolved.append(fixed)
                        bound += 1
                else:
                    resolved.append(s)
            steps = resolved
            if unparsed:
                remaining.append(q)          # ask again — the literal must never reach cut_range
                notes.append(f"could not read {value!r} as a range")
                continue
        placeholder = f"{ASK}{q.key}"
        for s in steps:
            if any(_mentions(v, placeholder) for v in s["args"].values()):
                s["args"] = _fill_placeholders(s["args"], {q.key: value})
                bound += 1
        for s in steps:
            if q.key in s["args"] and s["args"][q.key] is None:
                s["args"] = {**s["args"], q.key: value}
                bound += 1
        if bound == 0:
            for s in steps:
                if q.key in s["args"]:
                    s["args"] = {**s["args"], q.key: value}
                    bound += 1
        notes.append(f"{q.key}: {value}")

    if cancel:
        steps = []
    new_plan = plan.with_(steps=steps, needs_input=[q.model_dump() for q in remaining])
    return new_plan, notes


def _mentions(value: Any, placeholder: str) -> bool:
    if isinstance(value, str):
        return value == placeholder
    if isinstance(value, dict):
        return any(_mentions(v, placeholder) for v in value.values())
    if isinstance(value, list):
        return any(_mentions(v, placeholder) for v in value)
    return False


__all__ = ["normalise_answer", "facts_hash", "pending_path", "save_pending", "load_pending",
           "clear_pending", "pending_is_valid", "pending_plan", "blocking_questions",
           "parse_answer_for", "try_parse_answer", "apply_answers"]
