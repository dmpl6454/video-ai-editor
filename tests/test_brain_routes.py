"""Editor Brain surfaces (EB1-F): the brain routes and the `brain.enabled` flag.

`api/brain_routes.py` over the real FastAPI app against a temp WORKDIR:

  * `POST …/brain/analyse` → 202 `{job_id}` over the jobs machinery, calling
    D's `brain.graph.analyse(session_dir, sources)` (monkeypatched here with
    the frozen signature); progress is visible at `GET /api/jobs/{id}` and
    the finished job pins the session's current graph;
  * `GET …/brain/graph` is a summary with LEAF names only — never an
    absolute path, whatever the graph file holds;
  * every write route is picked up by `tests/test_same_origin_writes.py`'s
    enumeration and refused for a foreign page by the shared guard;
  * `POST …/brain/versions/{id}/restore` is ONE op in History (the snapshot
    tree committed as `restore_version`) through C's versions helper (the
    lane's built-in stand-in until it lands);
  * `brain.enabled` (brain_setting.py): default OFF, `VAI_BRAIN_ENABLED`
    overrides, `PUT /api/settings/brain` stores it; with the flag off every
    session brain route answers 404 `brain_disabled`.
"""
from __future__ import annotations

import json
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_load_project_by_content import client, workdir  # noqa: E402,F401  (fixtures)

from video_ai_editor import main as _main  # noqa: E402

OWN = {"Sec-Fetch-Site": "same-origin"}
HOSTILE = {"Sec-Fetch-Site": "same-site"}


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "1")


def _session(client) -> str:  # noqa: F811
    r = client.post("/api/sessions", json={"name": "brain routes"}, headers=OWN)
    assert r.status_code == 200, r.text
    return r.json()["id"]


GID = "g_abc123def456"


def _fake_graph_module(monkeypatch, tmp: Path, *, gid: str = GID) -> dict:
    """D's `brain.graph.analyse` in the frozen signature, recording its calls
    (stubbed at the seam the route and the analysis gate both start it through)."""
    from video_ai_editor.agent.prompt import brain_seams
    seen: dict = {}

    def analyse(session_dir, sources, *, set_progress=None, cancel_event=None, **kw):
        seen["session_dir"] = Path(session_dir)
        seen["sources"] = list(sources)
        seen["kw"] = kw
        if set_progress:
            set_progress(0.5)
        gdir = Path(session_dir) / "brain" / "graph"
        gdir.mkdir(parents=True, exist_ok=True)
        (gdir / f"{gid}.json").write_text(json.dumps({
            "id": gid, "digest": "d" * 12,
            "layers": {"speech": {"status": "ok"}, "audio": {"status": "ok"}},
            "sources": [{"src_key": "k1", "path": str(tmp / "uploads" / "talk.mp4")}],
        }), encoding="utf-8")
        return gid

    monkeypatch.setattr(brain_seams, "analyse_fn", lambda: analyse)
    monkeypatch.setattr(brain_seams, "_ANALYSIS_JOBS", {})
    return seen


def _wait_job(client, job_id: str, timeout: float = 20.0) -> dict:  # noqa: F811
    deadline = time.time() + timeout
    while True:
        body = client.get(f"/api/jobs/{job_id}").json()
        if body["status"] in ("completed", "failed", "cancelled"):
            return body
        assert time.time() < deadline, body
        time.sleep(0.05)


# --- analyse -----------------------------------------------------------------

def test_analyse_returns_202_and_job_progress(client, workdir, monkeypatch, flag_on):  # noqa: F811
    seen = _fake_graph_module(monkeypatch, workdir)
    sid = _session(client)
    r = client.post(f"/api/sessions/{sid}/brain/analyse", json={"force": True}, headers=OWN)
    assert r.status_code == 202, r.text
    body = r.json()
    assert set(body) >= {"job_id", "status", "status_url"} and body["status_url"] == f"/api/jobs/{body['job_id']}"
    job = _wait_job(client, body["job_id"])
    assert job["status"] == "completed", job
    assert job["progress"] == 1.0 and job["result"]["graph_id"] == GID
    assert job["kind"] == "brain_analyse" and job["session_id"] == sid
    assert seen["session_dir"] == workdir / sid
    # the finished job pins the session's current graph for the reads
    current = json.loads((workdir / sid / "brain" / "graph" / "current.json").read_text())
    assert current["id"] == GID
    g = client.get(f"/api/sessions/{sid}/brain/graph", headers=OWN)
    assert g.status_code == 200 and g.json()["id"] == GID
    assert g.json()["analysis"]["status"] == "completed"


