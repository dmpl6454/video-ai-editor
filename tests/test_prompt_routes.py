"""`/api/sessions/{sid}/prompt*` and `/api/prompt/*` (api/prompt_routes.py,
spec §4.7) over the real FastAPI app with a TestClient: the SSE routes stream
`data: <json>` frames ending in `done`; pending / run / cancel report the
session's real state; the brains and models routes answer honestly; and
the two model routes — the only network egress in the feature — refuse a
non-loopback caller with 403 and never start a download from a test.
"""
from __future__ import annotations

import importlib
import json
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))
import prompt_fixtures as F  # noqa: E402
from prompt_fixtures import desktop_posture, no_downloads  # noqa: E402,F401

from video_ai_editor.agent.prompt import executor, service  # noqa: E402
from video_ai_editor.agent.prompt.schema import NeedsInput, NeedsInputOption  # noqa: E402
from video_ai_editor.api import prompt_routes  # noqa: E402

D = importlib.import_module("video_ai_editor.agent.dispatch")

pytestmark = pytest.mark.usefixtures("desktop_posture", "no_downloads")

SID = "s_routes"


@pytest.fixture
def app_env(tmp_path: Path, monkeypatch):
    from video_ai_editor import main as _main, storage as _storage
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path)
    monkeypatch.setattr(_main, "WORKDIR", tmp_path)
    monkeypatch.setattr(executor, "_validate_plan", F.identity_validator)
    _main._STORES.clear()
    store = F.make_store(tmp_path, name=SID)
    facts = F.facts_for(store)
    monkeypatch.setattr(service, "build_facts_for", lambda st, ui: facts)
    return TestClient(_main.app), store, facts


def _frames(text: str) -> list[dict]:
    return [json.loads(f[len("data: "):]) for f in text.split("\n\n") if f.startswith("data: ")]


def _question() -> NeedsInput:
    return NeedsInput(key="target_lang", question="Which language?", kind="choice", required=True,
                      options=[NeedsInputOption(value=v, label=v) for v in ("hi", "en")])


# ---------------------------------------------------------------- POST /prompt

def test_prompt_streams_a_full_run_and_saves_history(app_env, monkeypatch):
    client, store, facts = app_env
    plan = F.plan_of(F.step("apply_lut", clip_id="$v1_all", src="warm.cube"), title="warm look")
    F.route_with(monkeypatch, F.FakeRouted(plan))
    r = client.post(f"/api/sessions/{SID}/prompt", json={"message": "warm look", "playhead": 1.0, "selection": None})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache"
    frames = _frames(r.text)
    types = [f["type"] for f in frames]
    assert types[0] == "brain" and "plan" in types and "verify" in types and "op" in types and types[-1] == "done"
    assert all("\n" not in json.dumps(f) for f in frames)
    executor.get_run(SID).thread.join(10.0)
    history = json.loads((store.dir / "chat.json").read_text(encoding="utf-8"))
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert history[1]["content"][0]["text"].startswith("via Recipes — warm look: done")


def test_prompt_undo_rewinds_the_whole_run_and_streams_one_op_frame(app_env, monkeypatch):
    """`undo that` after a prompt run, planned by the real router: the
    session (as the app's own store serves it) is back at its pre-run EDL
    and op count, exactly one `op` frame tells the desktop/phone to refresh,
    `redo_available` flips, and chat.json carries the reply."""
    real_route = service._route
    client, store, facts = app_env
    before = client.get(f"/api/sessions/{SID}").json()
    edl_before = client.get(f"/api/sessions/{SID}/edl").json()
    plan = F.plan_of(F.step("apply_lut", clip_id="$v1_all", src="warm.cube"), title="warm look")
    F.route_with(monkeypatch, F.FakeRouted(plan))
    assert client.post(f"/api/sessions/{SID}/prompt", json={"message": "warm look"}).status_code == 200
    executor.get_run(SID).thread.join(10.0)
    after = client.get(f"/api/sessions/{SID}").json()
    assert len(after["ops"]) == len(before["ops"]) + 1 and after["ops"][-1]["tool"] == "prompt"
    assert client.get(f"/api/sessions/{SID}/edl").json() != edl_before

    monkeypatch.setattr(service, "_route", real_route)
    r = client.post(f"/api/sessions/{SID}/prompt", json={"message": "undo that", "brain": "recipes"})
    assert r.status_code == 200
    frames = _frames(r.text)
    types = [f["type"] for f in frames]
    assert types.count("op") == 1 and "error" not in types and types[-1] == "done", types
    assert next(f for f in frames if f["type"] == "plan")["plan"]["intent"] == "undo"
    assert frames[types.index("op")]["op"]["tool"] == "undo"
    reply = next(f for f in frames if f["type"] == "text_delta")["text"]
    assert reply.startswith("via Recipes — Undid: Prompt: warm look") and reply.endswith("Redo with ⇧⌘Z.")
    now = client.get(f"/api/sessions/{SID}").json()
    assert now["ops"] == before["ops"] and now["redo_available"] is True
    assert client.get(f"/api/sessions/{SID}/edl").json() == edl_before
    history = json.loads((store.dir / "chat.json").read_text(encoding="utf-8"))
    assert history[-1]["role"] == "assistant" and history[-1]["content"][0]["text"] == reply


