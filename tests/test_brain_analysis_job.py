"""EB1 fix wave, lane FX-A: the analysis job and versions.

UX-01  analysis raced the upload's transcription and pinned a speech-less graph
SC-01  a double quote in a hook sentence failed the whole analysis (PathLeak)
SC-07  no single flight per session; the store's temp file name was per process
UX-07  cancel + real progress (layer names) on the analysis events
SC-13  GET …/brain/graph size cap + the store's current-graph rule
SC-14  absolute paths in analysis failure text
SC-06 / EX-05  the same plan applied twice recorded two identically named versions

The media is one 8 s synthetic tone clip with a hand-written transcript
(ingest.json beside it), so nothing here needs whisper or the fixture cache.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_load_project_by_content import client, workdir  # noqa: E402,F401  (fixtures: the real app on a temp WORKDIR)

from video_ai_editor import config  # noqa: E402
from video_ai_editor import platformutil as _pu  # noqa: E402

SENTENCES = ["Never buy the cheap one.", "It always breaks in a week.", "Here is why that matters a lot.",
             "Trust me on this one."]


def _transcript(sentences=SENTENCES) -> dict:
    segs, t = [], 0.2
    for s in sentences:
        t0, words = t, []
        for w in s.split():
            words.append({"word": w, "start": round(t, 2), "end": round(t + 0.3, 2), "probability": 0.9})
            t += 0.32
        segs.append({"start": t0, "end": round(t, 2), "text": s, "words": words})
        t += 0.3
    return {"language": "en", "duration": 8.0, "segments": segs}


@pytest.fixture(scope="module")
def clip_bytes(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("fxa-media") / "clip.mp4"
    subprocess.run([_pu.FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=220:duration=8:sample_rate=16000", "-f", "lavfi", "-i",
                    "color=c=black:s=160x90:r=10:d=8", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(out)], check=True, **_pu.SUBPROCESS_FLAGS)
    return out


@pytest.fixture
def work(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(config, "WORKDIR", tmp_path / "work")
    from video_ai_editor.agent.prompt import brain_seams
    monkeypatch.setattr(brain_seams, "_ANALYSIS_JOBS", {})
    return tmp_path


def _upload(work: Path, clip_bytes: Path, *, transcript: dict | None | str = "none", age_s: float = 0.0,
            name: str = "up") -> Path:
    """An upload folder: the clip and its ingest.json. `transcript=None`
    writes the file the way `ingest_upload(transcribe_audio=False)` does —
    the transcript is still to come; `"none"` writes no ingest.json."""
    d = work / "uploads" / name
    d.mkdir(parents=True, exist_ok=True)
    shutil.copy(clip_bytes, d / "clip.mp4")
    if transcript != "none":
        (d / "ingest.json").write_text(json.dumps({"src": "clip.mp4", "transcript": transcript}), encoding="utf-8")
        if age_s:
            past = time.time() - age_s
            os.utime(d / "ingest.json", (past, past))
    return d / "clip.mp4"


def _land_transcript(media: Path, delay_s: float = 0.0) -> threading.Thread:
    """What `_background_transcriber` does: rewrite ingest.json with the transcript."""
    def run() -> None:
        time.sleep(delay_s)
        p = media.parent / "ingest.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        data["transcript"] = _transcript()
        p.write_text(json.dumps(data), encoding="utf-8")
    th = threading.Thread(target=run, daemon=True)
    th.start()
    return th


# ---------------------------------------------------------------------------
# SC-01
# ---------------------------------------------------------------------------

def test_a_double_quote_is_not_a_path():
    from video_ai_editor.brain import digest
    payload = digest.rank_moments_payload([{"id": "s_1", "t0": 0.0, "t1": 2.0, "text": 'He said "no" to me, it\'s fine'}])
    assert payload["windows"][0]["text"] == 'He said "no" to me, it\'s fine'
    digest.assert_path_free({"windows": [{"start": 0, "end": 1, "text": 'a "quoted" bit'}], "k": 1})


@pytest.mark.parametrize("leak", [
    "/Users/alex/Movies/talk.mov", "~/Movies/talk", "see /tmp/x/y for it", "C:\\Users\\me\\clip",
    "\\\\server\\share\\clip", "clip.mp4", "notes/cut.json", "../../etc/passwd", "/private/var/folders/aa/bb",
])
def test_a_real_path_is_still_refused(leak):
    from video_ai_editor.brain import digest
    with pytest.raises(digest.PathLeak):
        digest.assert_path_free({"windows": [{"start": 0, "end": 1, "text": leak}], "k": 1})
    with pytest.raises(digest.PathLeak):
        digest.assert_path_free({leak: 1})
    with pytest.raises(digest.PathLeak):
        digest.assert_path_free([1, [{"deep": {"er": [leak]}}]])


def test_the_builder_scrubs_a_path_before_the_guard_sees_it():
    from video_ai_editor.brain import digest
    p = digest.rank_moments_payload([{"id": "s_1", "t0": 0, "t1": 1, "text": 'open /Users/me/a.wav and say "hi"'}])
    assert "/Users" not in json.dumps(p) and '"hi"' in p["windows"][0]["text"]


class _Off:
    """A provider that reports itself unavailable."""
    id, engine_version = "off", "0"

    def probe(self):
        return {"available": False}

    def run(self, payload, *, timeout_s):
        raise AssertionError("never asked")


def _golden_layers(quote: bool = True) -> tuple[dict, dict]:
    """The talking-head golden's speech and audio layers, s_0008 (the quotable line) opened with a quote."""
    g = json.loads((Path(__file__).parent / "goldens" / "brain" / "graphs" / "talking_head.json").read_text("utf-8"))
    speech, audio = g["layers"]["speech"], g["layers"]["audio"]
    if quote:
        for s in speech["sentences"]:
            if s["id"] == "s_0008":
                s["text"] = 'He said "never" and ' + s["text"]
    return speech, audio