def test_analyse_without_the_analyser_says_so(client, workdir, monkeypatch, flag_on):  # noqa: F811
    from video_ai_editor.agent.prompt import brain_seams
    monkeypatch.setattr(brain_seams, "analyse_fn", lambda: None)
    sid = _session(client)
    r = client.post(f"/api/sessions/{sid}/brain/analyse", json={}, headers=OWN)
    assert r.status_code == 501, r.text
    assert "analys" in r.text.lower()


def test_analyse_is_hidden_with_the_flag_off(client, workdir, monkeypatch):  # noqa: F811
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "0")
    _fake_graph_module(monkeypatch, workdir)
    sid = _session(client)
    r = client.post(f"/api/sessions/{sid}/brain/analyse", json={}, headers=OWN)
    assert r.status_code == 404 and r.json()["error"]["code"] == "brain_disabled", r.text
    assert not (workdir / sid / "brain").exists()


def test_analyse_unknown_session_is_404(client, workdir, monkeypatch, flag_on):  # noqa: F811
    _fake_graph_module(monkeypatch, workdir)
    r = client.post("/api/sessions/s_00000000aa/brain/analyse", json={}, headers=OWN)
    assert r.status_code == 404


# --- graph summary ------------------------------------------------------------

def test_graph_summary_has_no_absolute_paths(client, workdir, flag_on):  # noqa: F811
    sid = _session(client)
    gdir = workdir / sid / "brain" / "graph"
    gdir.mkdir(parents=True)
    leak = str(workdir / "uploads" / "cam_a.mp4")
    (gdir / "g_feedbeef0001.json").write_text(json.dumps({
        "id": "g_feedbeef0001", "digest": "0" * 12,
        "layers": {"speech": {"status": "ok", "params": {"model": "small"}}, "speakers": {"status": "ok"},
                   "audio": {"status": "partial", "path": leak}},
        "speakers": {"k": 2, "speakers": [{"id": "S1", "role_guess": "host"}, {"id": "S2", "role_guess": "guest"}]},
        "angles": {"reference": leak, "dialogue": str(workdir / "uploads" / "recorder.wav"),
                   "members": [{"angle": "A", "src_key": "k1", "path": leak, "sync_offset_s": 0.35,
                                "confidence": 0.9, "sees": ["S1"], "by": "own_mic"}]},
        "sources": [{"src_key": "k1", "path": leak, "realpath": "/private" + leak}],
        "scenes": [{"id": "sc_0001", "src": leak, "t0": 0.0, "t1": 4.0}],
        "timings": {"speech_s": 1.2, "audio_s": 0.4},
    }), encoding="utf-8")
    (gdir / "current.json").write_text(json.dumps({"id": "g_feedbeef0001"}), encoding="utf-8")
    r = client.get(f"/api/sessions/{sid}/brain/graph", headers=OWN)
    assert r.status_code == 200, r.text
    text = r.text
    for forbidden in (str(workdir), "/Users/", "/private/", "/tmp/", "/var/"):
        assert forbidden not in text, (forbidden, text[:400])
    body = r.json()
    assert body["id"] == "g_feedbeef0001"
    assert sorted(body["layers"]) == ["audio", "speakers", "speech"]
    assert body["angles"]["members"][0]["path"] == "cam_a.mp4"
    assert body["angles"]["dialogue"] == "recorder.wav"
    assert "cam_a.mp4" in text


def test_graph_summary_without_a_graph_is_404(client, workdir, flag_on):  # noqa: F811
    sid = _session(client)
    r = client.get(f"/api/sessions/{sid}/brain/graph", headers=OWN)
    assert r.status_code == 404 and r.json()["error"]["code"] == "no_graph"