def test_prompt_requires_a_message_and_a_real_session(app_env):
    client, *_ = app_env
    assert client.post(f"/api/sessions/{SID}/prompt", json={"message": "   "}).status_code == 400
    assert client.post("/api/sessions/nope/prompt", json={"message": "x"}).status_code == 404
    assert client.post(f"/api/sessions/{SID}/prompt", json={"message": "x" * 5000}).status_code == 422


def test_prompt_resume_run_replays_from_an_index(app_env, monkeypatch):
    client, store, facts = app_env
    F.route_with(monkeypatch, F.FakeRouted(F.plan_of(F.step("apply_lut", clip_id="$v1_all", src="warm.cube"))))
    first = _frames(client.post(f"/api/sessions/{SID}/prompt", json={"message": "x"}).text)
    run_id = executor.get_run(SID).run_id
    again = _frames(client.post(f"/api/sessions/{SID}/prompt", json={"message": "", "resume_run": run_id}).text)
    assert again == first
    tail = _frames(client.post(f"/api/sessions/{SID}/prompt",
                               json={"message": "", "resume_run": run_id, "from_index": len(first) - 2}).text)
    assert tail == first[-2:]
    assert client.post(f"/api/sessions/{SID}/prompt", json={"resume_run": run_id, "from_index": -1}).status_code == 422


# ---------------------------------------------------------------- pending / answer / cancel / run

def test_pending_answer_and_run_lifecycle(app_env, monkeypatch):
    client, store, facts = app_env
    assert client.get(f"/api/sessions/{SID}/prompt/pending").json() == {"pending": None}
    assert client.get(f"/api/sessions/{SID}/prompt/run").json() == {"run": None, "live": False, "replayable": False}
    plan = F.plan_of(F.step("apply_lut", clip_id="$v1_all", src="warm.cube"),
                     F.step("translate_captions", target_lang=None, _optional=True),
                     needs_input=[_question()], title="translate")
    F.route_with(monkeypatch, F.FakeRouted(plan))
    frames = _frames(client.post(f"/api/sessions/{SID}/prompt", json={"message": "translate"}).text)
    clarify = next(f for f in frames if f["type"] == "clarify")
    body = client.get(f"/api/sessions/{SID}/prompt/pending").json()["pending"]
    assert body["token"] == clarify["token"] and body["plan_id"] == plan.id and body["prompt"] == "translate"
    assert body["questions"][0]["key"] == "target_lang" and 0 < body["expires_in_s"] <= service.CLARIFY_TTL_S
    assert body["brain"] == "recipes"
    # A stale token is refused with a frame, not a 500.
    bad = _frames(client.post(f"/api/sessions/{SID}/prompt/answer", json={"token": "q_stale", "answers": {}}).text)
    assert [f["type"] for f in bad] == ["error", "done"] and "no pending question" in bad[0]["message"]
    ok = _frames(client.post(f"/api/sessions/{SID}/prompt/answer",
                             json={"token": clarify["token"], "answers": {"target_lang": "en"}}).text)
    assert ok[0] == {"type": "brain", "status": "answered", "brain": "recipes", "label": "Recipes"}
    assert ok[1]["type"] == "plan" and ok[1]["plan"]["steps"][1]["args"]["target_lang"] == "en"
    assert ok[-1]["type"] == "done" and any(f["type"] == "op" for f in ok)
    assert client.get(f"/api/sessions/{SID}/prompt/pending").json() == {"pending": None}
    executor.get_run(SID).thread.join(10.0)
    run = client.get(f"/api/sessions/{SID}/prompt/run").json()
    assert run["live"] is False and run["replayable"] is True
    assert run["run"]["status"] == "done" and run["run"]["plan_id"] == plan.id and run["run"]["op"]["tool"] == "prompt"
    assert [s["status"] for s in run["run"]["steps"]] == ["ok", "ok"]
    history = json.loads((store.dir / "chat.json").read_text(encoding="utf-8"))
    assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant"]
    assert history[2]["content"] == "target_lang: en"