def test_a_quoted_hook_sentence_does_not_fail_the_semantic_layer():
    """The re-tester's repro: PathLeak from gateway.prompt_hash before the provider is even probed."""
    from video_ai_editor.brain.analysis import semantic as SEM
    from video_ai_editor.brain.gateway import Gateway
    speech, audio = _golden_layers()
    layer = SEM.build_semantic_layer(speech, audio, gateway=Gateway([_Off()]))
    assert layer["scores"]["s_0008"]["hook"] > 0
    assert SEM.layer_status(layer) == "ok"          # no provider is not a failure (unchanged)


def test_a_payload_or_provider_error_degrades_to_heuristics_and_marks_partial():
    from video_ai_editor.brain.analysis import semantic as SEM
    from video_ai_editor.brain.digest import PathLeak
    from video_ai_editor.brain.gateway import Gateway
    speech, audio = _golden_layers(quote=False)
    plain = SEM.build_semantic_layer(speech, audio, gateway=None)

    class Broken(Gateway):
        def prompt_hash(self, sentences):
            raise PathLeak("path-like token in a model payload")

    class Boom(Gateway):
        def prompt_hash(self, sentences):
            return "sha256:0"

        def rank_moments(self, sentences, *, budget_s=60.0):
            raise RuntimeError("helper crashed")

    for gw in (Broken([_Off()]), Boom([_Off()])):
        layer = SEM.build_semantic_layer(speech, audio, gateway=gw)
        assert SEM.layer_status(layer) == "partial" and layer["budget"]["partial_from"] is not None
        assert SEM.canonical_content(layer) == SEM.canonical_content(plain)     # heuristic scores, unchanged


def test_analyse_survives_a_gateway_that_raises(work, clip_bytes):
    from video_ai_editor.brain import graph
    from video_ai_editor.brain.digest import PathLeak
    from video_ai_editor.brain.gateway import Gateway
    lines = ['He said "never" and meant it.', "Never buy the cheap one.", "It always breaks in a week.",
             "Here is why that matters a lot.", "Trust me on this one.", "Stop wasting your money today.",
             "Always read the reviews first.", "That is the only rule."]
    media = _upload(work, clip_bytes, transcript=_transcript(lines), name="qp")

    class Broken(Gateway):
        def prompt_hash(self, sentences):
            raise PathLeak("path-like token in a model payload")

    gid = graph.analyse(work / "sess-qp", [media], gateway=Broken([_Off()]))
    g = graph.load_graph(work / "sess-qp", gid)
    assert g["sources"][0]["layers"]["speech"] == "ok" and g["sources"][0]["layers"]["semantic"] in ("ok", "partial")


# ---------------------------------------------------------------------------
# SC-07: unique temp names, single flight
# ---------------------------------------------------------------------------

def _fake_store(work: Path, sid: str = "s_flight"):
    d = work / sid
    d.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(dir=d, edl=SimpleNamespace(tracks=[]))


def test_a_second_analysis_of_the_session_joins_the_running_one(work, monkeypatch):
    from video_ai_editor.agent.prompt import brain_seams
    gate, entered, calls = threading.Event(), threading.Event(), []

    def analyse(session_dir, sources, *, set_progress=None, cancel_event=None, **_kw):
        calls.append(1)
        entered.set()
        assert gate.wait(10)
        return "g_aaaaaaaaaaaa"
    monkeypatch.setattr(brain_seams, "analyse_fn", lambda: analyse)
    st = _fake_store(work)
    first = brain_seams.start_analysis(st)
    assert entered.wait(10)
    second = brain_seams.start_analysis(st)
    assert second.id == first.id
    # a forced rebuild or a layer subset cannot ride on a run that is not doing it: refused, with a sentence
    with pytest.raises(brain_seams.AnalysisBusy, match="already being read"):
        brain_seams.start_analysis(st, force=True)
    with pytest.raises(brain_seams.AnalysisBusy):
        brain_seams.start_analysis(st, layers=["audio"])
    gate.set()
    deadline = time.time() + 10
    while first.status in ("queued", "running"):
        assert time.time() < deadline
        time.sleep(0.02)
    assert first.status == "completed" and calls == [1]
    # once it is done a new one may start
    third = brain_seams.start_analysis(st)
    assert third.id != first.id


