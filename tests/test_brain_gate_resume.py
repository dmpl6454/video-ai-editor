"""The analysis gate, answered (EB1 integration; lane E's request to lane F).

`_x_edit` asks "Read the footage first / Stop" when the session has no
Content Graph. On **read** the turn starts the SAME analysis job the
`POST …/brain/analyse` route starts (one function, `brain_seams.start_analysis`),
says how far it is in `analysis` frames, and re-plans the original sentence
when the graph lands — the person answers once and gets their edit. On
**abort** nothing starts and nothing changes. A failed analysis is said, and
the timeline is untouched.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import brain_contract_fixtures as BF  # noqa: E402
import prompt_fixtures as F  # noqa: E402
from prompt_fixtures import desktop_posture, no_downloads  # noqa: E402,F401

from video_ai_editor import config, storage  # noqa: E402
from video_ai_editor.agent.prompt import brain_seams, service  # noqa: E402
from video_ai_editor.edl.schema import Clip  # noqa: E402

pytestmark = pytest.mark.usefixtures("desktop_posture", "no_downloads")
PROMPT = "make a 10-second reel"     # the fixture footage is 12 s: a longer ask is refused (test below)


@pytest.fixture
def session(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "1")
    monkeypatch.delenv("VAI_PROMPT_CONFIRM", raising=False)
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    monkeypatch.setattr(storage, "WORKDIR", tmp_path)
    monkeypatch.setattr(service, "_RESOLVE_STORE", None)
    store = F.make_store(tmp_path, name="s_gate")
    src = next(c for c in store.edl.get_track("v1").clips if isinstance(c, Clip)).src
    calls: list[dict] = []
    monkeypatch.setattr(brain_seams, "_ANALYSIS_JOBS", {})

    def analyse(session_dir, sources, *, set_progress=None, cancel_event=None, layers=None, force=False):
        calls.append({"session_dir": Path(session_dir), "sources": list(sources), "force": force})
        if set_progress:
            set_progress(0.5)
        BF.write_brain_files(Path(session_dir), src, workdir=tmp_path)
        return BF.GID
    monkeypatch.setattr(brain_seams, "analyse_fn", lambda: analyse)
    return store, src, calls


def _ask(store) -> dict:
    events = F.collect(service.prompt_turn(store, PROMPT, [], brain="recipes"))
    clarify = next(e for e in events if e["type"] == "clarify")
    assert [q["key"] for q in clarify["questions"]] == ["gate_analysis"]
    assert [o["value"] for o in clarify["questions"][0]["options"]] == ["read", "abort"]
    return clarify


def test_read_starts_the_analysis_and_replans_the_same_sentence(session):
    store, src, calls = session
    before = store.edl.hash()
    clarify = _ask(store)
    assert calls == [] and store.edl.hash() == before
    events = F.collect(service.resume(store, clarify["token"], {"gate_analysis": "read"}))
    assert events[-1]["type"] == "done" and not [e for e in events if e["type"] == "error"], events
    # ONE analysis, over the session's own media, through the jobs machinery
    assert len(calls) == 1 and calls[0]["session_dir"] == Path(store.dir) and calls[0]["sources"] == [src]
    job = brain_seams.latest_analysis_job(Path(store.dir).name)
    assert job is not None and job.kind == brain_seams.ANALYSE_KIND and job.status == "completed"
    assert job.result["graph_id"] == BF.GID
    # progress is said in `analysis` frames, ending at 100
    frames = [e for e in events if e["type"] == "analysis"]
    assert frames and frames[-1]["pct"] == 100.0 and all(service.is_known_event(e) for e in frames)
    # …then the ORIGINAL sentence is planned on the graph: the brain's plan, not a second question
    plan = next(e["plan"] for e in events if e["type"] == "plan")
    assert "edit" in plan["intent"].split("+")
    refs = {s["args"].get("plan_ref") for s in plan["steps"]} - {None}
    assert len(refs) == 1 and next(iter(refs)).startswith("d_")
    assert not [q for e in events if e["type"] == "clarify" for q in e["questions"] if q["key"] == "gate_analysis"]
    assert events.index(frames[-1]) < next(i for i, e in enumerate(events) if e["type"] == "plan")


def test_abort_starts_nothing_and_changes_nothing(session):
    store, _src, calls = session
    before, ops = store.edl.hash(), len(store.ops.ops)
    clarify = _ask(store)
    events = F.collect(service.resume(store, clarify["token"], {"gate_analysis": "abort"}))
    assert events[-1]["type"] == "done" and not [e for e in events if e["type"] in ("plan", "analysis", "error")]
    text = "".join(e.get("text", "") for e in events if e["type"] == "text_delta")
    assert "Nothing" in text and "changed" in text, text
    assert calls == [] and store.edl.hash() == before and len(store.ops.ops) == ops
    assert brain_seams.latest_analysis_job(Path(store.dir).name) is None


def test_a_failed_analysis_is_said_and_nothing_changes(session, monkeypatch):
    store, _src, _calls = session

    def broken(session_dir, sources, **_kw):
        raise RuntimeError("ffmpeg could not read the file")
    monkeypatch.setattr(brain_seams, "analyse_fn", lambda: broken)
    before = store.edl.hash()
    clarify = _ask(store)
    events = F.collect(service.resume(store, clarify["token"], {"gate_analysis": "read"}))
    assert events[-1]["type"] == "done" and not [e for e in events if e["type"] == "plan"]
    said = " ".join(e.get("message", "") + e.get("text", "") for e in events)
    assert "could not be read" in said and "ffmpeg could not read the file" in said, said
    assert store.edl.hash() == before


def test_the_route_and_the_turn_start_the_same_job(session):
    from video_ai_editor.api import brain_routes
    assert brain_routes.ANALYSE_KIND == brain_seams.ANALYSE_KIND
    assert brain_routes.analysis_event is brain_seams.analysis_event
    assert brain_routes._sources is brain_seams.session_sources


# --------------------------------------------------------------------------
# closer (review race3.py): the wait-for-the-transcript gate outlives the wait
# --------------------------------------------------------------------------

@pytest.fixture
def pending_session(tmp_path: Path, monkeypatch):
    """A fresh upload whose transcript is still being made: ingest.json has none yet."""
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "1")
    monkeypatch.delenv("VAI_PROMPT_CONFIRM", raising=False)
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    monkeypatch.setattr(storage, "WORKDIR", tmp_path)
    monkeypatch.setattr(service, "_RESOLVE_STORE", None)
    store = F.make_store(tmp_path, name="s_gate_pending", with_transcript=False)
    src = next(c for c in store.edl.get_track("v1").clips if isinstance(c, Clip)).src
    calls: list[dict] = []
    monkeypatch.setattr(brain_seams, "_ANALYSIS_JOBS", {})

    def analyse(session_dir, sources, *, set_progress=None, cancel_event=None, layers=None, force=False,
                **_kw):
        calls.append({"sources": list(sources)})
        BF.write_brain_files(Path(session_dir), src, workdir=tmp_path)
        return BF.GID
    monkeypatch.setattr(brain_seams, "analyse_fn", lambda: analyse)
    return store, src, calls


def test_the_still_being_read_gate_can_be_answered_after_the_transcript_lands(pending_session):
    store, src, calls = pending_session
    events = F.collect(service.prompt_turn(store, PROMPT, [], brain="recipes"))
    clarify = next(e for e in events if e["type"] == "clarify")
    assert "still being read" in clarify["questions"][0]["question"]
    F.write_ingest(Path(src), with_transcript=True)                    # the wait the gate asked for ends
    events = F.collect(service.resume(store, clarify["token"], {"gate_analysis": "read"}))
    said = " ".join(e.get("message", "") + e.get("text", "") for e in events)
    assert "no longer applies" not in said, said
    assert [e for e in events if e["type"] == "plan"] and len(calls) == 1, events


def test_only_the_analysis_progress_bit_is_left_out_of_a_gates_hash(pending_session):
    from video_ai_editor.agent.dispatch import dispatch
    from video_ai_editor.agent.prompt import pending
    store, src, _calls = pending_session
    before = F.facts_for(store, has_transcript=False)
    F.write_ingest(Path(src), with_transcript=True)
    landed = F.facts_for(store, has_transcript=True)
    assert pending.facts_hash(before, progress=False) == pending.facts_hash(landed, progress=False)
    assert pending.facts_hash(before) != pending.facts_hash(landed)                 # any other question: unchanged rule
    dispatch(store, "add_clip", {"track": "v1", "src": str(src), "in": 0, "out": 2, "start": 20})
    assert pending.facts_hash(F.facts_for(store, has_transcript=False), progress=False) != pending.facts_hash(before, progress=False)


def test_the_gate_after_a_cancel_says_the_read_is_still_stopping_and_never_the_exception_name(session, monkeypatch):
    """Closer review: after Cancel the next request joined the read that was stopping and was answered 'Cancelled'."""
    import threading
    from video_ai_editor.api.jobs import JobCancelled
    store, src, calls = session
    gate, entered = threading.Event(), threading.Event()

    def slow(session_dir, sources, *, set_progress=None, cancel_event=None, **_kw):
        entered.set()
        gate.wait(10)
        if cancel_event is not None and cancel_event.is_set():
            raise JobCancelled()
        return BF.GID
    monkeypatch.setattr(brain_seams, "analyse_fn", lambda: slow)
    first = brain_seams.start_analysis(store)
    assert entered.wait(10)
    brain_seams.cancel_analysis(Path(store.dir).name)
    clarify = _ask(store)
    events = F.collect(service.resume(store, clarify["token"], {"gate_analysis": "read"}))
    gate.set()
    said = " ".join(e.get("message", "") + e.get("text", "") for e in events)
    assert "still stopping" in said and "Nothing was changed" in said and "AnalysisBusy" not in said, said
    assert "Cancelled." not in said and not [e for e in events if e["type"] == "plan"]
    assert brain_seams.latest_analysis_job(Path(store.dir).name).id == first.id