def test_expired_pending_is_dropped_on_read(app_env, monkeypatch):
    from video_ai_editor.agent.prompt import pending
    client, store, facts = app_env
    record = pending.save_pending(Path(store.dir), plan=F.plan_of(needs_input=[_question()]), prompt="p", facts=facts)
    data = json.loads(pending.pending_path(Path(store.dir)).read_text())
    data["expires"] = 1.0
    pending.pending_path(Path(store.dir)).write_text(json.dumps(data))
    body = client.get(f"/api/sessions/{SID}/prompt/pending").json()
    assert body == {"pending": None, "dropped": "the question expired"}
    assert pending.load_pending(Path(store.dir)) is None
    assert record["token"] != ""


def test_cancel_stops_a_live_run_and_drops_the_question(app_env, monkeypatch):
    from video_ai_editor.agent.prompt import pending
    client, store, facts = app_env
    gate, started = threading.Event(), threading.Event()

    def slow(store, args):
        started.set()
        gate.wait(10.0)
        return {"summary": "x"}

    monkeypatch.setitem(D.DISPATCH, "add_marker", slow)
    plan = F.plan_of(F.step("add_marker", time=1.0), F.step("apply_lut", clip_id="$v1_all", src="warm.cube"))
    handle = executor.start_run(lambda s: store, SID, plan, facts, prompt="x")
    assert started.wait(5.0)
    live = client.get(f"/api/sessions/{SID}/prompt/run").json()
    assert live["live"] is True and live["run"]["status"] == "running"
    pending.save_pending(Path(store.dir), plan=F.plan_of(needs_input=[_question()]), prompt="q", facts=facts)
    r = client.post(f"/api/sessions/{SID}/prompt/cancel", json={})
    assert r.status_code == 200
    assert r.json()["cancelled"]["run"] == handle.run_id and r.json()["cancelled"]["pending"].startswith("q_")
    gate.set()
    handle.thread.join(10.0)
    assert handle.result.cancelled and pending.load_pending(Path(store.dir)) is None
    assert client.post(f"/api/sessions/{SID}/prompt/cancel").json() == {"cancelled": {"run": None, "pending": None}}
    # Cancelling with a token only drops THAT question.
    pending.save_pending(Path(store.dir), plan=F.plan_of(needs_input=[_question()]), prompt="q", facts=facts)
    assert client.post(f"/api/sessions/{SID}/prompt/cancel", json={"token": "q_other"}).json()["cancelled"]["pending"] is None
    assert pending.load_pending(Path(store.dir)) is not None


# ---------------------------------------------------------------- global routes

def test_brains_route_is_honest_about_this_machine(app_env):
    client, *_ = app_env
    body = client.get("/api/prompt/brains").json()
    assert {"ladder", "pinned", "best_available", "brains", "cloud_allowed", "machine", "generated_at"} <= set(body)
    rows = {b["id"]: b for b in body["brains"]}
    assert set(rows) == {"recipes", "apple_intelligence", "local_model", "claude"}
    assert rows["recipes"]["available"] is True and "recipes" in rows["recipes"]["detail"]
    for b in rows.values():
        assert {"label", "order", "in_ladder", "available", "detail", "fix", "action", "model"} <= set(b)
        if not b["available"]:
            assert b["fix"], f"{b['id']} is unavailable without a fix"
    assert rows["claude"]["available"] is False                     # ANTHROPIC_API_KEY="" in the gate
    assert body["cloud_allowed"] in (True, False)
    fresh = client.get("/api/prompt/brains?refresh=1").json()
    assert fresh["generated_at"] >= body["generated_at"]