def test_a_request_after_a_cancel_never_joins_the_read_that_is_stopping(work, monkeypatch):
    """Closer review: cancel only sets the job's cancel_event (the job stays 'running' until its next layer
    boundary), so the next request JOINED it and was answered 'Cancelled. Nothing was changed.' for a request the
    person had just made. A read that is stopping is refused with a sentence, then a fresh one starts."""
    from video_ai_editor.agent.prompt import brain_seams
    from video_ai_editor.api.jobs import JobCancelled
    gate, entered = threading.Event(), threading.Event()

    def analyse(session_dir, sources, *, set_progress=None, cancel_event=None, **_kw):
        entered.set()
        assert gate.wait(10)                      # ignores its cancel_event until released (a PCM decode)
        if cancel_event is not None and cancel_event.is_set():
            raise JobCancelled()
        return "g_bbbbbbbbbbbb"
    monkeypatch.setattr(brain_seams, "analyse_fn", lambda: analyse)
    st = _fake_store(work)
    first = brain_seams.start_analysis(st)
    assert entered.wait(10)
    assert brain_seams.cancel_analysis(Path(st.dir).name) == first.id
    assert first.status == "running"                                     # still stopping
    with pytest.raises(brain_seams.AnalysisBusy, match="stopping"):
        brain_seams.start_analysis(st)
    # the UI cancels through the job route, which only sets the event: the same rule
    gate.set()
    deadline = time.time() + 10
    while first.status in ("queued", "running"):
        assert time.time() < deadline
        time.sleep(0.02)
    assert first.status == "cancelled"
    again = brain_seams.start_analysis(st)
    assert again.id != first.id


def test_a_job_cancelled_through_the_jobs_route_is_stopping_too(work, monkeypatch):
    from video_ai_editor.agent.prompt import brain_seams
    from video_ai_editor.api.jobs import JOB_MANAGER
    gate, entered = threading.Event(), threading.Event()

    def analyse(session_dir, sources, *, set_progress=None, cancel_event=None, **_kw):
        entered.set()
        gate.wait(10)
        return "g_cccccccccccc"
    monkeypatch.setattr(brain_seams, "analyse_fn", lambda: analyse)
    st = _fake_store(work)
    first = brain_seams.start_analysis(st)
    assert entered.wait(10)
    JOB_MANAGER.cancel(first.id)                 # POST /api/jobs/{id}/cancel: no _CANCELLED entry
    with pytest.raises(brain_seams.AnalysisBusy, match="stopping"):
        brain_seams.start_analysis(st)
    gate.set()


def test_the_analyse_route_joins_and_refuses_with_a_clear_message(work, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from video_ai_editor.agent.prompt import brain_seams
    from video_ai_editor.api import brain_routes
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "1")
    gate, entered = threading.Event(), threading.Event()

    def analyse(session_dir, sources, *, set_progress=None, cancel_event=None, **_kw):
        entered.set()
        assert gate.wait(10)
        return "g_bbbbbbbbbbbb"
    monkeypatch.setattr(brain_seams, "analyse_fn", lambda: analyse)
    st = _fake_store(work, "s_route")
    monkeypatch.setattr(brain_routes, "_RESOLVE_STORE", lambda sid: st)      # restored after the test
    app = FastAPI()
    app.include_router(brain_routes.router)
    c = TestClient(app)
    a = c.post("/api/sessions/s_route/brain/analyse", json={})
    assert a.status_code == 202 and entered.wait(10)
    b = c.post("/api/sessions/s_route/brain/analyse", json={})
    assert b.status_code == 202 and b.json()["job_id"] == a.json()["job_id"] and b.json().get("joined") is True
    r = c.post("/api/sessions/s_route/brain/analyse", json={"force": True})
    assert r.status_code == 409 and "already being read" in r.text
    gate.set()


# ---------------------------------------------------------------------------
# UX-01: never pin a graph made without the transcript that is on its way
# ---------------------------------------------------------------------------

