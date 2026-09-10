"""The prompt executor (agent/prompt/executor.py, spec §4.2): one op per run,
rollback that restores files as well as the tree, optional-step skipping,
cancellation, execution that outlives the SSE subscriber, clip sentinels
resolved against the LIVE edl, the 409 policy, and shorts.finish children.

Fake tools are installed by replacing REAL `DISPATCH` entries, never by
adding new names: the executor's guard refuses a tool that is not in the
table or has no advertised schema, and these tests must run through that
guard the way production does. `validate_plan` (P's module) is swapped for
the identity where a test is about execution; the security-boundary file
proves the validator runs first.
"""
from __future__ import annotations

import importlib
import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import prompt_fixtures as F  # noqa: E402
from prompt_fixtures import desktop_posture  # noqa: E402,F401

from video_ai_editor.agent.prompt import executor  # noqa: E402
from video_ai_editor.agent.prompt.runlog import read_record  # noqa: E402
from video_ai_editor.agent.prompt.schema import bind_postconditions  # noqa: E402
from video_ai_editor.api import locks  # noqa: E402
from video_ai_editor.edl.schema import Clip, Effect, TextClip, Track  # noqa: E402

D = importlib.import_module("video_ai_editor.agent.dispatch")

pytestmark = pytest.mark.usefixtures("desktop_posture")


@pytest.fixture
def session(tmp_path: Path):
    store = F.make_store(tmp_path)
    return store, F.facts_for(store)


@pytest.fixture
def identity(monkeypatch):
    monkeypatch.setattr(executor, "_validate_plan", F.identity_validator)


def _run(store, plan, facts, *, cancel=None, events=None):
    events = events if events is not None else []
    res = executor.run_plan(store, plan, facts, emit=events.append,
                            cancel_event=cancel or threading.Event(), prompt="test prompt",
                            validator=F.identity_validator)
    return res, events


def _fail(store, args):
    raise RuntimeError("boom")


def _v1_ids(store) -> list[str]:
    return [c.id for c in store.edl.get_track("v1").clips if isinstance(c, Clip)]


# ---------------------------------------------------------------- one op

def test_a_multi_step_plan_is_one_op_and_one_undo_step(session):
    store, facts = session
    hash_before, ops_before = store.edl.hash(), len(store.ops.ops)
    plan = F.plan_of(F.step("cut_range", track="v1", start=4.0, end=6.0),
                     F.step("apply_lut", clip_id="$v1_all", src="warm.cube", intensity=0.7),
                     title="tighten + look")
    res, events = _run(store, plan, facts)
    assert res.error is None and res.committed and res.applied == 2
    assert len(store.ops.ops) == ops_before + 1
    op = store.ops.last()
    assert op.tool == "prompt" and op.summary.startswith("Prompt: tighten + look (2 steps)")
    assert op.args["plan_id"] == plan.id and op.args["steps"] == ["cut_range", "apply_lut"]
    assert res.op["tool"] == "prompt"
    assert store.edl.duration == pytest.approx(10.0)
    assert store.undo() and store.edl.hash() == hash_before          # ONE ⌘Z undoes the whole plan
    assert [e["type"] for e in events].count("step") >= 4
    assert not (Path(store.dir) / "cache" / "prompt_snap").exists() or \
        not any((Path(store.dir) / "cache" / "prompt_snap").iterdir())  # snapshot discarded on success


def test_a_plan_with_no_effect_commits_nothing(session):
    store, facts = session
    ops_before = len(store.ops.ops)
    plan = F.plan_of(F.step("remove_fillers", words=["zzz"], track="v1"))
    res, events = _run(store, plan, facts)
    assert res.error is None and not res.committed and res.op is None and res.applied == 0
    assert len(store.ops.ops) == ops_before
    assert res.steps[0].status == "ok" and res.steps[0].effect == "none"
    ok_evt = [e for e in events if e["type"] == "step" and e.get("status") == "ok"][0]
    assert ok_evt["effect"] == "none" and "no effect" in ok_evt["summary"]


# ---------------------------------------------------------------- rollback

