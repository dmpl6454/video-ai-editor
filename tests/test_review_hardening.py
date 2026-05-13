"""Tests for the production-hardening additions made during /review:

  1. _STORES LRU cap — old sessions evict, recent stay warm.
  2. Path-restriction guard — VAI_RESTRICT_PATHS=1 rejects paths outside roots.
  3. Background job manager — submit, poll, and complete a render-style job.
  4. Async /preview + /export — wait=0 returns 202 + job_id.
"""
from __future__ import annotations
import os
import time
from pathlib import Path
import importlib

import pytest


# ---------- 1. LRU cap on _STORES ----------

def test_stores_cache_evicts_lru_when_full(monkeypatch, tmp_path: Path):
    # Force a tiny cap so we exercise eviction with 3 sessions.
    monkeypatch.setenv("VAI_STORES_CACHE_MAX", "2")
    from video_ai_editor import storage as _storage
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path)

    # Re-import main so the cap env-var is read at module load.
    from video_ai_editor import main as _main
    importlib.reload(_main)
    monkeypatch.setattr(_main, "WORKDIR", tmp_path)
    _main._STORES.clear()

    from fastapi.testclient import TestClient
    client = TestClient(_main.app)
    sids = []
    for _ in range(3):
        sids.append(client.post("/api/sessions").json()["id"])
    # GET each to populate the cache via _store()
    for sid in sids:
        client.get(f"/api/sessions/{sid}")
    # The cap is 2 → first session created should have been evicted from cache.
    assert len(_main._STORES) <= 2
    # Re-fetching the evicted one re-populates it (re-loads from disk).
    client.get(f"/api/sessions/{sids[0]}")
    assert sids[0] in _main._STORES


# ---------- 2. Path-restriction allowlist ----------

def test_assert_path_allowed_off_by_default(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("VAI_RESTRICT_PATHS", raising=False)
    from video_ai_editor import config
    importlib.reload(config)
    # Off → any path resolves without raising.
    assert config.assert_path_allowed("/etc/hosts") == Path("/etc/hosts").resolve()


def test_assert_path_allowed_blocks_outside_roots(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("VAI_RESTRICT_PATHS", "1")
    monkeypatch.setenv("VAI_ALLOWED_ROOTS", str(tmp_path))
    from video_ai_editor import config
    importlib.reload(config)
    # Inside the allowed root → allowed.
    inside = tmp_path / "ok.mp4"
    inside.write_bytes(b"x")
    assert config.assert_path_allowed(inside) == inside.resolve()
    # Outside → ValueError.
    with pytest.raises(ValueError, match="outside the allowed roots"):
        config.assert_path_allowed("/etc/hosts")
    # Cleanup so other tests don't see RESTRICT_PATHS=1.
    monkeypatch.delenv("VAI_RESTRICT_PATHS")
    importlib.reload(config)


# ---------- 3. Job manager ----------

def test_job_manager_runs_and_completes_a_job():
    from video_ai_editor.api.jobs import JobManager
    mgr = JobManager(workers=1, retain_completed=10)

    def _work() -> dict:
        return {"answer": 42}

    job = mgr.submit(kind="test", fn=_work)
    # Wait up to 2s for the worker to finish.
    deadline = time.time() + 2
    while time.time() < deadline and job.status not in ("completed", "failed"):
        time.sleep(0.02)
    assert job.status == "completed"
    assert job.result == {"answer": 42}
    assert job.error is None
    mgr.shutdown(wait=True)


def test_job_manager_captures_failure():
    from video_ai_editor.api.jobs import JobManager
    mgr = JobManager(workers=1)

    def _bad() -> dict:
        raise RuntimeError("kaboom")

    job = mgr.submit(kind="test", fn=_bad)
    deadline = time.time() + 2
    while time.time() < deadline and job.status not in ("completed", "failed"):
        time.sleep(0.02)
    assert job.status == "failed"
    assert "kaboom" in (job.error or "")
    assert job.result is None
    mgr.shutdown(wait=True)


# ---------- 4. Async /preview returns 202 + job_id ----------

def test_preview_endpoint_async_returns_202(monkeypatch, tmp_path: Path):
    from video_ai_editor import storage as _storage
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path)
    from video_ai_editor import main as _main
    importlib.reload(_main)
    monkeypatch.setattr(_main, "WORKDIR", tmp_path)
    _main._STORES.clear()

    from fastapi.testclient import TestClient
    client = TestClient(_main.app)
    sid = client.post("/api/sessions").json()["id"]
    r = client.post(f"/api/sessions/{sid}/preview?wait=0")
    assert r.status_code == 202
    body = r.json()
    assert "job_id" in body
    assert body["status"] in ("queued", "running", "completed")
    assert body["status_url"] == f"/api/jobs/{body['job_id']}"
    # Job is reachable via the polling endpoint.
    j = client.get(body["status_url"]).json()
    assert j["id"] == body["job_id"]
    assert j["kind"] == "preview"


def test_get_job_returns_404_for_unknown_id(monkeypatch, tmp_path: Path):
    from video_ai_editor import storage as _storage
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path)
    from video_ai_editor import main as _main
    importlib.reload(_main)

    from fastapi.testclient import TestClient
    client = TestClient(_main.app)
    r = client.get("/api/jobs/does-not-exist")
    assert r.status_code == 404
