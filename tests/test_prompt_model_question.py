"""Item 23 (wave E, F4b): a MODEL's question becomes the clarify card.

When Apple Intelligence (or the local model, or Claude) plans a request
whose recipe cannot run without one more fact — "rotate the second clip"
with no angle — the plan has no steps and its reply is a question. That
reply used to be shown as a plain notice: the card had no answer box, the
user retyped the whole request. Now it is a paused plan with one free-text
question (`service.MODEL_QUESTION_KEY`), answerable in place; the answer
re-plans "<prompt> — <answer>" through the whole ladder.

The ladder here is the REAL router with the REAL recipes brain; only the
Apple Intelligence rung is a stand-in (no Swift helper in CI) that drafts
recipe intents exactly like `fm.FMBrain._expand` does (`from_intents`).
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
from video_ai_editor.agent.prompt.recipes import from_intents  # noqa: E402
from video_ai_editor.agent.prompt.schema import IntentDraft  # noqa: E402

#: The grammar reads none of this (confidence 0), so the ladder asks the model.
PROMPT = "give the second shot a quarter turn"


class FakeFM:
    """Apple Intelligence as the ladder sees it: drafts a `rotate` with no
    angle for the bare request (the recipe then asks "Rotate by how much?"),
    and a 90° rotate once the request carries the answer."""
    id = "apple_intelligence"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def availability(self):
        return B.available("fake Apple Intelligence", model="fake-fm")

    def plan(self, req, *, timeout_s):
        self.prompts.append(req.prompt)
        slots = {"clip_ref": "the second clip"}
        if service.ANSWER_JOIN in req.prompt and "90" in req.prompt:
            slots["degrees"] = 90
        draft = IntentDraft(intents=[{"recipe": "rotate", "slots": slots}], confidence=0.8, reply="")
        plan = from_intents(draft, req.facts)
        return B.BrainResult(plan=plan.with_(brain=self.id), brain=self.id, ok=True, latency_ms=3, model="fake-fm")

    def text(self, task, *, timeout_s):
        return None


@pytest.fixture
def ladder(monkeypatch):
    fm = FakeFM()
    monkeypatch.delenv("VAI_BRAIN", raising=False)
    monkeypatch.setattr(router, "default_brains", lambda: {"recipes": RecipesBrain(), "apple_intelligence": fm})
    return fm


def _session(tmp_path):
    store = F.make_store(tmp_path)
    dispatch(store, "split_at", {"track": "v1", "time": 4.0})
    dispatch(store, "split_at", {"track": "v1", "time": 8.0})
    return store


def _rotations(store) -> list[float]:
    return [float(c.transform.rotation) for c in store.edl.get_track("v1").clips]


@pytest.mark.usefixtures("desktop_posture", "no_downloads")
def test_a_models_question_is_a_clarify_card_and_the_answer_replans(tmp_path, ladder):
    store = _session(tmp_path)
    before = store.edl.hash()
    events = F.collect(service.prompt_turn(store, PROMPT, [], ui_state={"playhead": 1.0}))
    clarify = [e for e in events if e["type"] == "clarify"]
    assert clarify, [e["type"] for e in events]
    (q,) = clarify[0]["questions"]
    assert q["key"] == service.MODEL_QUESTION_KEY and q["kind"] == "text" and "how much" in q["question"]
    plan = next(e["plan"] for e in events if e["type"] == "plan")
    assert plan["brain"] == "apple_intelligence" and plan["intent"] == service.MODEL_QUESTION_INTENT
    assert store.edl.hash() == before                     # a question, not an edit
    assert pending.load_pending(Path(store.dir)) is not None

    # answered IN PLACE (the card's own box: POST …/prompt/answer → resume)
    events = F.collect(service.resume(store, clarify[0]["token"], {service.MODEL_QUESTION_KEY: "90 degrees"},
                                      history=[], user_message="90 degrees"))
    assert ladder.prompts[-1] == f"{PROMPT}{service.ANSWER_JOIN}90 degrees", ladder.prompts
    assert not [e for e in events if e["type"] == "error"], events
    assert _rotations(store) == [0.0, 90.0, 0.0]
    assert pending.load_pending(Path(store.dir)) is None


@pytest.mark.usefixtures("desktop_posture", "no_downloads")
def test_a_typed_answer_resumes_but_a_new_request_is_planned_fresh(tmp_path, ladder):
    store = _session(tmp_path)
    F.collect(service.prompt_turn(store, PROMPT, [], ui_state={"playhead": 1.0}))
    # "mute the music"… a complete request on its own is NOT glued on as the answer
    events = F.collect(service.prompt_turn(store, "reverse the first clip", [], ui_state={"playhead": 1.0}))
    assert all(service.ANSWER_JOIN not in p for p in ladder.prompts), ladder.prompts
    assert store.edl.get_track("v1").clips[0].reverse is True
    assert any("Dropped the earlier question" in (e.get("plan") or {}).get("reply", "") or ""
               for e in events if e["type"] == "plan")

    # a bare answer typed as the next message resumes the model's question
    F.collect(service.prompt_turn(store, PROMPT, [], ui_state={"playhead": 1.0}))
    F.collect(service.prompt_turn(store, "90 degrees", [], ui_state={"playhead": 1.0}))
    assert ladder.prompts[-1] == f"{PROMPT}{service.ANSWER_JOIN}90 degrees"
    assert _rotations(store) == [0.0, 90.0, 0.0]


def test_only_a_models_stepless_question_becomes_a_card():
    """The recipes brain's questions stay replies (the next prompt answers
    them, and the sweep pins their wording); a plan with steps, one with its
    own questions, or a statement is left alone."""
    from video_ai_editor.agent.prompt.recipes import ask
    from video_ai_editor.agent.prompt.schema import Plan, Step
    q = Plan.new(intent="rotate", brain="apple_intelligence", reply="Rotate by how much? Say like '90 degrees'.")
    card = service.model_question_plan(q)
    assert card is not None and service.is_model_question(card) and card.reply is None
    assert card.needs_input[0].question.startswith("Rotate by how much?")
    assert service.model_question_plan(q.with_(brain="recipes")) is None
    assert service.model_question_plan(q.with_(reply="Nothing to change.")) is None
    assert service.model_question_plan(q.with_(steps=[Step(tool="split_at", args={"time": 1.0}, why="x")])) is None
    assert service.model_question_plan(q.with_(needs_input=[ask("text", "What should it say?", kind="text")])) is None
    long = "I can do that. " * 20 + "Which clip should I rotate? Name it."
    assert service.model_question_plan(q.with_(reply=long[:400])).needs_input[0].question.startswith("Which clip")