def test_analysis_does_not_pin_a_graph_while_the_transcript_is_still_coming(work, clip_bytes):
    """The re-tester's race: the upload answered (ingest.json exists, no transcript yet), whisper is
    still running, the person typed a prompt."""
    from video_ai_editor.brain import graph, store
    media = _upload(work, clip_bytes, transcript=None)
    sess = work / "sess"
    gid = graph.analyse(sess, [media], gateway=None)
    assert getattr(gid, "pinned", True) is False and getattr(gid, "waiting_for", None) == "transcript"
    assert store.current_graph_id(sess) is None
    assert not (sess / "brain" / "graph" / "current.json").exists()
    g = graph.load_graph(sess, gid)
    assert g["sources"][0]["layers"]["speech"] == "missing"


def test_analysis_waits_for_the_transcript_and_then_pins_a_graph_with_speech(work, clip_bytes):
    from video_ai_editor.brain import graph, store
    media = _upload(work, clip_bytes, transcript=None)
    sess, seen = work / "sess", []
    _land_transcript(media, delay_s=0.6)
    gid = graph.analyse(sess, [media], gateway=None, wait_transcript_s=20, transcript_poll_s=0.05,
                        set_progress=lambda p, layer=None: seen.append((round(p, 3), layer)))
    assert getattr(gid, "pinned", True) is True
    assert store.current_graph_id(sess) == gid
    g = graph.load_graph(sess, gid)
    assert g["sources"][0]["layers"]["speech"] == "ok"
    assert any(layer == "transcript" for _p, layer in seen), seen


def test_cancel_while_waiting_for_the_transcript_stops_the_analysis(work, clip_bytes):
    from video_ai_editor.api.jobs import JobCancelled
    from video_ai_editor.brain import graph, store
    media = _upload(work, clip_bytes, transcript=None)
    ev, sess = threading.Event(), work / "sess"
    threading.Timer(0.3, ev.set).start()
    t0 = time.monotonic()
    with pytest.raises(JobCancelled):
        graph.analyse(sess, [media], gateway=None, cancel_event=ev, wait_transcript_s=60, transcript_poll_s=0.05)
    assert time.monotonic() - t0 < 5
    assert store.current_graph_id(sess) is None


def test_a_transcript_that_never_comes_pins_a_speechless_graph_the_planner_can_refuse(work, clip_bytes):
    """Long after the upload (whisper is not running, or failed), the graph is built without speech,
    pinned, and says so — the expander's cue to refuse."""
    from video_ai_editor.brain import graph, store
    media = _upload(work, clip_bytes, transcript=None, age_s=3600)
    sess = work / "sess"
    gid = graph.analyse(sess, [media], gateway=None)
    assert getattr(gid, "pinned", True) is True and store.current_graph_id(sess) == gid
    assert graph.blockers(graph.load_graph(sess, gid))[0]["code"] == "no_speech"


def test_a_speechless_graph_is_invalidated_when_the_transcript_lands(work, clip_bytes):
    from video_ai_editor.brain import graph, store
    media = _upload(work, clip_bytes, transcript=None, age_s=3600)
    sess = work / "sess"
    gid = graph.analyse(sess, [media], gateway=None)
    assert store.current_graph_id(sess) == gid
    _land_transcript(media).join()
    assert store.current_graph_id(sess) is None            # what every reader of "the current graph" goes through
    # reading again builds the graph WITH speech and pins it
    gid2 = graph.analyse(sess, [media], gateway=None)
    assert gid2 != gid and store.current_graph_id(sess) == gid2
    assert graph.blockers(graph.load_graph(sess, gid2)) == []


def test_the_pending_window_is_the_one_the_prompt_bar_lives_by():
    from video_ai_editor.agent.prompt.facts import TRANSCRIPT_PENDING_MAX_AGE_S
    from video_ai_editor.brain.analysis import transcripts
    assert transcripts.PENDING_MAX_AGE_S == TRANSCRIPT_PENDING_MAX_AGE_S


def test_transcript_state_reads_what_ingest_reports(work, clip_bytes):
    from video_ai_editor.brain.analysis.transcripts import NONE, PENDING, READY, transcript_state
    assert transcript_state(_upload(work, clip_bytes, transcript="none", name="a")) == NONE            # no upload record
    assert transcript_state(_upload(work, clip_bytes, transcript=None, name="b")) == PENDING            # ingest answered, whisper not yet
    assert transcript_state(_upload(work, clip_bytes, transcript=None, age_s=3600, name="c")) == NONE   # nothing is coming
    assert transcript_state(_upload(work, clip_bytes, transcript=_transcript(), name="d")) == READY
    failed = _upload(work, clip_bytes, transcript=None, name="f")                                       # a writer that knows
    (failed.parent / "ingest.json").write_text(json.dumps({"src": "clip.mp4", "transcript_status": "failed"}))
    assert transcript_state(failed) == NONE
    empty = {"language": "en", "duration": 8.0, "segments": []}                                        # a silent clip, transcribed
    assert transcript_state(_upload(work, clip_bytes, transcript=empty, name="e")) == NONE


