"""Final QA round 3: answering a Prompt-bar question must not loop.

"split clip 2" → "Where should I split?" → "at 7 seconds" came back with the
SAME question, forever: the answer re-planned "<prompt> — <answer>", which the
grammar could not read, and a typo'd verb ("delet clip 3") stayed in every
retry, so Apple Intelligence asked "Which clip should I delete?" each time.
Now a typo'd original prompt is re-read with its typos fixed, and when the
re-plan asks the very same question again the reply says plainly what to
type instead of pausing on it once more.

The ladder is the REAL router with the REAL recipes brain; only the Apple
Intelligence rung is a stand-in (as in test_prompt_model_question.py).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import prompt_fixtures as F  # noqa: E402
from prompt_fixtures import desktop_posture, no_downloads  # noqa: E402,F401

from video_ai_editor.agent.dispatch import dispatch  # noqa: E402
from video_ai_editor.agent.prompt import pending, service  # noqa: E402
from video_ai_editor.agent.prompt.brains import base as B  # noqa: E402
from video_ai_editor.agent.prompt.brains import router  # noqa: E402
from video_ai_editor.agent.prompt.brains.recipes_brain import RecipesBrain  # noqa: E402
from video_ai_editor.agent.prompt.schema import Plan  # noqa: E402


class AskingFM:
    """Apple Intelligence that only ever asks `question` (no steps)."""
    id = "apple_intelligence"

    def __init__(self, question: str) -> None:
        self.question = question
        self.prompts: list[str] = []

    def availability(self):
        return B.available("fake Apple Intelligence", model="fake-fm")

    def plan(self, req, *, timeout_s):
        self.prompts.append(req.prompt)
        plan = Plan.new(intent="clarify", brain=self.id, confidence=0.6, reply=self.question)
        return B.BrainResult(plan=plan, brain=self.id, ok=True, latency_ms=3, model="fake-fm")

    def text(self, task, *, timeout_s):
        return None


def _ladder(monkeypatch, fm) -> None:
    monkeypatch.delenv("VAI_BRAIN", raising=False)
    monkeypatch.setattr(router, "default_brains", lambda: {"recipes": RecipesBrain(), "apple_intelligence": fm})


def _session(tmp_path):
    store = F.make_store(tmp_path)
    dispatch(store, "split_at", {"track": "v1", "time": 4.0})
    dispatch(store, "split_at", {"track": "v1", "time": 8.0})
    return store


def _said(events) -> str:
    bits = [q["question"] for e in events if e["type"] == "clarify" for q in e.get("questions", [])]
    return " ".join(bits + [e.get("text", "") for e in events if e["type"] == "text_delta"])


@pytest.mark.usefixtures("desktop_posture", "no_downloads")
def test_a_typo_in_the_first_prompt_is_fixed_when_the_answer_replans(tmp_path, monkeypatch):
    fm = AskingFM("Which clip should I delete?")
    _ladder(monkeypatch, fm)
    store = _session(tmp_path)
    ids = [c.id for c in store.edl.get_track("v1").clips]
    events = F.collect(service.prompt_turn(store, "delet clip 3", [], ui_state={"playhead": 1.0}))
    clarify = [e for e in events if e["type"] == "clarify"]
    assert clarify, _said(events)
    events = F.collect(service.resume(store, clarify[0]["token"], {service.MODEL_QUESTION_KEY: "the last clip"},
                                      history=[], user_message="the last clip"))
    assert [c.id for c in store.edl.get_track("v1").clips] == ids[:2], _said(events)
    assert pending.load_pending(Path(store.dir)) is None


@pytest.mark.usefixtures("desktop_posture", "no_downloads")
def test_the_same_question_twice_is_not_asked_a_third_time(tmp_path, monkeypatch):
    fm = AskingFM("Where should I split?")
    _ladder(monkeypatch, fm)
    store = _session(tmp_path)
    before = store.edl.hash()
    events = F.collect(service.prompt_turn(store, "give the second shot a wobble", [], ui_state={"playhead": 1.0}))
    clarify = [e for e in events if e["type"] == "clarify"]
    assert clarify, _said(events)
    events = F.collect(service.resume(store, clarify[0]["token"], {service.MODEL_QUESTION_KEY: "at 7 seconds"},
                                      history=[], user_message="at 7 seconds"))
    said = _said(events)
    assert not [e for e in events if e["type"] == "clarify"], said
    assert pending.load_pending(Path(store.dir)) is None
    assert "one line" in said, said
    assert store.edl.hash() == before
