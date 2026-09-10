"""The SSE contract (spec §4.1): the phone's six frozen shapes are untouched,
the five new types are ignorable by a client that does not know them, every
frame is one line, `done` is last and unique, and the first `text_delta` of
every no-key turn starts with "via <Brain> — ".

The phone's rule is replayed here from `mobile/lib/sse.ts` itself: its
`KNOWN_TYPES` set is parsed out of the TypeScript and every frame a real
turn produces is classified the way `parseFrame` would classify it.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import prompt_fixtures as F  # noqa: E402
from prompt_fixtures import desktop_posture  # noqa: E402,F401

from video_ai_editor.agent import loop as _loop  # noqa: E402
from video_ai_editor.agent.prompt import executor, service  # noqa: E402
from video_ai_editor.agent.prompt.schema import NeedsInput, NeedsInputOption  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.usefixtures("desktop_posture")

#: The frozen key sets of the six legacy shapes (`agent/loop.py` docstring).
LEGACY_SHAPES: dict[str, tuple[set[str], set[str]]] = {   # type -> (required keys, optional keys)
    "text_delta": ({"type", "text"}, set()),
    "tool_use": ({"type", "name", "args", "id"}, set()),
    "tool_result": ({"type", "name", "result", "id"}, {"is_error"}),
    "op": ({"type", "op"}, set()),
    "done": ({"type"}, set()),
    "error": ({"type", "message"}, set()),
}


def phone_known_types() -> set[str]:
    ts = (REPO / "mobile/lib/sse.ts").read_text(encoding="utf-8")
    block = ts.split("const KNOWN_TYPES", 1)[1].split("]", 1)[0]
    return set(re.findall(r'"([a-z_]+)"', block))


def phone_classify(frame_text: str) -> str:
    """`parseFrame` from mobile/lib/sse.ts, in Python: event | empty | unreadable."""
    data = [line[5:].removeprefix(" ") for line in frame_text.split("\n") if line.startswith("data:")]
    if not data:
        return "empty"
    try:
        parsed = json.loads("\n".join(data))
    except ValueError:
        return "unreadable"
    if not isinstance(parsed, dict) or not isinstance(parsed.get("type"), str):
        return "unreadable"
    return "event" if parsed["type"] in phone_known_types() else "empty"


@pytest.fixture
def session(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(_loop, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(executor, "_validate_plan", F.identity_validator)
    store = F.make_store(tmp_path)
    facts = F.facts_for(store)
    monkeypatch.setattr(service, "build_facts_for", lambda st, ui: facts)
    return store, facts


def _turn(store, message, history=None):
    return F.collect(_loop.chat_turn(store, message, history if history is not None else []))


def _assert_contract(events: list[dict]) -> None:
    types = [e["type"] for e in events]
    assert set(types) <= set(service.EVENT_TYPES), service.unknown_event_types(events)
    assert types[-1] == "done" and types.count("done") == 1
    for e in events:
        frame = f"data: {json.dumps(e)}\n\n"
        assert frame.count("\n") == 2, "frames must be single-line json"
        if e["type"] in LEGACY_SHAPES:
            required, optional = LEGACY_SHAPES[e["type"]]
            assert required <= set(e) <= required | optional, e
            assert phone_classify(frame) == "event"
        else:
            assert e["type"] in service.PROMPT_EVENT_TYPES
            assert phone_classify(frame) == "empty"           # ignorable, never "unreadable"
    text = [e for e in events if e["type"] == "text_delta"]
    if text:
        assert text[0]["text"].startswith("via "), text[0]["text"]


def test_the_phones_known_types_are_exactly_the_legacy_six():
    assert phone_known_types() == set(service.LEGACY_EVENT_TYPES)
    assert set(service.PROMPT_EVENT_TYPES).isdisjoint(phone_known_types())


def test_a_mutating_turn_honours_the_contract(session, monkeypatch):
    store, facts = session
    plan = F.plan_of(F.step("cut_range", track="v1", start=4.0, end=6.0),
                     F.step("apply_lut", clip_id="$v1_all", src="warm.cube"),
                     F.step("set_speed", clip_id="$v1_first", factor=9.9, _optional=True), title="edit")
    monkeypatch.setitem(__import__("importlib").import_module("video_ai_editor.agent.dispatch").DISPATCH,
                        "set_speed", lambda s, a: (_ for _ in ()).throw(RuntimeError("nope")))
    F.route_with(monkeypatch, F.FakeRouted(plan))
    events = _turn(store, "edit it")
    _assert_contract(events)
    types = [e["type"] for e in events]
    assert types.index("verify") < types.index("op") < types.index("text_delta") < types.index("done")
    assert types.count("op") == 1
    # tool_use / tool_result pair by id, in order, including the skipped step's error result.
    uses = [e for e in events if e["type"] == "tool_use"]
    results = [e for e in events if e["type"] == "tool_result"]
    assert [u["id"] for u in uses] == [r["id"] for r in results] == [f"{plan.id}_s{i}" for i in range(3)]
    assert results[2]["is_error"] is True and "nope" in json.dumps(results[2]["result"])
    # Brain frames carry the statuses the badge ladder reduces over.
    assert [e["status"] for e in events if e["type"] == "brain"] == ["trying", "answered"]
    assert all(e["status"] in service.STEP_STATUSES for e in events if e["type"] == "step")
    # The phone sees a Claude-shaped turn: prose, tool calls, one op, done.
    phone = [e["type"] for e in events if e["type"] in phone_known_types()]
    assert phone[:2] == ["tool_use", "tool_result"] and phone[-3:] == ["op", "text_delta", "done"]


def test_a_paused_turn_ends_with_clarify_then_done(session, monkeypatch):
    store, facts = session
    q = NeedsInput(key="target_lang", question="Which language?", kind="confirm", required=True,
                   options=[NeedsInputOption(value="yes", label="yes"), NeedsInputOption(value="no", label="no")])
    F.route_with(monkeypatch, F.FakeRouted(F.plan_of(F.step("translate_captions", target_lang=None), needs_input=[q])))
    events = _turn(store, "translate")
    _assert_contract(events)
    types = [e["type"] for e in events]
    assert types[-3:] == ["text_delta", "clarify", "done"]
    assert events[-3]["text"].endswith("Reply **yes** or **no**.")
    assert set(events[-2]) == {"type", "token", "plan_id", "questions", "expires_in_s"}
    assert set(events[-2]["questions"][0]) >= {"key", "question", "kind", "required"}


def test_error_turns_honour_the_contract(session, monkeypatch):
    store, facts = session
    monkeypatch.setattr(service, "_route", lambda req, *, brain, emit: (_ for _ in ()).throw(RuntimeError("router died")))
    events = _turn(store, "x")
    _assert_contract(events)
    assert [e["type"] for e in events] == ["error", "done"] and "router died" in events[0]["message"]


def test_the_chat_and_prompt_routes_frame_identically(tmp_path: Path, monkeypatch):
    from fastapi.testclient import TestClient
    from video_ai_editor import main as _main, storage as _storage
    monkeypatch.setattr(_loop, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(executor, "_validate_plan", F.identity_validator)
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path)
    monkeypatch.setattr(_main, "WORKDIR", tmp_path)
    _main._STORES.clear()
    sid = "s_sse"
    store = F.make_store(tmp_path, name=sid)
    facts = F.facts_for(store)
    monkeypatch.setattr(service, "build_facts_for", lambda st, ui: facts)
    client = TestClient(_main.app)
    for route in (f"/api/sessions/{sid}/chat", f"/api/sessions/{sid}/prompt"):
        F.route_with(monkeypatch, F.FakeRouted(F.plan_of(F.step("apply_lut", clip_id="$v1_all", src="warm.cube"))))
        r = client.post(route, json={"message": "warm look"})
        assert r.status_code == 200
        raw_frames = [f for f in r.text.split("\n\n") if f]
        assert all(f.startswith("data: ") and "\n" not in f for f in raw_frames), route
        kinds = [phone_classify(f + "\n\n") for f in raw_frames]
        assert "unreadable" not in kinds and kinds[-1] == "event"
        events = [json.loads(f[6:]) for f in raw_frames]
        _assert_contract(events)
        executor.get_run(sid).thread.join(10.0)


def test_summary_prefix_names_the_content_brain_when_it_differs():
    from types import SimpleNamespace
    from video_ai_editor.agent.prompt.summary import compose_reply
    plan = F.plan_of(intent="hook", brain="recipes", content_brain="apple_intelligence", reply="hook text: Apple Intelligence")
    res = SimpleNamespace(error=None, steps=[], new_sessions=[], child_runs=[], committed=False)
    text = compose_reply(plan, res, None)
    assert text.startswith("via Recipes · text by Apple Intelligence — ")
    failed = compose_reply(plan, SimpleNamespace(error="Cancelled — timeline unchanged.", steps=[]), None)
    assert failed == "via Recipes · text by Apple Intelligence — Cancelled — timeline unchanged."