def test_a_failed_whisper_pass_writes_the_failed_marker_and_ends_the_wait(work, clip_bytes, monkeypatch):
    """Finalize (FX-A request): main._bg_transcribe used to swallow the exception and write nothing, so the
    analysis waited its full window for a transcript that was never coming."""
    from video_ai_editor import main
    from video_ai_editor.brain.analysis.transcripts import NONE, transcript_state
    from video_ai_editor.ingest import transcribe as T
    media = _upload(work, clip_bytes, transcript=None, name="w")
    assert transcript_state(media) != NONE                       # still pending: ingest answered, whisper not yet

    def boom(*a, **k):
        raise RuntimeError("whisper fell over")
    monkeypatch.setattr(T, "transcribe", boom)
    main._background_transcriber(media, media.parent, "")()      # runs inline; must not raise
    assert json.loads((media.parent / "ingest.json").read_text())["transcript_status"] == "failed"
    assert transcript_state(media) == NONE


def test_an_upload_that_asked_for_no_transcript_is_not_waited_for(work, clip_bytes):
    """Finalize: `transcribe=false` left an ingest.json with no transcript, which the gate and the analysis read as
    "still being transcribed" for ten minutes. The upload now says `skipped`; facts and the analysis believe it."""
    from video_ai_editor.brain.analysis.transcripts import NONE, transcript_state
    media = _upload(work, clip_bytes, transcript=None, name="s")
    ing = media.parent / "ingest.json"
    data = json.loads(ing.read_text())
    data["transcript_status"] = "skipped"
    ing.write_text(json.dumps(data))
    assert transcript_state(media) == NONE


def test_a_graph_with_speech_is_never_invalidated_by_a_later_write(work, clip_bytes):
    from video_ai_editor.brain import graph, store
    media = _upload(work, clip_bytes, transcript=_transcript())
    sess = work / "sess"
    gid = graph.analyse(sess, [media], gateway=None)
    (media.parent / "ingest.json").write_text(json.dumps({"transcript": _transcript(SENTENCES[:2])}))
    assert store.current_graph_id(sess) == gid


def test_the_job_reports_a_pending_transcript_and_pins_nothing(work, clip_bytes, monkeypatch):
    from video_ai_editor.agent.prompt import brain_seams
    from video_ai_editor.brain import store
    media = _upload(work, clip_bytes, transcript=None)
    monkeypatch.setattr(brain_seams, "session_sources", lambda edl: [str(media)])
    monkeypatch.setattr(brain_seams, "TRANSCRIPT_WAIT_S", 0.0)
    st = _fake_store(work, "s_pend")
    job = brain_seams.start_analysis(st)
    _finish(job)
    assert job.status == "completed" and job.result["pinned"] is False and job.result["waiting_for"] == "transcript"
    assert store.current_graph_id(st.dir) is None


def _finish(job, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while job.status in ("queued", "running"):
        assert time.time() < deadline, job.status
        time.sleep(0.02)


def test_the_gate_says_still_reading_and_does_not_plan_from_nothing(work, clip_bytes, monkeypatch):
    """The recipe's analysis gate, answered while the speech is still being read: one clear line,
    the job can be asked again, and no plan is made."""
    import prompt_fixtures as F
    from video_ai_editor.agent.prompt import brain_seams, service
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "1")
    monkeypatch.setattr(service, "_RESOLVE_STORE", None)
    media = _upload(work, clip_bytes, transcript=None)
    monkeypatch.setattr(brain_seams, "session_sources", lambda edl: [str(media)])
    monkeypatch.setattr(brain_seams, "TRANSCRIPT_WAIT_S", 0.0)
    st = _fake_store(work, "s_gatewait")
    monkeypatch.setattr(service, "_resolver_for", lambda store: (lambda sid: st))
    planned: list = []

    async def no_plan(*a, **k):
        planned.append(1)
        yield {"type": "done"}
    monkeypatch.setattr(service, "prompt_turn", no_plan)
    hist: list = []
    events = F.collect(service._resume_gate(st, {"prompt": "make a 45-second reel"}, {"gate_analysis": "read"},
                                            history=hist, ui_state=None, user_message=None))
    said = " ".join(e.get("text", "") + e.get("message", "") for e in events)
    assert "still being read" in said and "Nothing was changed" in said, said
    assert planned == [] and events[-1]["type"] == "done" and not [e for e in events if e["type"] == "error"]


OWN = {"Sec-Fetch-Site": "same-origin"}