def test_required_failure_rolls_back_the_tree_and_restores_ingest_json(session, monkeypatch):
    """A step that rewrote ingest.json (auto_caption translating the
    transcript before commit) followed by a required failure: the EDL hash
    AND the transcript file must be byte-identical to before."""
    store, facts = session
    ingest = Path(facts.ingest_json_path)
    ingest_bytes = ingest.read_bytes()
    hash_before, ops_before = store.edl.hash(), len(store.ops.ops)

    def fake_auto_caption(store, args):
        data = json.loads(ingest.read_text(encoding="utf-8"))
        data["transcript"]["language"] = "en-translated"
        ingest.write_text(json.dumps(data), encoding="utf-8")
        cap = store.edl.get_track("captions") or Track(id="captions", type="captions")
        if store.edl.get_track("captions") is None:
            store.edl.tracks.append(cap)
        cap.clips.append(TextClip(text="hello", start=0.0, end=1.0, role="caption"))
        store.commit("auto_caption", args, "captions")
        return {"summary": "captions", "cues": 1}

    monkeypatch.setitem(D.DISPATCH, "auto_caption", fake_auto_caption)
    monkeypatch.setitem(D.DISPATCH, "set_speed", _fail)
    plan = F.plan_of(F.step("auto_caption", target="en"),
                     F.step("set_speed", clip_id="$v1_first", factor=1.5))
    res, events = _run(store, plan, facts)
    assert res.error == "Step 2/2 set_speed failed: boom. Timeline unchanged. Transcript restored."
    assert not res.committed and res.op is None
    assert store.edl.hash() == hash_before and len(store.ops.ops) == ops_before
    assert ingest.read_bytes() == ingest_bytes
    assert "ingest.json" in res.restored_files
    assert [s.status for s in res.steps] == ["ok", "failed"]
    err = [e for e in events if e["type"] == "error"]
    assert err and err[0]["message"] == res.error
    failed = [e for e in events if e["type"] == "step" and e.get("status") == "failed"]
    assert failed[0]["tool"] == "set_speed" and failed[0]["error"] == "boom"
    tr = [e for e in events if e["type"] == "tool_result" and e.get("is_error")]
    assert tr and tr[0]["id"] == f"{plan.id}_s1"


def test_a_file_a_failed_run_created_is_removed_on_restore(session, monkeypatch):
    store, facts = session
    created = Path(store.dir) / "transcript.json"
    assert not created.exists()

    def fake_transcribe(store, args, **_):
        created.write_text("{}", encoding="utf-8")
        return {"summary": "x", "words": 3}

    monkeypatch.setitem(D.DISPATCH, "transcribe", fake_transcribe)
    monkeypatch.setitem(D.DISPATCH, "set_speed", _fail)
    plan = F.plan_of(F.step("transcribe"), F.step("set_speed", clip_id="$v1_first", factor=2.0))
    res, _ = _run(store, plan, facts)
    assert res.error and not created.exists()


def test_optional_failure_is_skipped_and_the_rest_commits(session, monkeypatch):
    store, facts = session
    monkeypatch.setitem(D.DISPATCH, "set_speed", _fail)
    plan = F.plan_of(F.step("set_speed", clip_id="$v1_first", factor=1.5, _optional=True),
                     F.step("apply_lut", clip_id="$v1_all", src="cool.cube", intensity=0.5))
    res, events = _run(store, plan, facts)
    assert res.error is None and res.committed and res.applied == 1
    assert [s.status for s in res.steps] == ["skipped", "ok"]
    assert res.steps[0].error == "boom"
    skipped = [e for e in events if e["type"] == "step" and e.get("status") == "skipped"]
    assert skipped and skipped[0]["tool"] == "set_speed" and skipped[0]["error"] == "boom"
    assert any(e["type"] == "tool_result" and e.get("is_error") and e["id"] == f"{plan.id}_s0" for e in events)


def test_cancel_between_steps_leaves_the_timeline_unchanged(session, monkeypatch):
    store, facts = session
    cancel = threading.Event()
    hash_before = store.edl.hash()
    ran: list[str] = []

    def first(store, args):
        ran.append("apply_lut")
        store.edl.get_track("v1").clips[0].effects.append(Effect(type="lut", params={"src": "x"}))
        cancel.set()                                   # the Cancel button lands mid-run
        return {"summary": "lut"}

    def second(store, args):
        ran.append("set_speed")
        return {"summary": "speed"}

    monkeypatch.setitem(D.DISPATCH, "apply_lut", first)
    monkeypatch.setitem(D.DISPATCH, "set_speed", second)
    plan = F.plan_of(F.step("apply_lut", clip_id="$v1_first", src="warm.cube"),
                     F.step("set_speed", clip_id="$v1_first", factor=1.5))
    res, events = _run(store, plan, facts, cancel=cancel)
    assert res.cancelled and res.error == "Cancelled — timeline unchanged." and not res.committed
    assert ran == ["apply_lut"]
    assert store.edl.hash() == hash_before                # the first step's edit was rolled back too