def test_brains_fallback_report_has_the_same_shape(monkeypatch):
    real = prompt_routes._brains_report(False)
    fallback = prompt_routes._fallback_brains_report()
    assert set(real) <= set(fallback) | {"detail"}
    assert [b["id"] for b in fallback["brains"]] == [b["id"] for b in real["brains"]]
    assert fallback["best_available"] == "recipes" and fallback["brains"][0]["available"] is True


def test_models_route_lists_the_tier_and_never_offers_a_download_for_a_cached_model(app_env):
    client, *_ = app_env
    body = client.get("/api/prompt/models").json()
    assert "tier" in body and isinstance(body["models"], list)
    for m in body["models"]:
        assert {"id", "installed"} <= set(m)
        assert "0.5B" not in m["id"]                                 # never offered (finding 12)


def test_model_download_and_delete_are_loopback_only_and_run_as_jobs(app_env, monkeypatch):
    client, *_ = app_env
    from video_ai_editor.agent.prompt import models as _models
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(_models, "download", lambda mid, set_progress=None, cancel_event=None: calls.append(("download", mid)) or {"ok": True})
    monkeypatch.setattr(_models, "delete", lambda mid, set_progress=None, cancel_event=None: calls.append(("delete", mid)) or {"ok": True})
    r = client.post("/api/prompt/models/download", json={"id": "mlx-community/Qwen2.5-7B-Instruct-4bit"})
    assert r.status_code == 202 and set(r.json()) == {"job_id", "status", "status_url"}
    job = client.get(r.json()["status_url"])
    assert job.status_code == 200
    r = client.post("/api/prompt/models/delete", json={"id": "mlx-community/Qwen2.5-7B-Instruct-4bit"})
    assert r.status_code == 202
    # A paired phone (non-loopback) may not start a 4 GB download on the Mac.
    monkeypatch.setattr(prompt_routes, "_is_loopback", lambda request: False)
    r = client.post("/api/prompt/models/download", json={"id": "x"})
    assert r.status_code == 403 and "desktop_only" in r.text and "FORBIDDEN" in r.text
    assert client.post("/api/prompt/models/delete", json={"id": "x"}).status_code == 403
    assert client.post("/api/prompt/models/download", json={}).status_code == 422
    import time
    deadline = time.time() + 5
    while time.time() < deadline and len(calls) < 2:
        time.sleep(0.05)
    assert sorted(calls) == [("delete", "mlx-community/Qwen2.5-7B-Instruct-4bit"),
                             ("download", "mlx-community/Qwen2.5-7B-Instruct-4bit")]


def test_routes_refuse_before_configure(monkeypatch):
    monkeypatch.setattr(prompt_routes, "_RESOLVE_STORE", None)
    with pytest.raises(Exception) as ei:
        prompt_routes._store("any")
    assert getattr(ei.value, "status_code", None) == 503


def test_route_table_matches_the_mounted_app():
    from video_ai_editor.main import app
    mounted = {(m, r.path) for r in app.routes for m in getattr(r, "methods", set())}
    for name, (method, path) in service.ROUTES.items():
        assert (method, path) in mounted, f"{name}: {method} {path} is not mounted"
        assert "{sid}" in path or name in ("brains", "models", "models_download", "models_delete")
    assert not any("/None/" in r.path for r in app.routes)         # route("prompt") once formatted {sid} to "None"
    assert service.route("prompt") == "/api/sessions/{sid}/prompt"
    assert prompt_routes.prompt_running_response("s_none") is None


# ---------------------------------------------------------------- answers reach the steps (no `$ask:` ever dispatches)

def _real_validator(monkeypatch):
    from video_ai_editor.agent.prompt.validate import validate_plan
    monkeypatch.setattr(executor, "_validate_plan", validate_plan)


