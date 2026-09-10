"""`chat_turn` without an Anthropic key delegates to the Prompt Editor
(agent/loop.py + agent/prompt/service.py, spec §4.6): same event stream,
same `history` list, the run outliving the generator, and an honest
"via <Brain> — " reply. The cloud path is untouched.

The router is stood in for by `prompt_fixtures.route_with` (a known Plan
and the brain events the real router emits) so these tests drive the
turn shape, not the grammar; one test goes through the real router and
P's real `build_facts` to prove the wiring end to end.
"""
from __future__ import annotations

import importlib
import json
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import prompt_fixtures as F  # noqa: E402
from prompt_fixtures import desktop_posture  # noqa: E402,F401

from video_ai_editor.agent import loop as _loop  # noqa: E402
from video_ai_editor.agent.prompt import executor, pending, service  # noqa: E402
from video_ai_editor.agent.prompt.schema import NeedsInput, NeedsInputOption  # noqa: E402

D = importlib.import_module("video_ai_editor.agent.dispatch")

pytestmark = pytest.mark.usefixtures("desktop_posture")


@pytest.fixture
def nokey(monkeypatch):
    monkeypatch.setattr(_loop, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(executor, "_validate_plan", F.identity_validator)


@pytest.fixture
def session(tmp_path: Path, monkeypatch):
    store = F.make_store(tmp_path)
    facts = F.facts_for(store)
    monkeypatch.setattr(service, "build_facts_for", lambda st, ui: facts)
    return store, facts


def _turn(store, message: str, history: list[dict] | None = None, **kw):
    history = history if history is not None else []
    events = F.collect(_loop.chat_turn(store, message, history, **kw))
    return events, history


def _types(events):
    return [e["type"] for e in events]


# ---------------------------------------------------------------- a full mutating turn

def test_a_turn_plans_runs_verifies_and_replies_via_the_brain(session, nokey, monkeypatch):
    store, facts = session
    plan = F.plan_of(F.step("cut_range", track="v1", start=4.0, end=6.0),
                     F.step("apply_lut", clip_id="$v1_all", src="warm.cube", intensity=0.7), title="tighten + look")
    F.route_with(monkeypatch, F.FakeRouted(plan))
    events, history = _turn(store, "tighten it and give it a warm look")
    types = _types(events)
    assert types[:3] == ["brain", "brain", "plan"]
    assert events[0]["status"] == "trying" and events[1]["status"] == "answered" and events[1]["brain"] == "recipes"
    assert events[1]["label"] == "Recipes"
    assert events[2]["plan"]["id"] == plan.id and events[2]["plan"]["steps"][0]["tool"] == "cut_range"
    assert "verify" in types and "op" in types
    assert types.index("verify") < types.index("op") < types.index("text_delta")
    assert types[-1] == "done" and types.count("done") == 1
    text = [e for e in events if e["type"] == "text_delta"]
    assert len(text) == 1 and text[0]["text"].startswith("via Recipes — ")
    assert "tighten + look: done" in text[0]["text"] and "Undo with ⌘Z" in text[0]["text"]
    assert [e["name"] for e in events if e["type"] == "tool_use"] == ["cut_range", "apply_lut"]
    ids = {e["id"] for e in events if e["type"] == "tool_use"}
    assert ids == {e["id"] for e in events if e["type"] == "tool_result"} == {f"{plan.id}_s0", f"{plan.id}_s1"}
    assert store.ops.last().tool == "prompt" and events[types.index("op")]["op"]["tool"] == "prompt"
    # History: the user message and ONE final assistant text block, no tool blocks.
    assert history[0] == {"role": "user", "content": "tighten it and give it a warm look"}
    assert len(history) == 2 and history[1]["role"] == "assistant"
    blocks = history[1]["content"]
    assert len(blocks) == 1 and blocks[0]["type"] == "text" and blocks[0]["text"] == text[0]["text"]
    assert "(run " not in blocks[0]["text"]


def test_verify_payload_shape_and_effect_none_in_the_reply(session, nokey, monkeypatch):
    store, facts = session
    plan = F.plan_of(F.step("remove_fillers", words=["zzz"], track="v1"), title="fillers")
    F.route_with(monkeypatch, F.FakeRouted(plan))
    events, _ = _turn(store, "remove the zzz")
    verify = next(e for e in events if e["type"] == "verify")
    assert {"plan_id", "checks", "passed", "total", "rendered"} <= set(verify)
    assert verify["plan_id"] == plan.id
    step_ok = [e for e in events if e["type"] == "step" and e.get("status") == "ok"][0]
    assert step_ok["effect"] == "none"
    text = next(e for e in events if e["type"] == "text_delta")["text"]
    assert "remove_fillers:" in text and "op" not in _types(events)        # nothing committed


def test_a_failing_required_step_reports_and_leaves_the_tree(session, nokey, monkeypatch):
    store, facts = session
    hash_before = store.edl.hash()
    monkeypatch.setitem(D.DISPATCH, "set_speed", lambda s, a: (_ for _ in ()).throw(RuntimeError("no such speed")))
    plan = F.plan_of(F.step("set_speed", clip_id="$v1_first", factor=1.5), title="speed")
    F.route_with(monkeypatch, F.FakeRouted(plan))
    events, history = _turn(store, "speed it up")
    err = [e for e in events if e["type"] == "error"]
    assert err and err[0]["message"].startswith("Step 1/1 set_speed failed: no such speed. Timeline unchanged.")
    assert _types(events)[-1] == "done" and "op" not in _types(events)
    assert store.edl.hash() == hash_before
    assert history[-1]["content"][0]["text"].startswith("via Recipes — Step 1/1 set_speed failed")


# ---------------------------------------------------------------- read-only, clarify, failures

def test_a_read_only_answer_is_text_only(session, nokey, monkeypatch):
    store, facts = session
    F.route_with(monkeypatch, F.FakeRouted(F.plan_of(intent="ask", reply="The timeline is 12.0 s with one clip.")))
    events, history = _turn(store, "how long is it?")
    assert _types(events) == ["brain", "brain", "plan", "text_delta", "done"]
    assert events[3]["text"] == "via Recipes — The timeline is 12.0 s with one clip."
    assert history[-1]["content"][0]["text"] == events[3]["text"]


# ---------------------------------------------------------------- undo / redo: whole-run history steps

def test_undo_reverts_the_whole_previous_run_and_redo_restores_it(session, nokey, monkeypatch):
    """A prompt "undo" is the ⌘Z of the previous prompt run — ONE history
    step for a two-step plan — streamed as `op` + reply + `done`, and "redo"
    brings it back. The undo/redo turns go through the real recipes grammar
    (no fake router), so the planner → service plumbing is what is proved."""
    from video_ai_editor.edl import EDLStore, Op
    real_route = service._route
    store, facts = session
    h0, n0 = store.edl.hash(), len(store.ops.ops)
    plan = F.plan_of(F.step("cut_range", track="v1", start=4.0, end=6.0),
                     F.step("apply_lut", clip_id="$v1_all", src="warm.cube", intensity=0.7), title="tighten + look")
    F.route_with(monkeypatch, F.FakeRouted(plan))
    _, history = _turn(store, "tighten it and give it a warm look")
    h1 = store.edl.hash()
    assert h1 != h0 and len(store.ops.ops) == n0 + 1 and store.ops.last().tool == "prompt"

    monkeypatch.setattr(service, "_route", real_route)
    events = F.collect(service.prompt_turn(store, "undo that", history, brain="recipes"))
    types = _types(events)
    assert types[-1] == "done" and types.count("op") == 1 and "error" not in types, types
    assert next(e for e in events if e["type"] == "plan")["plan"]["intent"] == "undo"
    assert types.index("op") < types.index("text_delta")
    frame = events[types.index("op")]["op"]
    Op.model_validate(frame)                                      # the production op shape
    assert frame["tool"] == "undo" and frame["summary"] == "Undo: Prompt: tighten + look (2 steps)"
    assert frame["edl_hash_before"] == h1 and frame["edl_hash_after"] == h0
    text = next(e for e in events if e["type"] == "text_delta")["text"]
    assert text == "via Recipes — Undid: Prompt: tighten + look (2 steps). Redo with ⇧⌘Z."
    assert store.edl.hash() == h0 and len(store.ops.ops) == n0 and store.redo_available
    assert EDLStore(Path(store.dir)).edl.hash() == h0             # disk agrees: a reload sees the rewind
    assert history[-2] == {"role": "user", "content": "undo that"}
    assert history[-1] == {"role": "assistant", "content": [{"type": "text", "text": text}]}

    events = F.collect(service.prompt_turn(store, "redo", history, brain="recipes"))
    types = _types(events)
    assert types.count("op") == 1 and "error" not in types and types[-1] == "done", types
    assert next(e for e in events if e["type"] == "plan")["plan"]["intent"] == "redo"
    assert events[types.index("op")]["op"]["tool"] == "redo"
    text = next(e for e in events if e["type"] == "text_delta")["text"]
    assert text == "via Recipes — Redid the last undone edit. Undo with ⌘Z."
    assert store.edl.hash() == h1 and len(store.ops.ops) == n0 + 1 and not store.redo_available
    assert history[-1]["content"][0]["text"] == text


def test_undo_or_redo_with_nothing_to_step_is_honest_and_streams_no_op(tmp_path: Path, nokey, monkeypatch):
    """A fresh store has only its seed snapshot and an empty redo stack: the
    turn says so and streams NO `op` (the clients refresh on `op`, and
    nothing moved)."""
    from video_ai_editor.edl import EDLStore
    store = EDLStore(tmp_path / "blank")
    monkeypatch.setattr(service, "build_facts_for", lambda st, ui: F.facts_for(store))
    h0 = store.edl.hash()
    for verb, prompt in (("undo", "undo that"), ("redo", "redo")):
        history: list[dict] = []
        events = F.collect(service.prompt_turn(store, prompt, history, brain="recipes"))
        types = _types(events)
        assert "op" not in types and "error" not in types and types[-3:] == ["plan", "text_delta", "done"], types
        assert events[-3]["plan"]["intent"] == verb
        assert events[-2]["text"] == f"via Recipes — Nothing to {verb}."
        assert store.edl.hash() == h0 and not store.ops.ops
        assert history[-1]["content"][0]["text"] == events[-2]["text"]


def test_undo_while_a_prompt_run_holds_the_session_says_so_instead_of_rewinding_it(session, nokey, monkeypatch):
    """Same answer `/dispatch` gives (409 prompt_running): an undo must not
    queue behind a live run and then rewind THAT run."""
    from video_ai_editor.api import locks
    store, facts = session
    sid = Path(store.dir).name
    h0, n0 = store.edl.hash(), len(store.ops.ops)
    locks.register_prompt_run(sid, "r_busy")
    try:
        history: list[dict] = []
        events = F.collect(service.prompt_turn(store, "undo", history, brain="recipes"))
    finally:
        locks.clear_prompt_run(sid, "r_busy")
    types = _types(events)
    assert "op" not in types and types[-2:] == ["error", "done"], types
    message = events[-2]["message"]
    assert message.startswith("via Recipes — ") and "r_busy" in message and "undo again" in message
    assert store.edl.hash() == h0 and len(store.ops.ops) == n0
    assert history[-1]["content"][0]["text"] == message


def test_a_required_question_pauses_with_the_options_in_the_text(session, nokey, monkeypatch):
    store, facts = session
    q = NeedsInput(key="target_lang", question="Which language for the captions?", kind="choice", required=True,
                   options=[NeedsInputOption(value=v, label=v) for v in ("hi", "en", "hinglish", "es")])
    plan = F.plan_of(F.step("translate_captions", target_lang=None), needs_input=[q], title="translate")
    F.route_with(monkeypatch, F.FakeRouted(plan))
    events, history = _turn(store, "translate the captions")
    assert _types(events) == ["brain", "brain", "plan", "text_delta", "clarify", "done"]
    assert events[3]["text"] == ("via Recipes — Which language for the captions? "
                                 "Reply **hi**, **en**, **hinglish** or **es**.")
    clarify = events[4]
    assert clarify["plan_id"] == plan.id and clarify["expires_in_s"] == service.CLARIFY_TTL_S
    assert clarify["questions"][0]["key"] == "target_lang" and clarify["token"].startswith("q_")
    record = pending.load_pending(Path(store.dir))
    assert record["token"] == clarify["token"]
    assert [m["role"] for m in history] == ["user", "assistant"]            # alternation preserved
    # The next whole-message answer resumes it; the reply notes the binding.
    events2, history2 = _turn(store, "hinglish", history)
    assert events2[0]["type"] == "brain" and events2[0]["status"] == "answered"
    assert events2[-1]["type"] == "done" and pending.load_pending(Path(store.dir)) is None
    assert history2[2] == {"role": "user", "content": "hinglish"}
    # …while a new request drops the question and plans fresh.
    F.route_with(monkeypatch, F.FakeRouted(plan))
    _turn(store, "translate the captions", history)
    F.route_with(monkeypatch, F.FakeRouted(F.plan_of(intent="ask", reply="ok")))
    events3, _ = _turn(store, "make it hi-res please", history)
    text = next(e for e in events3 if e["type"] == "text_delta")["text"]
    assert "Dropped the earlier question — planning your new request." in text
    assert pending.load_pending(Path(store.dir)) is None


def test_confirm_questions_say_yes_or_no(session, nokey, monkeypatch):
    store, facts = session
    q = NeedsInput(key="go", question="This will take about 3 minutes (auto captions). Start?", kind="confirm",
                   required=True)
    F.route_with(monkeypatch, F.FakeRouted(F.plan_of(F.step("auto_caption"), needs_input=[q], estimated_seconds=180)))
    events, _ = _turn(store, "caption everything")
    assert events[3]["text"].endswith("Start? Reply **yes** or **no**.")
    events2, _ = _turn(store, "no")
    assert _types(events2)[-1] == "done" and "op" not in _types(events2)
    text = next(e for e in events2 if e["type"] == "text_delta")["text"]
    assert "unchanged" in text                             # planner.apply_answers: "Cancelled — the timeline is unchanged."


def test_a_clarify_intent_from_the_router_becomes_a_question(session, nokey, monkeypatch):
    store, facts = session
    q = NeedsInput(key="intent", question="Did you mean captions, silences or music?", kind="choice", required=True,
                   options=[NeedsInputOption(value="captions", label="captions")])
    F.route_with(monkeypatch, F.FakeRouted(None, brain="recipes", clarify=q, note="clarify_intent"))
    events, _ = _turn(store, "make it pop")
    assert "clarify" in _types(events) and events[-1]["type"] == "done"
    plan_evt = next(e for e in events if e["type"] == "plan")
    assert plan_evt["plan"]["intent"] == "ask" and plan_evt["plan"]["steps"] == []
    # The question IS the answer: the ladder ends with an `answered` for the
    # brain that asked it, after the `failed` the router reported.
    statuses = [(e["status"]) for e in events if e["type"] == "brain"]
    assert statuses[-1] == "answered" and "failed" in statuses


def test_router_that_answers_nothing_says_so(session, nokey, monkeypatch):
    store, facts = session
    F.route_with(monkeypatch, F.FakeRouted(None, brain="recipes"))
    events, history = _turn(store, "asdfgh")
    assert _types(events)[-2:] == ["text_delta", "done"]
    assert events[-2]["text"].startswith("via Recipes — I could not work out")
    assert history[-1]["role"] == "assistant"


def test_planning_failures_reach_the_user_as_error_frames(session, nokey, monkeypatch):
    store, facts = session
    from video_ai_editor.agent.prompt.brains.base import BrainUnavailable

    def _no_brain(req, *, brain, emit):
        raise BrainUnavailable("no planner", "install the planner")

    monkeypatch.setattr(service, "_route", _no_brain)
    events, _ = _turn(store, "add captions")
    assert _types(events) == ["error", "done"] and "Fix: install the planner" in events[0]["message"]
    monkeypatch.setattr(service, "build_facts_for", lambda st, ui: (_ for _ in ()).throw(RuntimeError("bad edl")))
    events, _ = _turn(store, "add captions")
    assert _types(events) == ["error", "done"] and "Could not read the timeline: bad edl" in events[0]["message"]


def test_pinned_brain_and_env_are_passed_to_the_router(session, nokey, monkeypatch):
    store, facts = session
    seen: list[str | None] = []

    def _route(req, *, brain, emit):
        seen.append(brain)
        return F.FakeRouted(F.plan_of(intent="ask", reply="ok"))

    monkeypatch.setattr(service, "_route", _route)
    F.collect(service.prompt_turn(store, "x", [], brain="local_model"))
    monkeypatch.setenv("VAI_BRAIN", "fm")
    F.collect(service.prompt_turn(store, "x", []))
    monkeypatch.setenv("VAI_BRAIN", "auto")
    F.collect(service.prompt_turn(store, "x", []))
    assert seen == ["local_model", "fm", None]


# ---------------------------------------------------------------- reconnect + replay

def test_resume_run_replays_the_bus_from_an_index(session, nokey, monkeypatch):
    store, facts = session
    plan = F.plan_of(F.step("apply_lut", clip_id="$v1_all", src="warm.cube"), title="look")
    F.route_with(monkeypatch, F.FakeRouted(plan))
    first, _ = _turn(store, "warm look")
    handle = executor.get_run(Path(store.dir).name)
    replay = F.collect(service.prompt_turn(store, "", [], resume_run=handle.run_id))
    assert replay == handle.bus.snapshot() and replay[0]["type"] == "brain" and replay[-1]["type"] == "done"
    partial = F.collect(service.prompt_turn(store, "", [], resume_run=handle.run_id, from_index=3))
    assert partial == replay[3:]
    gone = F.collect(service.prompt_turn(store, "", [], resume_run="r_nothere"))
    assert _types(gone) == ["error", "done"] and "not in memory" in gone[0]["message"]


# ---------------------------------------------------------------- app-level wiring

def test_validate_ai_config_logs_the_local_brains_at_info(monkeypatch, caplog):
    from video_ai_editor import config, main as _main
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    # The app logger writes JSON to stderr without propagating; route the
    # call through a plain logger so caplog can read the level and message.
    plain = logging.getLogger("test.vai.config")
    plain.propagate = True
    monkeypatch.setattr(_main, "get_logger", lambda: plain)
    with caplog.at_level(logging.INFO, logger="test.vai.config"):
        _main._validate_ai_config()
    infos = [r for r in caplog.records if "local brains" in r.getMessage()]
    assert infos and infos[0].levelno == logging.INFO
    assert "recipes / Apple Intelligence / local model" in infos[0].getMessage()
    assert not any(r.levelno >= logging.WARNING and "ANTHROPIC_API_KEY" in r.getMessage() for r in caplog.records)


def test_chat_route_streams_the_turn_and_saves_history_with_the_final_text(tmp_path: Path, monkeypatch, nokey):
    from fastapi.testclient import TestClient
    from video_ai_editor import main as _main, storage as _storage
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path)
    monkeypatch.setattr(_main, "WORKDIR", tmp_path)
    _main._STORES.clear()
    sid = "s_chat_nokey"
    store = F.make_store(tmp_path, name=sid)
    facts = F.facts_for(store)
    monkeypatch.setattr(service, "build_facts_for", lambda st, ui: facts)
    plan = F.plan_of(F.step("apply_lut", clip_id="$v1_all", src="cool.cube"), title="cool look")
    F.route_with(monkeypatch, F.FakeRouted(plan))
    client = TestClient(_main.app)
    r = client.post(f"/api/sessions/{sid}/chat", json={"message": "give it a cool look"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    frames = [json.loads(f[len("data: "):]) for f in r.text.split("\n\n") if f.startswith("data: ")]
    assert frames[0]["type"] == "brain" and frames[-1]["type"] == "done"
    final = next(f for f in frames if f["type"] == "text_delta")["text"]
    assert final.startswith("via Recipes — cool look: done")
    handle = executor.get_run(sid)
    handle.thread.join(10.0)
    saved = json.loads((tmp_path / sid / "chat.json").read_text(encoding="utf-8"))
    assert [m["role"] for m in saved] == ["user", "assistant"]
    assert saved[1]["content"][0]["text"] == final                 # finalize + the route's save agree
    assert client.get(f"/api/sessions/{sid}/history").json()["history"] == saved


def test_end_to_end_through_the_real_router_and_real_facts(tmp_path: Path, monkeypatch):
    """No fakes: P's grammar plans "give it a warm look" on the fixture,
    P's validate_plan validates it, the executor runs it, the verifier
    measures it."""
    from video_ai_editor.agent.prompt.brains import router as _router
    monkeypatch.setattr(_loop, "ANTHROPIC_API_KEY", "")
    store = F.make_store(tmp_path)
    src = Path(store.edl.get_track("v1").clips[0].src)
    # The real facts builder reads `<session>/uploads`; the fixture put the
    # clip beside the session dir, so mirror it into the real layout.
    real = Path(store.dir) / "uploads" / "talk"
    real.mkdir(parents=True)
    (real / src.name).write_bytes(src.read_bytes())
    (real / "ingest.json").write_text((src.parent / "ingest.json").read_text(encoding="utf-8"), encoding="utf-8")
    store.edl.get_track("v1").clips[0].src = str(real / src.name)
    store.commit("add_clip", {}, "move")
    facts = service.build_facts_for(store, None)
    assert facts.has_transcript and facts.v1_clip_ids
    events = F.collect(_loop.chat_turn(store, "give it a warm look", []))
    types = _types(events)
    assert types[-1] == "done"
    assert any(e["type"] == "brain" and e["status"] == "answered" for e in events)
    errors = [e for e in events if e["type"] == "error"]
    assert not errors, errors
    assert any(e["type"] == "tool_use" and e["name"] == "apply_lut" for e in events), types
    assert next(e for e in events if e["type"] == "text_delta")["text"].startswith("via ")
    assert any(any(x.type == "lut" for x in c.effects) for c in store.edl.get_track("v1").clips)
    assert isinstance(_router.brains_report(), dict)