def test_validation_unavailable_refuses_before_any_dispatch(session, monkeypatch):
    store, facts = session
    calls: list[str] = []
    monkeypatch.setattr(D, "dispatch", lambda *a, **k: calls.append(a[1]))

    def missing(plan, facts):
        raise executor.PlanValidationUnavailable("validate.py missing")

    plan = F.plan_of(F.step("apply_lut", clip_id="$v1_all", src="warm.cube"))
    res = executor.run_plan(store, plan, facts, emit=lambda e: None, cancel_event=None,
                            prompt="x", validator=missing)
    assert res.error.startswith("Plan refused before any step ran: validate.py missing")
    assert calls == [] and not res.committed


# ---------------------------------------------------------------- sentinels

def test_sentinels_resolve_against_the_live_edl_after_a_cut(session):
    """`cut_range` leaves two fragments with NEW ids; a later `$v1_all` step
    must fan out over both — a pre-plan id would grade only the first."""
    store, facts = session
    before_ids = _v1_ids(store)
    plan = F.plan_of(F.step("cut_range", track="v1", start=4.0, end=6.0),
                     F.step("apply_lut", clip_id="$v1_all", src="warm.cube", intensity=0.7))
    res, events = _run(store, plan, facts)
    after_ids = _v1_ids(store)
    # `cut_range` keeps the left fragment's id and clones the right one with
    # a fresh id — the one a pre-plan `clip_id` could never have named.
    assert len(after_ids) == 2 and after_ids[0] in before_ids and after_ids[1] not in before_ids
    lut_step = res.steps[1]
    assert [a["clip_id"] for a in lut_step.args] == after_ids
    assert len(lut_step.results) == 2
    assert all(any(e.type == "lut" for e in c.effects) for c in store.edl.get_track("v1").clips)
    fanned = [e for e in events if e["type"] == "tool_use" and e["name"] == "apply_lut"][0]
    assert fanned["args"]["clip_id"] == after_ids           # the UI sees every id the step touched
    tr = [e for e in events if e["type"] == "tool_result" and e["name"] == "apply_lut"][0]
    assert len(tr["result"]["results"]) == 2
    prog = [e["progress"] for e in events if e["type"] == "step" and e["tool"] == "apply_lut"]
    assert 0.5 in prog and prog[-1] == 1.0                  # fractional progress across the fan-out


def test_each_sentinel_names_the_right_clips(session):
    store, facts = session
    D.dispatch(store, "cut_range", {"track": "v1", "start": 4.0, "end": 6.0})
    ids = _v1_ids(store)
    edl = store.edl
    f = facts.model_copy(update={"selection": ids[1], "playhead": 7.0})
    assert executor.resolve_clip_ref(edl, "$v1_all", f) == ids
    assert executor.resolve_clip_ref(edl, "$v1_first", f) == [ids[0]]
    assert executor.resolve_clip_ref(edl, "$v1_last", f) == [ids[1]]
    assert executor.resolve_clip_ref(edl, "$selected", f) == [ids[1]]
    assert executor.resolve_clip_ref(edl, "$playhead", f) == [ids[1]]      # 7.0 s is inside the second fragment
    assert executor.resolve_clip_ref(edl, ids[0], f) == [ids[0]]           # a plain id passes through
    with pytest.raises(executor.StepRefused):
        executor.resolve_clip_ref(edl, "$selected", facts)                 # nothing selected
    with pytest.raises(executor.StepRefused):
        executor.resolve_clip_ref(edl, "$playhead", facts.model_copy(update={"playhead": 99.0}))
    args = executor.resolve_step_args(edl, {"clip_ids": ["$v1_first", "$v1_last"], "x": 1}, f)
    assert args == [{"clip_ids": ids, "x": 1}]


def test_the_plan_object_is_unchanged_after_run(session):
    """`dispatch._validate_tool_args` rewrites enum spellings in place; the
    executor must hand it a COPY so the validated Plan stays what it was."""
    store, facts = session
    plan = F.plan_of(F.step("set_clip_fit", clip_id="$v1_first", fit="COVER"))
    snapshot = plan.model_dump()
    args_obj = plan.steps[0].args
    res, _ = _run(store, plan, facts)
    assert res.error is None and res.committed
    assert plan.model_dump() == snapshot
    assert args_obj["fit"] == "COVER" and args_obj["clip_id"] == "$v1_first"
    assert store.edl.get_track("v1").clips[0].fit == "cover"
    assert res.steps[0].args[0]["fit"] == "COVER"           # the dispatched copy carried the caller's spelling


# ---------------------------------------------------------------- the run thread

def _gated_tool(gate: threading.Event, started: threading.Event):
    def tool(store, args):
        started.set()
        assert gate.wait(10.0), "test gate never opened"
        store.edl.markers.append(__import__("video_ai_editor.edl.schema", fromlist=["Marker"]).Marker(
            time=float(args.get("time", 1.0)), label=str(args.get("label", "x"))))
        store.commit("add_marker", args, "marker")
        return {"summary": "marker"}
    return tool