# --- decisions -----------------------------------------------------------------

def test_decisions_route_returns_the_edp_with_leaf_names(client, workdir, flag_on):  # noqa: F811
    sid = _session(client)
    ddir = workdir / sid / "brain" / "decisions"
    ddir.mkdir(parents=True)
    leak = str(workdir / "uploads" / "talk.mp4")
    (ddir / "d_0a1b2c3d.json").write_text(json.dumps({
        "version": 1, "id": "d_0a1b2c3d", "summary": {"project_type": "talking_head", "target": "Reel",
                                                      "hook": {"sent": "s_003", "src": leak, "t0": 12.1,
                                                               "t1": 15.0, "quote": "the single biggest number"}},
        "decisions": [{"id": "k_0001", "kind": "cut_range", "ref": {"src": leak, "t0": 1.0, "t1": 1.8},
                       "reason": {"code": "silence", "facts": ["sil_0001"], "text": "silence of 0.8 s at 00:00:01:00"}}],
    }), encoding="utf-8")
    r = client.get(f"/api/sessions/{sid}/brain/decisions/d_0a1b2c3d", headers=OWN)
    assert r.status_code == 200, r.text
    assert str(workdir) not in r.text and r.json()["decisions"][0]["ref"]["src"] == "talk.mp4"
    assert client.get(f"/api/sessions/{sid}/brain/decisions/d_ffffffff", headers=OWN).status_code == 404
    assert client.get(f"/api/sessions/{sid}/brain/decisions/..%2Fedl", headers=OWN).status_code in (404, 422)


# --- same origin -----------------------------------------------------------------

def test_same_origin_enforced_on_writes(client, workdir, flag_on):  # noqa: F811
    """The write routes are picked up by the enumeration in
    tests/test_same_origin_writes.py (nobody has to remember them), and the
    shared guard refuses a page on another local port."""
    import test_same_origin_writes as SO
    paths = {(m, p) for m, p in SO.WRITE_ROUTES}
    sid = SO._FILL["sid"]
    mine = {("POST", f"/api/sessions/{sid}/brain/analyse"),
            ("POST", f"/api/sessions/{sid}/brain/versions/x/restore"),
            ("PUT", "/api/settings/brain")}
    assert mine <= paths, sorted(paths - mine)[:5]
    for method, path in sorted(mine):
        r = client.request(method, path, headers=HOSTILE)
        assert SO._refused_by_guard(r), (method, path, r.status_code, r.text[:200])
        r = client.request(method, path, headers=OWN)
        assert not SO._refused_by_guard(r), (method, path, r.status_code)


# --- versions ------------------------------------------------------------------

def _ops(client, sid: str) -> list[dict]:  # noqa: F811
    return client.get(f"/api/sessions/{sid}", headers=OWN).json()["ops"]


def test_restore_is_one_op_in_history(client, workdir, flag_on):  # noqa: F811
    from video_ai_editor.agent.prompt import brain_seams
    sid = _session(client)
    dispatch = f"/api/sessions/{sid}/dispatch"
    assert client.post(dispatch, json={"tool": "add_text", "args": {"text": "one", "start": 0, "end": 1}},
                       headers=OWN).status_code == 200
    store = _main._store(sid)
    hash_v1 = store.edl.hash()
    row = brain_seams.versions().record(store, label="V1 Reel", decisions_id="d_0a1b2c3d", kind="brain")
    assert row["id"] and row["label"] == "V1 Reel" and row["edl_hash"] == hash_v1 and row["pinned"] is True
    assert client.post(dispatch, json={"tool": "add_text", "args": {"text": "two", "start": 1, "end": 2}},
                       headers=OWN).status_code == 200
    assert store.edl.hash() != hash_v1
    ops_before = _ops(client, sid)
    depth_before = store.undo_depth

    listed = client.get(f"/api/sessions/{sid}/brain/versions", headers=OWN)
    assert listed.status_code == 200, listed.text
    rows = listed.json()["versions"]
    assert [v["label"] for v in rows] == ["V1 Reel"] and rows[0]["current"] is False and rows[0]["restorable"] is True

    r = client.post(f"/api/sessions/{sid}/brain/versions/{row['id']}/restore", headers=OWN)
    assert r.status_code == 200, r.text
    ops_after = _ops(client, sid)
    assert len(ops_after) == len(ops_before) + 1, [o["tool"] for o in ops_after]
    assert ops_after[-1]["tool"] == "restore_version" and ops_after[-1]["edl_hash_after"] == hash_v1
    assert store.edl.hash() == hash_v1 and store.undo_depth == depth_before + 1
    assert r.json()["op"]["tool"] == "restore_version"
    rows = client.get(f"/api/sessions/{sid}/brain/versions", headers=OWN).json()["versions"]
    assert rows[0]["current"] is True
    # unknown id → 404; restore of the current tree changes nothing (200, op null)
    assert client.post(f"/api/sessions/{sid}/brain/versions/v_999/restore", headers=OWN).status_code == 404
    again = client.post(f"/api/sessions/{sid}/brain/versions/{row['id']}/restore", headers=OWN)
    assert again.status_code == 200 and again.json()["op"] is None
    assert len(_ops(client, sid)) == len(ops_after)