def _upload_through_the_app(client, clip_bytes: Path) -> tuple[str, Path]:  # noqa: F811
    """The re-tester's first step: import the clip. The app answers when the video is normalised; the
    transcript is written into ingest.json LATER by the background pass (here: by `_land_transcript`)."""
    sid = client.post("/api/sessions", json={"name": "race"}, headers=OWN).json()["id"]
    with open(clip_bytes, "rb") as fh:
        r = client.post(f"/api/sessions/{sid}/upload", files={"file": ("clip.mp4", fh, "video/mp4")},
                        data={"transcribe": "false", "add_to_timeline": "true"}, headers=OWN)
    assert r.status_code == 200, r.text
    ingest = next((config.WORKDIR / sid / "uploads").rglob("ingest.json"))
    data = json.loads(ingest.read_text(encoding="utf-8"))
    assert not data.get("transcript")
    # `transcribe=false` says "skipped" (nothing is coming); the race is the `transcribe=true` upload BEFORE its
    # whisper pass lands, which has no such marker — so this helper stands in for that upload.
    assert data.pop("transcript_status", None) == "skipped"
    ingest.write_text(json.dumps(data), encoding="utf-8")
    return sid, ingest.parent / "clip.normalized.mp4"


def _job(client, job_id: str, timeout: float = 60.0) -> dict:  # noqa: F811
    deadline = time.time() + timeout
    while True:
        body = client.get(f"/api/jobs/{job_id}").json()
        if body["status"] in ("completed", "failed", "cancelled"):
            return body
        assert time.time() < deadline, body
        time.sleep(0.05)


def test_ux01_end_to_end_prompt_at_once_after_import_reads_the_speech(client, clip_bytes, monkeypatch):  # noqa: F811
    """Upload, then ask for the analysis AT ONCE (the transcript lands a moment later): the graph that is
    pinned has the speech — it used to be pinned without it and never retired."""
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "1")
    monkeypatch.setenv("VAI_BRAIN", "recipes")
    sid, media = _upload_through_the_app(client, clip_bytes)
    _land_transcript(media, delay_s=0.8)
    r = client.post(f"/api/sessions/{sid}/brain/analyse", json={}, headers=OWN)
    assert r.status_code == 202, r.text
    job = _job(client, r.json()["job_id"])
    assert job["status"] == "completed", job
    assert job["result"]["pinned"] is True and job["result"]["waiting_for"] is None
    assert any(e.get("waiting") == "transcript" and e["layer"] == "transcript" for e in job["result"]["events"]), job
    g = client.get(f"/api/sessions/{sid}/brain/graph", headers=OWN).json()
    assert g["layer_status"]["speech"] == "ok" and g["blockers"] == [], g


def test_ux01_end_to_end_a_read_that_cannot_wait_pins_nothing_and_can_be_asked_again(client, clip_bytes,
                                                                                    monkeypatch):  # noqa: F811
    from video_ai_editor.agent.prompt import brain_seams
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "1")
    monkeypatch.setenv("VAI_BRAIN", "recipes")
    monkeypatch.setattr(brain_seams, "TRANSCRIPT_WAIT_S", 0.0)
    sid, media = _upload_through_the_app(client, clip_bytes)
    first = _job(client, client.post(f"/api/sessions/{sid}/brain/analyse", json={}, headers=OWN).json()["job_id"])
    assert first["status"] == "completed" and first["result"]["pinned"] is False
    assert first["result"]["waiting_for"] == "transcript"
    nope = client.get(f"/api/sessions/{sid}/brain/graph", headers=OWN)
    assert nope.status_code == 404 and nope.json()["error"]["code"] == "no_graph"    # nothing to plan from
    _land_transcript(media).join()
    again = _job(client, client.post(f"/api/sessions/{sid}/brain/analyse", json={}, headers=OWN).json()["job_id"])
    assert again["result"]["pinned"] is True
    g = client.get(f"/api/sessions/{sid}/brain/graph", headers=OWN).json()
    assert g["layer_status"]["speech"] == "ok" and g["blockers"] == []


# ---------------------------------------------------------------------------
# UX-07: progress with layer names; cancel at every boundary and in the resume path
# ---------------------------------------------------------------------------

def test_progress_names_the_layer_being_read(work, clip_bytes):
    from video_ai_editor.brain import graph
    media = _upload(work, clip_bytes, transcript=_transcript())
    seen: list = []
    graph.analyse(work / "sess", [media], gateway=None, set_progress=lambda p, layer=None: seen.append((p, layer)))
    layers = [layer for _p, layer in seen]
    for name in ("audio", "speakers", "speech", "semantic"):
        assert name in layers, layers
    assert layers.index("audio") < layers.index("speakers") < layers.index("speech") < layers.index("semantic")
    fr = [p for p, _l in seen]
    assert fr == sorted(fr) and fr[-1] == 1.0


def test_a_one_argument_progress_callback_still_works(work, clip_bytes):
    from video_ai_editor.brain import graph
    media = _upload(work, clip_bytes, transcript=_transcript())
    seen: list = []
    graph.analyse(work / "sess", [media], gateway=None, set_progress=seen.append)
    assert seen and seen[-1] == 1.0


