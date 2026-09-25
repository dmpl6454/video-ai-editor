"""UPLOAD-QUEUES-BEHIND-EXPORT.

QA-007 made every UI import an async `upload` job on the shared 2-worker
JOB_MANAGER, so an import waited behind two running exports; and cancelling
those exports did not free the workers, because the chunk stage of an export
ran its ffmpegs under render.cancel.run, which honoured only the preview
supersede scope — not the job's cancel_event.

1. JobManager runs `upload` jobs on their own pool (unit level, real threads).
2. Through the HTTP API with real ffmpeg: while two long exports run, an
   upload?wait=0 completes; POST /jobs/{id}/cancel then ends both exports
   promptly (their chunk ffmpegs are terminated).
"""
from __future__ import annotations

import importlib
import subprocess
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from video_ai_editor.api.jobs import JobManager


def test_upload_jobs_do_not_queue_behind_render_jobs():
    jm = JobManager(workers=2, lane_workers={"ingest": 1})
    release = threading.Event()
    try:
        exports = [jm.submit(kind="export", fn=lambda: (release.wait(10), {})[1])
                   for _ in range(2)]
        deadline = time.monotonic() + 2
        while any(j.status != "running" for j in exports) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert [j.status for j in exports] == ["running", "running"]
        up = jm.submit(kind="upload", fn=lambda: {"ok": True})
        deadline = time.monotonic() + 2
        while up.status != "completed" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert up.status == "completed", up.status
        assert jm.get(up.id) is up and up in jm.list()
    finally:
        release.set()
        jm.shutdown(wait=True)


# ------------------------------------------------------------------ live API

@pytest.fixture()
def client(monkeypatch, tmp_path: Path):
    from video_ai_editor import storage as _storage
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path / "wd")
    from video_ai_editor import main as _main
    importlib.reload(_main)
    monkeypatch.setattr(_main, "WORKDIR", tmp_path / "wd")
    _main._STORES.clear()
    return TestClient(_main.app)


def _lavfi(dst: Path, seconds: int, size: str) -> Path:
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    f"testsrc2=s={size}:r=30:d={seconds}", "-f", "lavfi", "-i",
                    f"sine=f=440:d={seconds}", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(dst)],
                   check=True, capture_output=True)
    return dst


def _poll(client, job_id, want, timeout):
    deadline = time.monotonic() + timeout
    job = client.get(f"/api/jobs/{job_id}").json()
    while job["status"] not in want and time.monotonic() < deadline:
        time.sleep(0.1)
        job = client.get(f"/api/jobs/{job_id}").json()
    return job


def test_import_completes_and_cancel_frees_exports(client, tmp_path):
    long_src = _lavfi(tmp_path / "long.mp4", 240, "1920x1080")
    small = _lavfi(tmp_path / "small.mp4", 2, "320x180")
    sid = client.post("/api/sessions").json()["id"]
    r = client.post(f"/api/sessions/{sid}/dispatch", json={"tool": "add_clip", "args": {
        "track": "v1", "src": str(long_src), "in": 0, "out": 240, "start": 0}})
    assert r.status_code == 200, r.text
    ids = []
    for h in (1080, 720):
        r = client.post(f"/api/sessions/{sid}/export?wait=0", json={"height": h})
        assert r.status_code == 202, r.text
        ids.append(r.json()["job_id"])
    time.sleep(1.5)
    assert [client.get(f"/api/jobs/{i}").json()["status"] for i in ids] == ["running", "running"]

    with small.open("rb") as fh:
        r = client.post(f"/api/sessions/{sid}/upload?wait=0",
                        files={"file": ("small.mp4", fh, "video/mp4")},
                        data={"transcribe": "false", "add_to_timeline": "false"})
    assert r.status_code == 202, r.text
    up = _poll(client, r.json()["job_id"], {"completed", "failed"}, 20)
    assert up["status"] == "completed", up
    assert [client.get(f"/api/jobs/{i}").json()["status"] for i in ids] == ["running", "running"], \
        "exports finished before the upload was measured — fixture too small"

    t0 = time.monotonic()
    for i in ids:
        assert client.post(f"/api/jobs/{i}/cancel").status_code == 200
    ends = [_poll(client, i, {"cancelled", "failed", "completed"}, 15) for i in ids]
    assert [j["status"] for j in ends] == ["cancelled", "cancelled"], ends
    assert time.monotonic() - t0 < 10