@pytest.mark.parametrize("prompt, answer, tool, expect", [
    ("trim it", {"range": "the first 5 seconds"}, "cut_range", {"start": 0.0, "end": 5.0}),
    ("add background music", {"music_src": "bed.wav"}, "add_music", {"src": "bed.wav"}),
    ("export preset", {"platform": "tiktok"}, "apply_export_preset", {"name": "tiktok"}),
])
def test_answering_a_planner_question_binds_the_placeholder_end_to_end(app_env, monkeypatch, prompt, answer, tool, expect):
    """Live before the fix: 'trim it' → clarify(range) → 'the first 5
    seconds' → cut_range{start:'$ask:range'} → 'must be of type number' →
    run failed. The whole route now: plan → clarify → answer → dispatch with
    the bound value, and no `$ask:` string ever reaches dispatch."""
    from video_ai_editor.agent.prompt import planner as P
    client, store, facts = app_env
    _real_validator(monkeypatch)
    bed = F.music_bed(store.dir.parent)
    facts2 = facts.with_(uploads_audio=[str(bed.resolve()), str((bed.parent / "other.wav").resolve())],
                         allowed_paths=set(facts.allowed_paths) | {str(bed.resolve()), str((bed.parent / "other.wav").resolve())},
                         has_transcript=False)
    (bed.parent / "other.wav").write_bytes(bed.read_bytes())
    monkeypatch.setattr(service, "build_facts_for", lambda st, ui: facts2)
    plan = P.plan(prompt, facts2)
    assert plan.blocking_questions, plan
    F.route_with(monkeypatch, F.FakeRouted(plan))
    seen: list = []
    real = D.dispatch

    def spy(store_, tool_, args, **kw):
        seen.append((tool_, dict(args)))
        assert not any(isinstance(v, str) and v.startswith("$ask:") for v in args.values()), (tool_, args)
        return real(store_, tool_, args, **kw)

    monkeypatch.setattr(D, "dispatch", spy)
    first = _frames(client.post(f"/api/sessions/{SID}/prompt", json={"message": prompt}).text)
    clarify = next(f for f in first if f["type"] == "clarify")
    if "music_src" in answer:
        answer = {"music_src": str(bed.resolve())}
        expect = {"src": str(bed.resolve())}
    second = _frames(client.post(f"/api/sessions/{SID}/prompt/answer", json={"token": clarify["token"], "answers": answer}).text)
    executor.get_run(SID).thread.join(20.0)
    assert second[-1]["type"] == "done" and not [f for f in second if f["type"] == "error"], second
    call = next((a for t, a in seen if t == tool), None)
    assert call is not None, seen
    for k, v in expect.items():
        assert call[k] == v, (k, call)
    replayed = next(f for f in second if f["type"] == "plan")["plan"]
    assert not any(isinstance(v, str) and v.startswith("$ask:") for st in replayed["steps"] for v in st["args"].values())


# ---------------------------------------------------------------- the upload ingresses obey the run lock

def test_audio_upload_answers_409_while_a_prompt_run_holds_the_session(app_env, monkeypatch):
    """An upload landing DURING a run's batch had its commit swallowed (folded
    into the run's op or wiped by its rollback) after answering 200."""
    client, store, facts = app_env
    gate, started = threading.Event(), threading.Event()

    def gated(store_, args):
        started.set()
        gate.wait(10.0)
        return {"summary": "m"}

    monkeypatch.setitem(D.DISPATCH, "add_marker", gated)
    from video_ai_editor import main as _main
    handle = executor.start_run(_main._store, SID, F.plan_of(F.step("add_marker", time=1.0)), facts, prompt="x")
    assert started.wait(5.0)
    try:
        wav = F.music_bed(store.dir.parent, name="late.wav")
        r = client.post(f"/api/sessions/{SID}/audio_upload", files={"file": ("late.wav", wav.read_bytes(), "audio/wav")})
        assert r.status_code == 409 and r.json()["code"] == "prompt_running" and r.json()["run_id"] == handle.run_id
        r = client.post(f"/api/sessions/{SID}/subtitle_upload", files={"file": ("c.srt", b"1\n00:00:00,000 --> 00:00:01,000\nhi\n", "text/plain")})
        assert r.status_code == 409
        r = client.post(f"/api/sessions/{SID}/vo_record", files={"file": ("v.wav", wav.read_bytes(), "audio/wav")})
        assert r.status_code == 409
        assert store.edl.get_track("music") is None or not store.edl.get_track("music").clips
    finally:
        gate.set()
        handle.thread.join(15.0)
    r = client.post(f"/api/sessions/{SID}/audio_upload", files={"file": ("late.wav", wav.read_bytes(), "audio/wav")})
    assert r.status_code == 200 and _main._store(SID).edl.get_track("music").clips      # the app's own store object