@pytest.mark.parametrize("stop_at", ["sync", "audio", "speakers", "speech", "semantic"])
def test_cancel_is_honoured_at_every_layer_boundary(work, clip_bytes, stop_at):
    from video_ai_editor.api.jobs import JobCancelled
    from video_ai_editor.brain import graph, store
    media = _upload(work, clip_bytes, transcript=_transcript())
    ev, sess = threading.Event(), work / "sess"

    def progress(p, layer=None):
        if layer == stop_at:
            ev.set()
    with pytest.raises(JobCancelled):
        graph.analyse(sess, [media], gateway=None, cancel_event=ev, set_progress=progress)
    assert store.current_graph_id(sess) is None
    assert not list((sess / "brain" / "graph").glob("g_*.json")) if (sess / "brain" / "graph").exists() else True


def test_the_job_carries_layer_names_on_its_events(work, clip_bytes, monkeypatch):
    from video_ai_editor.agent.prompt import brain_seams
    media = _upload(work, clip_bytes, transcript=_transcript())
    monkeypatch.setattr(brain_seams, "session_sources", lambda edl: [str(media)])
    st = _fake_store(work, "s_events")
    job = brain_seams.start_analysis(st)
    _finish(job)
    assert job.status == "completed", job.error
    evs = job.result["events"]
    assert {"audio", "speech"} <= {e["layer"] for e in evs}, evs
    assert all(set(e) >= {"type", "layer", "pct", "eta_s"} and e["type"] == "analysis" for e in evs)
    assert brain_seams.analysis_event("", 10, None) == {"type": "analysis", "layer": "", "pct": 10.0, "eta_s": None}


def test_cancel_analysis_stops_the_running_job(work, monkeypatch):
    from video_ai_editor.agent.prompt import brain_seams
    from video_ai_editor.api.jobs import JobCancelled
    entered = threading.Event()

    def analyse(session_dir, sources, *, set_progress=None, cancel_event=None, **_kw):
        entered.set()
        deadline = time.time() + 20                        # bounded: a red run must not leak a worker
        while not cancel_event.is_set() and time.time() < deadline:
            time.sleep(0.01)
        raise JobCancelled()
    monkeypatch.setattr(brain_seams, "analyse_fn", lambda: analyse)
    st = _fake_store(work, "s_cancel")
    job = brain_seams.start_analysis(st)
    assert entered.wait(10)
    assert brain_seams.cancel_analysis("s_cancel") == job.id
    _finish(job)
    assert job.status == "cancelled"
    assert brain_seams.cancel_analysis("s_cancel") is None            # nothing left to cancel
    assert brain_seams.cancel_analysis("s_other") is None


def test_a_cancelled_analysis_never_pins_even_if_the_analyser_finished(work, monkeypatch):
    from video_ai_editor.agent.prompt import brain_seams
    from video_ai_editor.brain import store
    entered, gate = threading.Event(), threading.Event()

    def analyse(session_dir, sources, *, set_progress=None, cancel_event=None, **_kw):
        entered.set()
        assert gate.wait(10)
        return "g_cccccccccccc"                           # finishes despite the cancel
    monkeypatch.setattr(brain_seams, "analyse_fn", lambda: analyse)
    st = _fake_store(work, "s_late")
    job = brain_seams.start_analysis(st)
    assert entered.wait(10)
    brain_seams.cancel_analysis("s_late")
    gate.set()
    _finish(job)
    assert job.status == "cancelled" and store.current_graph_id(st.dir) is None


def test_cancelling_the_run_while_the_footage_is_read_stops_the_replan(work, monkeypatch):
    """The gate's resume path: cancel during the read → no plan, no preview card, an honest line."""
    import prompt_fixtures as F
    from video_ai_editor.agent.prompt import brain_seams, service
    from video_ai_editor.api.jobs import JobCancelled
    monkeypatch.setattr(service, "_RESOLVE_STORE", None)
    entered = threading.Event()

    def analyse(session_dir, sources, *, set_progress=None, cancel_event=None, **_kw):
        entered.set()
        deadline = time.time() + 20                        # bounded: a red run must not leak a worker
        while not cancel_event.is_set() and time.time() < deadline:
            time.sleep(0.01)
        raise JobCancelled()
    monkeypatch.setattr(brain_seams, "analyse_fn", lambda: analyse)
    st = _fake_store(work, "s_resume_cancel")
    monkeypatch.setattr(service, "_resolver_for", lambda store: (lambda sid: st))
    planned: list = []

    async def no_plan(*a, **k):
        planned.append(1)
        yield {"type": "plan", "plan": {}}
        yield {"type": "done"}
    monkeypatch.setattr(service, "prompt_turn", no_plan)
    threading.Thread(target=lambda: (entered.wait(10), brain_seams.cancel_analysis("s_resume_cancel")),
                     daemon=True).start()
    events = F.collect(service._resume_gate(st, {"prompt": "make a reel"}, {"gate_analysis": "read"}, history=[],
                                            ui_state=None, user_message=None))
    said = " ".join(e.get("text", "") + e.get("message", "") for e in events)
    assert planned == [] and not [e for e in events if e["type"] == "plan"], events
    assert "Cancelled" in said and "Nothing was changed" in said, said
    assert events[-1]["type"] == "done"


