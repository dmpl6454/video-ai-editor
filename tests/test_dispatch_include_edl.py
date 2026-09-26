"""`POST /dispatch?include=edl` (wave D, INSTANT_PREVIEW_SPEC §4.1, §5.2,
Appendix B): the answer carries the post-op EDL, its hash and the render hash,
all taken under the session lock that serialised the edit."""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from video_ai_editor.api import hardening
from video_ai_editor.edl.schema import EDL
from video_ai_editor.main import app


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    from video_ai_editor import main as _main, storage as _storage
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path)
    monkeypatch.setattr(_main, "WORKDIR", tmp_path)
    _main._STORES.clear()
    hardening.RATE.windows.clear()
    return TestClient(app)


def _sid(client) -> str:
    return client.post("/api/sessions").json()["id"]


def _text(client, sid, text="hi", start=0.0, end=1.0, include=True, **extra):
    params = {"include": "edl"} if include else {}
    return client.post(f"/api/sessions/{sid}/dispatch", params=params,
                       json={"tool": "add_text",
                             "args": {"text": text, "start": start, "end": end}, **extra})


def _render_hash(sid: str) -> str:
    from video_ai_editor import main as _main
    return _main._preview_edl(_main._store(sid)).render_hash()


def test_default_answer_is_unchanged(client):
    sid = _sid(client)
    body = _text(client, sid, include=False).json()
    assert set(body) == {"result", "edl_hash", "op", "undo_depth"}


def test_include_edl_returns_the_stored_edl_and_hashes(client):
    sid = _sid(client)
    r = _text(client, sid)
    assert r.status_code == 200, r.text
    body = r.json()
    stored = client.get(f"/api/sessions/{sid}/edl").json()
    assert body["edl"] == stored
    assert body["edl_hash"] == EDL.model_validate(body["edl"]).hash()
    assert body["edl_hash"] == client.get(f"/api/sessions/{sid}/head").json()["edl_hash"]
    assert body["render_hash"] == _render_hash(sid)
    assert body["op"]["edl_hash_after"] == body["edl_hash"]


def test_render_hash_ignores_what_no_render_reads(client):
    """A marker changes the EDL (edl_hash) but not the render (QA-131), so
    the engine keeps its frames and the preview key."""
    sid = _sid(client)
    first = _text(client, sid).json()
    r = client.post(f"/api/sessions/{sid}/dispatch", params={"include": "edl"},
                    json={"tool": "add_marker", "args": {"time": 0.5, "label": "m"}})
    body = r.json()
    assert body["edl_hash"] != first["edl_hash"]
    assert body["render_hash"] == first["render_hash"]
    assert len(body["edl"]["markers"]) == 1


def test_include_edl_on_undo_and_redo(client):
    sid = _sid(client)
    a = _text(client, sid, "one").json()
    b = _text(client, sid, "two", start=1.0, end=2.0).json()
    u = client.post(f"/api/sessions/{sid}/dispatch", params={"include": "edl"},
                    json={"tool": "undo", "args": {}}).json()
    assert u["edl_hash"] == a["edl_hash"] and u["edl"] == a["edl"]
    assert u["render_hash"] == a["render_hash"]
    r = client.post(f"/api/sessions/{sid}/dispatch", params={"include": "edl"},
                    json={"tool": "redo", "args": {}}).json()
    assert r["edl_hash"] == b["edl_hash"] and r["edl"] == b["edl"]


def test_include_edl_through_an_async_job(client):
    sid = _sid(client)
    r = client.post(f"/api/sessions/{sid}/dispatch", params={"include": "edl", "wait": 0},
                    json={"tool": "add_text", "args": {"text": "j", "start": 0, "end": 1}})
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    import time
    for _ in range(200):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("completed", "failed", "cancelled"):
            break
        time.sleep(0.02)
    assert job["status"] == "completed", job
    result = job["result"]
    assert result["edl"] == client.get(f"/api/sessions/{sid}/edl").json()
    assert result["render_hash"] == _render_hash(sid)


def test_concurrent_edits_each_answer_their_own_snapshot(client):
    """Eight edits race; each answer's EDL is exactly the state its own edit
    produced (its hash matches its own EDL, all hashes differ, and the last
    one is what is stored)."""
    sid = _sid(client)
    answers: list[dict] = []
    lock = threading.Lock()

    def edit(i: int) -> None:
        body = _text(client, sid, f"t{i}", start=float(i), end=float(i) + 1).json()
        with lock:
            answers.append(body)

    threads = [threading.Thread(target=edit, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(answers) == 8
    for a in answers:
        assert EDL.model_validate(a["edl"]).hash() == a["edl_hash"]
    counts = sorted(sum(len(t["clips"]) for t in a["edl"]["tracks"] if t["type"] == "text")
                    for a in answers)
    assert counts == list(range(1, 9))
    assert len({a["edl_hash"] for a in answers}) == 8
    final = client.get(f"/api/sessions/{sid}/edl").json()
    assert any(a["edl"] == final for a in answers)


def test_huge_edl_is_omitted_not_inlined(client, monkeypatch):
    from video_ai_editor import main as _main
    monkeypatch.setattr(_main, "DISPATCH_EDL_MAX_BYTES", 200)
    sid = _sid(client)
    body = _text(client, sid).json()
    assert body.get("edl_omitted") is True and "edl" not in body
    assert body["render_hash"] == _render_hash(sid)


def test_edl_payload_size_is_measured(client):
    """Spec §14 risk 12: record what a big timeline costs per edit."""
    sid = _sid(client)
    for i in range(150):
        client.post(f"/api/sessions/{sid}/dispatch",
                    json={"tool": "add_text", "args": {"text": f"c{i}", "start": i, "end": i + 1}})
    hardening.RATE.windows.clear()
    r = _text(client, sid, "last", start=200, end=201)
    size = len(json.dumps(r.json()["edl"]))
    print(f"include=edl payload with 151 text clips: {size / 1024:.0f} KiB")
    assert size < 1_000_000