def test_versions_hidden_with_the_flag_off(client, workdir, monkeypatch):  # noqa: F811
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "0")
    sid = _session(client)
    r = client.get(f"/api/sessions/{sid}/brain/versions", headers=OWN)
    assert r.status_code == 404 and r.json()["error"]["code"] == "brain_disabled"


# --- the setting -------------------------------------------------------------------

def test_brain_setting_defaults_off_env_overrides_and_put_stores(client, workdir, monkeypatch):  # noqa: F811
    from video_ai_editor import brain_setting
    monkeypatch.delenv("VAI_BRAIN_ENABLED", raising=False)
    assert brain_setting.DEFAULT_ENABLED is False
    r = client.get("/api/settings/brain", headers=OWN)
    assert r.status_code == 200 and r.json() == {"enabled": False, "source": "default", "default": False}
    assert r.headers.get("cache-control") == "no-store"
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "1")
    assert client.get("/api/settings/brain", headers=OWN).json() == {"enabled": True, "source": "env", "default": False}
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "0")
    assert client.get("/api/settings/brain", headers=OWN).json()["enabled"] is False
    monkeypatch.delenv("VAI_BRAIN_ENABLED")
    r = client.put("/api/settings/brain", json={"enabled": True}, headers=OWN)
    assert r.status_code == 200 and r.json() == {"enabled": True, "source": "settings", "default": False}
    from video_ai_editor.api import pairing
    assert pairing.load_settings()["brain"] == {"enabled": True}
    assert brain_setting.enabled() == (True, "settings")
    r = client.put("/api/settings/brain", json={"enabled": False}, headers=OWN)
    assert r.json()["enabled"] is False
    # shape: exactly one boolean field, JSON only
    assert client.put("/api/settings/brain", json={"enabled": "yes"}, headers=OWN).status_code == 400
    assert client.put("/api/settings/brain", json={"on": True}, headers=OWN).status_code == 400
    assert client.put("/api/settings/brain", content="enabled=true",
                      headers={**OWN, "content-type": "text/plain"}).status_code == 415


# --- FX-A (SC-13): the graph route's cap and the store's current-graph rule --------------------------

def _seed_graph(workdir: Path, sid: str, *, current: bool, pad: int = 0) -> Path:  # noqa: F811
    gdir = workdir / sid / "brain" / "graph"
    gdir.mkdir(parents=True)
    (gdir / "g_feedbeef0001.json").write_text(json.dumps({"id": "g_feedbeef0001", "layers": {}, "pad": "x" * pad}),
                                              encoding="utf-8")
    if current:
        (gdir / "current.json").write_text(json.dumps({"id": "g_feedbeef0001"}), encoding="utf-8")
    return gdir