def test_disconnect_does_not_cancel_the_run(session, monkeypatch, identity):
    """Drop the subscriber mid-run; the commit lands and prompt_run.json
    reaches `done` — iOS suspends the app on lock and recovers from history."""
    store, facts = session
    sid = Path(store.dir).name
    gate, started = threading.Event(), threading.Event()
    monkeypatch.setitem(D.DISPATCH, "add_marker", _gated_tool(gate, started))
    plan = F.plan_of(F.step("add_marker", time=1.0, label="hook"), title="mark it")
    handle = executor.start_run(lambda s: store, sid, plan, facts, prompt="mark it")
    assert started.wait(5.0)
    assert locks.prompt_run(sid) == handle.run_id and executor.active_run(sid) is handle
    # A subscriber attaches, sees the running step, then goes away.
    stop = threading.Event()
    seen: list[dict] = []
    sub = threading.Thread(target=lambda: seen.extend(handle.bus.iter_from(0, should_stop=stop.is_set)), daemon=True)
    sub.start()
    time.sleep(0.2)
    stop.set()
    sub.join(3.0)
    assert not sub.is_alive() and any(e["type"] == "step" for e in seen) and not handle.finished
    assert not handle.cancel_event.is_set()
    gate.set()
    handle.thread.join(15.0)
    assert handle.finished and handle.result.committed and handle.result.error is None
    assert store.ops.last().tool == "prompt"
    record = read_record(Path(store.dir))
    assert record["status"] == "done" and record["run_id"] == handle.run_id and record["op"]["tool"] == "prompt"
    assert record["reply"].startswith("via Recipes — ")
    assert locks.prompt_run(sid) is None and executor.active_run(sid) is None
    types = [e["type"] for e in handle.bus.snapshot()]
    assert types[-1] == "done" and types.count("done") == 1 and "verify" in types
    assert types.index("verify") < types.index("op") < types.index("text_delta")  # op after verify (§4.1)


def test_cancel_via_handle_is_the_only_way_to_stop_a_run(session, monkeypatch, identity):
    store, facts = session
    sid = Path(store.dir).name
    gate, started = threading.Event(), threading.Event()
    hash_before = store.edl.hash()

    def slow_then_check(store, args):
        started.set()
        gate.wait(10.0)
        return {"summary": "x"}

    monkeypatch.setitem(D.DISPATCH, "add_marker", slow_then_check)
    plan = F.plan_of(F.step("add_marker", time=1.0), F.step("apply_lut", clip_id="$v1_all", src="warm.cube"))
    handle = executor.start_run(lambda s: store, sid, plan, facts, prompt="x")
    assert started.wait(5.0)
    handle.cancel()
    gate.set()
    handle.thread.join(15.0)
    assert handle.result.cancelled and store.edl.hash() == hash_before
    assert read_record(Path(store.dir))["status"] == "cancelled"
    assert [s.tool for s in handle.result.steps] == ["add_marker"]      # apply_lut never ran


def test_dispatch_answers_409_while_a_prompt_run_holds_the_session(tmp_path: Path, monkeypatch, identity):
    from fastapi.testclient import TestClient
    from video_ai_editor import main as _main, storage as _storage
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path)
    monkeypatch.setattr(_main, "WORKDIR", tmp_path)
    _main._STORES.clear()
    sid = "s_prompt409"
    store = F.make_store(tmp_path, name=sid)
    facts = F.facts_for(store)
    gate, started = threading.Event(), threading.Event()
    monkeypatch.setitem(D.DISPATCH, "add_marker", _gated_tool(gate, started))
    client = TestClient(_main.app)
    plan = F.plan_of(F.step("add_marker", time=1.0, label="x"))
    handle = executor.start_run(_main._store, sid, plan, facts, prompt="x")
    assert started.wait(5.0)
    r = client.post(f"/api/sessions/{sid}/dispatch", json={"tool": "get_timeline", "args": {}})
    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "prompt_running" and body["run_id"] == handle.run_id
    assert body["cancel_url"] == f"/api/sessions/{sid}/prompt/cancel"
    r = client.post(f"/api/sessions/{sid}/dispatch?wait=0", json={"tool": "get_timeline", "args": {}})
    assert r.status_code == 409                                   # the async path refuses too
    gate.set()
    handle.thread.join(15.0)
    r = client.post(f"/api/sessions/{sid}/dispatch", json={"tool": "get_timeline", "args": {}})
    assert r.status_code == 200