def test_the_prompt_cancel_route_cancels_the_analysis_job(work, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from video_ai_editor.agent.prompt import brain_seams, service
    from video_ai_editor.api import prompt_routes
    from video_ai_editor.api.jobs import JobCancelled
    entered = threading.Event()

    def analyse(session_dir, sources, *, set_progress=None, cancel_event=None, **_kw):
        entered.set()
        deadline = time.time() + 20                        # bounded: a red run must not leak a worker
        while not cancel_event.is_set() and time.time() < deadline:
            time.sleep(0.01)
        raise JobCancelled()
    monkeypatch.setattr(brain_seams, "analyse_fn", lambda: analyse)
    st = _fake_store(work, "s_cancel_route")
    job = brain_seams.start_analysis(st)
    assert entered.wait(10)
    monkeypatch.setattr(prompt_routes, "_store", lambda sid: st)
    app = FastAPI()
    app.include_router(prompt_routes.router)
    r = TestClient(app).post(service.route("cancel", "s_cancel_route"))
    assert r.status_code == 200, r.text
    assert r.json()["cancelled"]["analysis"] == job.id
    _finish(job)
    assert job.status == "cancelled"


# ---------------------------------------------------------------------------
# SC-14: paths in failure text
# ---------------------------------------------------------------------------

LEAKY = ("pcm decode failed: /Users/alex/Library/Caches/Video AI Editor/work/s_1/uploads/a b/clip one.mp4: "
         "Invalid data found when processing input (also ~/Movies/x.mov and C:\\Users\\me\\y.wav)")


def test_the_text_scrub_keeps_words_and_drops_directories():
    from video_ai_editor.brain import digest
    out = digest.scrub_paths_in_text(LEAKY)
    for bad in ("/Users", "Library", "Caches", "~/", "C:\\", "/work"):
        assert bad not in out, out
    assert "clip one.mp4" in out and "Invalid data found" in out and "pcm decode failed" in out
    assert digest.scrub_paths_in_text("and/or 24/7 a \"quote\"") == "and/or 24/7 a \"quote\""


def test_a_failed_job_record_carries_no_absolute_path(work, monkeypatch):
    from video_ai_editor.agent.prompt import brain_seams

    def broken(session_dir, sources, **_kw):
        raise RuntimeError(LEAKY)
    monkeypatch.setattr(brain_seams, "analyse_fn", lambda: broken)
    job = brain_seams.start_analysis(_fake_store(work, "s_fail"))
    _finish(job)
    assert job.status == "failed" and "/Users" not in job.error and "Library" not in job.error, job.error
    assert "clip one.mp4" in job.error


def test_the_gate_reply_for_a_failed_read_carries_no_absolute_path(work, monkeypatch):
    import prompt_fixtures as F
    from video_ai_editor.agent.prompt import brain_seams, service
    monkeypatch.setattr(service, "_RESOLVE_STORE", None)

    def broken(session_dir, sources, **_kw):
        raise RuntimeError(LEAKY)
    monkeypatch.setattr(brain_seams, "analyse_fn", lambda: broken)
    st = _fake_store(work, "s_gatefail")
    monkeypatch.setattr(service, "_resolver_for", lambda store: (lambda sid: st))
    hist: list = []
    events = F.collect(service._resume_gate(st, {"prompt": "make a reel"}, {"gate_analysis": "read"}, history=hist,
                                            ui_state=None, user_message=None))
    blob = json.dumps([events, hist])
    assert "could not be read" in blob and "/Users" not in blob and "Library/Caches" not in blob, blob


def test_the_first_eta_comes_from_the_media_length_then_from_the_pace_of_the_read():
    from video_ai_editor.agent.prompt import brain_seams as B
    assert B.estimate_read_s(0) == B.READ_FIXED_S and B.estimate_read_s(70) < 10
    assert B.estimate_read_s(3600) > 60 > B.estimate_read_s(600)              # an hour of footage is minutes
    assert B._eta(0.05, 1.0, 10.0) == pytest.approx(9.5) and B._eta(0.05, 1.0, None) is None
    assert B._eta(0.5, 4.0, 10.0) == pytest.approx(4.0)                        # past 20 %: elapsed * (1 - p) / p