def test_the_graph_route_has_no_newest_file_fallback(client, workdir, flag_on):  # noqa: F811
    sid = _session(client)
    gdir = _seed_graph(workdir, sid, current=False)
    r = client.get(f"/api/sessions/{sid}/brain/graph", headers=OWN)
    assert r.status_code == 404 and r.json()["error"]["code"] == "no_graph", r.text     # the validator sees no graph either
    (gdir / "current.json").write_text(json.dumps({"id": "g_feedbeef0001"}), encoding="utf-8")
    assert client.get(f"/api/sessions/{sid}/brain/graph", headers=OWN).status_code == 200
    (gdir / "current.json").write_text(json.dumps({"id": "g_0000000000ff"}), encoding="utf-8")   # names a graph that is not there
    assert client.get(f"/api/sessions/{sid}/brain/graph", headers=OWN).status_code == 404


def test_the_graph_route_refuses_a_file_over_the_cap(client, workdir, flag_on, monkeypatch):  # noqa: F811
    from video_ai_editor.brain import schema as S
    sid = _session(client)
    _seed_graph(workdir, sid, current=True, pad=5000)
    assert client.get(f"/api/sessions/{sid}/brain/graph", headers=OWN).status_code == 200
    monkeypatch.setattr(S, "MAX_GRAPH_BYTES", 1000)
    r = client.get(f"/api/sessions/{sid}/brain/graph", headers=OWN)
    assert r.status_code == 413 and r.json()["error"]["code"] == "graph_too_large", r.text


def test_the_decisions_route_refuses_a_file_over_the_cap(client, workdir, flag_on, monkeypatch):  # noqa: F811
    from video_ai_editor.brain import schema as S
    sid = _session(client)
    ddir = workdir / sid / "brain" / "decisions"
    ddir.mkdir(parents=True)
    (ddir / "d_0a1b2c3d.json").write_text(json.dumps({"id": "d_0a1b2c3d", "pad": "x" * 5000}), encoding="utf-8")
    assert client.get(f"/api/sessions/{sid}/brain/decisions/d_0a1b2c3d", headers=OWN).status_code == 200
    monkeypatch.setattr(S, "MAX_GRAPH_BYTES", 1000)
    r = client.get(f"/api/sessions/{sid}/brain/decisions/d_0a1b2c3d", headers=OWN)
    assert r.status_code == 413, r.text


def test_unknown_and_pruned_versions_answer_404_and_409(client, workdir, flag_on):  # noqa: F811
    from video_ai_editor.agent.prompt import brain_seams
    from video_ai_editor.brain import versions as V
    sid = _session(client)
    dispatch = f"/api/sessions/{sid}/dispatch"
    assert client.post(dispatch, json={"tool": "add_text", "args": {"text": "one", "start": 0, "end": 1}},
                       headers=OWN).status_code == 200
    store = _main._store(sid)
    row = brain_seams.versions().record(store, label="V1 Edit", decisions_id=None, kind="manual")
    assert client.post(dispatch, json={"tool": "add_text", "args": {"text": "two", "start": 1, "end": 2}},
                       headers=OWN).status_code == 200
    V.snapshot_file(store.dir, next(v for v in V.list_versions(store.dir) if v.id == row["id"])).unlink()
    gone = client.post(f"/api/sessions/{sid}/brain/versions/{row['id']}/restore", headers=OWN)
    assert gone.status_code == 409 and gone.json()["error"]["code"] == "no_longer_restorable", gone.text
    unknown = client.post(f"/api/sessions/{sid}/brain/versions/v_404/restore", headers=OWN)
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "unknown_version", unknown.text



def test_the_versions_route_answers_200_on_a_garbage_file(client, workdir, flag_on):  # noqa: F811
    """Closer review: rows that are not objects, or lack fields, raised AttributeError/TypeError -> 500."""
    import json as _json
    sid = _session(client)
    store = _main._store(sid)
    (Path(store.dir) / "brain").mkdir(exist_ok=True)
    (Path(store.dir) / "brain" / "versions.json").write_text(
        _json.dumps({"versions": [1, "s", None, {"id": "v_1"}, {"op_seq": None, "edl_hash": "abc", "pinned": True}]}))
    r = client.get(f"/api/sessions/{sid}/brain/versions", headers=OWN)
    assert r.status_code == 200 and r.json()["versions"] == []