def test_history_finalize_replaces_the_provisional_line_in_either_order(session, identity, monkeypatch):
    from video_ai_editor import storage as _storage
    from video_ai_editor.agent.prompt import service
    store, facts = session
    sid = Path(store.dir).name
    monkeypatch.setattr(_storage, "WORKDIR", Path(store.dir).parent)   # chat.json lives in session_dir(sid)
    writer = service.FileHistoryWriter()
    chat = Path(store.dir) / "chat.json"
    plan = F.plan_of(F.step("apply_lut", clip_id="$v1_all", src="warm.cube"), title="look")
    # Order A: the route saved the provisional line before the run finished.
    history = [{"role": "user", "content": "give it a look"},
               {"role": "assistant", "content": [{"type": "text", "text": service.provisional_text("Recipes", "look", "r_a")}]}]
    writer.save(sid, history)
    handle = executor.start_run(lambda s: store, sid, plan, facts, prompt="give it a look",
                                history_writer=writer, run_id="r_a")
    handle.thread.join(15.0)
    saved = json.loads(chat.read_text(encoding="utf-8"))
    assert len(saved) == 2 and saved[1]["content"][0]["text"] == handle.final_text
    assert "(run r_a)" not in saved[1]["content"][0]["text"]
    # Order B: the run finished first; the route's save then carries the stale
    # provisional text, and the SAME replace makes it right.
    late = [{"role": "user", "content": "x"},
            {"role": "assistant", "content": [{"type": "text", "text": service.provisional_text("Recipes", "look", "r_a")}]}]
    fixed = service.replace_provisional(late, "r_a", handle.final_text)
    assert fixed[1]["content"][0]["text"] == handle.final_text and late[1]["content"][0]["text"] != handle.final_text
    assert service.replace_provisional([], "r_zz", "final")[-1]["content"][0]["text"] == "final"


# ---------------------------------------------------------------- transcript wait + progress

def test_transcript_pending_wait_drops_the_transcribe_step_when_the_upload_transcript_lands(tmp_path, monkeypatch):
    src = F.speech_clip(tmp_path)
    F.write_ingest(src, with_transcript=False)
    store = F.make_store(tmp_path, src=src, with_transcript=False)
    facts = F.facts_for(store, transcript_pending=True, has_transcript=False)
    ingest = src.parent / "ingest.json"

    def land_it(_s: float) -> None:
        data = json.loads(ingest.read_text(encoding="utf-8"))
        data["transcript"] = F.transcript()
        ingest.write_text(json.dumps(data), encoding="utf-8")

    real_wait = executor.wait_for_upload_transcript
    monkeypatch.setattr(executor, "wait_for_upload_transcript",
                        lambda *a, **k: real_wait(*a, **{**k, "sleep": land_it, "poll_s": 0.0}))
    plan = F.plan_of(F.step("transcribe"), F.step("remove_fillers", words=["um", "uh"], track="v1"))
    res, events = _run(store, plan, facts)
    assert res.error is None and res.committed
    assert res.steps[0].tool == "transcribe" and res.steps[0].effect == "none" and res.steps[0].results == []
    waiting = [e for e in events if e["type"] == "step" and e.get("summary") == "waiting for upload transcript"]
    assert waiting and waiting[0]["tool"] == "transcribe"
    assert res.steps[1].status == "ok" and res.steps[1].results[0]["cuts"] >= 1


def test_transcript_wait_times_out_and_the_transcribe_step_then_runs(tmp_path, monkeypatch):
    src = F.speech_clip(tmp_path)
    F.write_ingest(src, with_transcript=False)
    store = F.make_store(tmp_path, src=src, with_transcript=False)
    facts = F.facts_for(store, transcript_pending=True, has_transcript=False)
    clock = [0.0]
    monkeypatch.setattr(executor.time, "monotonic", lambda: clock[0])
    real_wait = executor.wait_for_upload_transcript

    def tick(_s: float) -> None:
        clock[0] += 10.0

    monkeypatch.setattr(executor, "wait_for_upload_transcript",
                        lambda *a, **k: real_wait(*a, **{**k, "sleep": tick, "poll_s": 0.0}))
    ran: list[str] = []
    monkeypatch.setitem(D.DISPATCH, "transcribe", lambda s, a, **k: (ran.append("transcribe"), {"summary": "t", "words": 5})[1])
    plan = F.plan_of(F.step("transcribe"))
    res, _ = _run(store, plan, facts)
    assert ran == ["transcribe"] and res.steps[0].status == "ok"


def test_synthetic_progress_for_a_handler_that_reports_none(session, monkeypatch):
    store, facts = session

    def slow(store, args):
        time.sleep(1.3)
        return {"summary": "done"}

    monkeypatch.setattr(executor, "_PROGRESS_TICK_S", 0.2)
    monkeypatch.setitem(D.DISPATCH, "add_marker", slow)
    monkeypatch.setitem(executor._STEP_COST_S, "add_marker", 2.0)
    plan = F.plan_of(F.step("add_marker", time=1.0))
    res, events = _run(store, plan, facts)
    prog = [e["progress"] for e in events if e["type"] == "step" and e["tool"] == "add_marker" and e["status"] == "running"]
    assert any(0.0 < p <= 0.9 for p in prog) and max(prog) <= 0.9
    assert executor._handler_reports_progress("auto_caption") and not executor._handler_reports_progress("add_text")
    assert executor.estimated_step_seconds("auto_caption", 120.0) == 120.0
    assert executor.estimated_step_seconds("add_text", 120.0) == executor._DEFAULT_STEP_COST_S


# ---------------------------------------------------------------- shorts.finish children

def test_finish_children_runs_one_plan_per_child_session_under_its_own_lock(session, monkeypatch, identity):
    from video_ai_editor.agent.prompt import recipes as _recipes, facts as _facts_mod
    store, facts = session
    root = Path(store.dir).parent
    children = {}
    for name in ("short_a", "short_b"):
        children[name] = F.make_store(root, src=F.speech_clip(root), name=name)

    def fake_make_shorts(store, args):
        return {"summary": "2 shorts", "shorts": [], "new_sessions": list(children)}

    monkeypatch.setitem(D.DISPATCH, "make_shorts", fake_make_shorts)
    monkeypatch.setattr(_facts_mod, "build_facts", lambda st, ui, feature_report=None: F.facts_for(st))
    monkeypatch.setattr(_recipes, "from_intents", lambda draft, f: F.plan_of(
        F.step("apply_lut", clip_id="$v1_all", src="warm.cube"), intent="finish"))
    seen: list[tuple[str, bool]] = []
    real_run = executor.run_plan

    def spy_run(st, plan, f, **kw):
        sid = Path(st.dir).name
        seen.append((sid, locks.session_lock(sid).locked()))
        return real_run(st, plan, f, **kw)

    monkeypatch.setattr(executor, "run_plan", spy_run)
    plan = F.plan_of(F.step("make_shorts", target_count=2, max_dur=30, save_as_sessions=True),
                     postconditions=bind_postconditions("make_shorts", {"target_count": 2}) +
                     [__import__("video_ai_editor.agent.prompt.schema", fromlist=["Postcondition"]).Postcondition(
                         check="shorts_finished", args={}, human="each short is finished")])
    assert executor._wants_children(plan)
    handle = executor.start_run(lambda s: store if s == Path(store.dir).name else children[s],
                                Path(store.dir).name, plan, facts, prompt="make shorts")
    handle.thread.join(30.0)
    res = handle.result
    assert res.error is None and res.new_sessions == ["short_a", "short_b"]
    parent_sid = Path(store.dir).name
    children_seen = [(s, locked) for s, locked in seen if s != parent_sid]   # the spy wraps the parent's run too
    assert [s for s, _ in children_seen] == ["short_a", "short_b"] and all(locked for _, locked in children_seen)
    assert [c["status"] for c in res.child_runs] == ["ok", "ok"] and all(c["applied"] == 1 for c in res.child_runs)
    for child in children.values():
        assert child.ops.last().tool == "prompt"                       # each child: its own one-op commit
    events = handle.bus.snapshot()
    assert [e["name"] for e in events if e["type"] == "tool_use" and e["name"] == "finish_short"] == ["finish_short"] * 2
    assert read_record(Path(store.dir))["child_runs"][0]["session"] == "short_a"
    # The parent records ONE op for the shorts even though its tree did not
    # change — the ops log is the only trace of where the shorts came from.
    assert res.committed and store.ops.last().tool == "prompt"


def test_child_failure_is_recorded_and_never_takes_the_parent_down(session, monkeypatch, identity):
    from video_ai_editor.agent.prompt import recipes as _recipes, facts as _facts_mod
    store, facts = session
    monkeypatch.setitem(D.DISPATCH, "make_shorts", lambda s, a: {"summary": "x", "new_sessions": ["ghost"]})
    monkeypatch.setattr(_facts_mod, "build_facts", lambda st, ui, feature_report=None: F.facts_for(st))
    monkeypatch.setattr(_recipes, "from_intents", lambda d, f: F.plan_of())
    plan = F.plan_of(F.step("make_shorts", target_count=1),
                     postconditions=[__import__("video_ai_editor.agent.prompt.schema", fromlist=["Postcondition"]).Postcondition(
                         check="shorts_finished", args={}, human="finished")])

    def resolver(s):
        if s == "ghost":
            raise KeyError("session ghost not found")
        return store

    handle = executor.start_run(resolver, Path(store.dir).name, plan, facts, prompt="x")
    handle.thread.join(15.0)
    assert handle.result.child_runs == [{"session": "ghost", "status": "failed",
                                         "error": "KeyError: 'session ghost not found'"}]
    assert handle.finished and read_record(Path(store.dir))["status"] == "done"


# ---------------------------------------------------------------- §1.4: the consented download path

def test_a_download_answered_yes_is_fetched_as_a_pre_step_and_the_step_dispatches(session, monkeypatch):
    """Before: 'yes' → validate_plan accepted the step (listed in
    downloads_needed) → guard_step refused it as 'not downloaded' → nothing
    ever fetched. Now the consented artefact is fetched first, re-probed, and
    the step runs; without consent the same plan is refused before dispatch."""
    from video_ai_editor.agent.prompt.schema import DownloadNeeded
    store, facts = session
    on_disk = {"small"}
    monkeypatch.setattr(D, "whisper_model_on_disk", lambda m: m in on_disk)
    ran: list = []
    monkeypatch.setitem(D.DISPATCH, "auto_caption", lambda s, a, **k: (ran.append(a), {"summary": "c", "cues": 3})[1])
    fetched: list = []

    def fake_fetch(key):
        fetched.append(key)
        on_disk.add(key.split(":", 1)[1])

    plan = F.plan_of(F.step("auto_caption", model="large-v3", style="ig_chunky"),
                     downloads_needed=[DownloadNeeded(what="faster-whisper large-v3 (3.1 GB)", bytes=3_100_000_000,
                                                      tool="auto_caption")])
    events: list = []
    res = executor.run_plan(store, plan, facts, emit=events.append, cancel_event=threading.Event(),
                            prompt="accurate captions", validator=F.identity_validator,
                            consented_downloads=frozenset({"auto_caption"}), fetch=fake_fetch)
    assert fetched == ["whisper:large-v3"] and ran and ran[0]["model"] == "large-v3"
    assert res.error is None and res.downloads == ["whisper:large-v3"]
    dl = [e for e in events if e["type"] == "step" and e["tool"] == "download"]
    assert [e["status"] for e in dl] == ["running", "ok"] and "you said yes" in dl[0]["summary"]
    assert dl[0]["index"] == 1 == dl[0]["total"]                  # after the real steps, like verify_render

    on_disk.discard("large-v3")
    ran.clear()
    res, _ = _run(store, plan, facts)                              # no consent → refused, never dispatched
    assert ran == [] and res.error and "not downloaded" in res.error


def test_a_failed_consented_download_fails_the_plan_before_any_step(session, monkeypatch):
    from video_ai_editor.agent.prompt.schema import DownloadNeeded
    store, facts = session
    monkeypatch.setattr(D, "whisper_model_on_disk", lambda m: m == "small")
    ran: list = []
    monkeypatch.setitem(D.DISPATCH, "auto_caption", lambda s, a, **k: (ran.append(a), {"summary": "c"})[1])

    def boom(key):
        raise RuntimeError("offline")

    plan = F.plan_of(F.step("auto_caption", model="large-v3"),
                     downloads_needed=[DownloadNeeded(what="large-v3", bytes=1, tool="auto_caption")])
    events: list = []
    res = executor.run_plan(store, plan, facts, emit=events.append, cancel_event=threading.Event(), prompt="x",
                            validator=F.identity_validator, consented_downloads=frozenset({"auto_caption"}), fetch=boom)
    assert ran == [] and res.error == "download of whisper:large-v3 failed: offline. Timeline unchanged."
    assert [e["status"] for e in events if e["type"] == "step" and e["tool"] == "download"] == ["running", "failed"]
    assert not res.committed


# ---------------------------------------------------------------- $v1_seams: transitions after the cuts

def test_seam_sentinel_fans_out_over_the_live_seams_after_the_cuts(session):
    """`auto_edit` with cuts emits ONE add_transition(at=$v1_seams); the
    executor resolves the seams from the edl AFTER the cut_range steps in
    the same batch (the plan-time seams are wrong by construction)."""
    store, facts = session
    plan = F.plan_of(F.step("cut_range", track="v1", start=3.0, end=4.0),          # splits the one clip: seam at 3.0
                     F.step("cut_range", track="v1", start=7.0, end=8.0),          # second seam at 7.0 (post-cut time)
                     F.step("add_transition", at="$v1_seams", type="whip", duration=0.25))
    res, events = _run(store, plan, facts)
    assert res.error is None, res.error
    v1 = store.edl.get_track("v1")
    assert sorted(round(t.at, 3) for t in v1.transitions) == [3.0, 7.0]
    assert all(t.type == "whip" and t.duration == 0.25 for t in v1.transitions)
    use = next(e for e in events if e["type"] == "tool_use" and e["name"] == "add_transition")
    assert use["args"]["at"] == [3.0, 7.0]
    ok = next(e for e in events if e["type"] == "step" and e["tool"] == "add_transition" and e["status"] == "ok")
    assert ok["summary"] == "2 seams"
    assert res.steps[2].args == [{"at": 3.0, "type": "whip", "duration": 0.25}, {"at": 7.0, "type": "whip", "duration": 0.25}]


def test_seam_sentinel_on_a_single_clip_is_refused_not_dispatched(session, monkeypatch):
    store, facts = session
    spy: list = []
    monkeypatch.setitem(D.DISPATCH, "add_transition", lambda s, a: (spy.append(a), {})[1])
    res, _ = _run(store, F.plan_of(F.step("add_transition", at="$v1_seams", type="fade")), facts)
    assert spy == [] and res.error and "names no seam" in res.error


# ---------------------------------------------------------------- the ticker never outlives its step

def test_synthetic_progress_never_publishes_after_the_terminal_frame(session, monkeypatch):
    """`_ProgressTicker.__exit__` joins its thread and the step's `finished`
    flag drops a racing tick: no `running` after `ok`, ever (the run record
    and the desktop row used to flip back to running with nothing to fix it)."""
    store, facts = session
    monkeypatch.setattr(executor, "_PROGRESS_TICK_S", 0.01)
    monkeypatch.setitem(D.DISPATCH, "add_marker", lambda s, a: (time.sleep(0.05), {"summary": "m"})[1])
    monkeypatch.setitem(executor._STEP_COST_S, "add_marker", 1.0)
    for _ in range(20):
        events: list = []
        res = executor.run_plan(store, F.plan_of(F.step("add_marker", time=1.0)), facts, emit=events.append,
                                cancel_event=threading.Event(), prompt="x", validator=F.identity_validator)
        assert res.error is None
        marker = [e for e in events if e["type"] == "step" and e["tool"] == "add_marker"]
        terminal = next(i for i, e in enumerate(marker) if e["status"] == "ok")
        assert all(e["status"] == "ok" for e in marker[terminal:]), [e["status"] for e in marker]
        assert not any(t.name == "vai-prompt-progress" and t.is_alive() for t in threading.enumerate())


# ---------------------------------------------------------------- the transcript follows derived media

def test_reframe_then_captions_still_lays_cues_and_verify_measures_speech(session, monkeypatch):
    """The flagship order (reframe 6 → captions 7): `auto_reframe` rewrites
    every v1 src to cache/reframe_*.mp4. Keyed to the live src the
    transcript vanished (0 cues, speech_preserved unmeasured — the 84.5 s
    TikTok run). With the origin sidecar the cues land and the verifier
    still finds the words."""
    import shutil
    from video_ai_editor.agent.prompt import verify as V
    from video_ai_editor.agent.prompt.schema import Postcondition
    from video_ai_editor.ai import reframe as _reframe
    store, facts = session
    src = Path(store.edl.get_track("v1").clips[0].src)

    def fake_reframe(path, cache_dir, *, target_w, target_h):
        out = Path(cache_dir) / f"reframe_{target_w}x{target_h}.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, out)
        return out

    monkeypatch.setattr(_reframe, "reframe_clip", fake_reframe)
    before = store.edl.model_copy(deep=True)
    plan = F.plan_of(F.step("auto_reframe", ratio="9:16", subject_track=True),
                     F.step("add_caption_track", style="ig_chunky", position="bottom"),
                     postconditions=[Postcondition(check="captions_nonempty", args={}, human="cues"),
                                     Postcondition(check="speech_preserved", args={}, human="words")])
    res, _ = _run(store, plan, facts)
    assert res.error is None, res.error
    new_src = store.edl.get_track("v1").clips[0].src
    assert new_src != str(src) and "reframe_" in new_src
    assert D._first_v1_media_src(store) == str(src)                    # resolved to the upload
    assert D._current_v1_ingest_json(store) == src.parent / "ingest.json"
    cues = store.edl.get_track("captions").clips
    assert len(cues) >= 1, "captions were laid after the reframe"
    result = V.verify_plan(store, plan, res, facts, emit=lambda e: None, render=False)
    by = {c["check"]: c for c in result["checks"]}
    assert by["captions_nonempty"]["pass"] is True
    assert by["speech_preserved"]["pass"] is True and by["speech_preserved"].get("detail") is None
    # and a LATER prompt on the same session still has the transcript
    from video_ai_editor.agent.prompt.facts import build_facts
    later = build_facts(store, None, feature_report={"unavailable": []})
    assert later.has_transcript and later.words == len(F.WORDS) and later.speech_seconds > 0
