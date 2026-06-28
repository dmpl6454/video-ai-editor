"""Import/render error handling: a bad file must NEVER return a bare 500.

Reproduces the "video import failed 500" report: uploading a file ffmpeg can't
read used to bubble a non-RuntimeError out of the ingest pipeline as an
unhandled 500. It must be a clean 422 the UI can show. Likewise a render that
fails on a corrupt clip → 422, not 500.
"""
from __future__ import annotations
import importlib
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch, tmp_path: Path):
    from video_ai_editor import storage as _storage
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path)
    from video_ai_editor import main as _main
    importlib.reload(_main)
    monkeypatch.setattr(_main, "WORKDIR", tmp_path)
    _main._STORES.clear()
    return TestClient(_main.app)


def test_garbage_file_import_returns_422_not_500(client, tmp_path: Path):
    bad = tmp_path / "not_a_video.mp4"
    bad.write_bytes(os.urandom(3000))  # random bytes, not a valid container
    sid = client.post("/api/sessions").json()["id"]
    with bad.open("rb") as f:
        r = client.post(f"/api/sessions/{sid}/upload",
                        files={"file": ("not_a_video.mp4", f, "video/mp4")},
                        data={"add_to_timeline": "true", "transcribe": "false"})
    assert r.status_code == 422, f"expected 422, got {r.status_code}: {r.text[:200]}"
    assert r.status_code != 500


def test_empty_file_import_returns_422(client, tmp_path: Path):
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    sid = client.post("/api/sessions").json()["id"]
    with empty.open("rb") as f:
        r = client.post(f"/api/sessions/{sid}/upload",
                        files={"file": ("empty.mp4", f, "video/mp4")},
                        data={"add_to_timeline": "true", "transcribe": "false"})
    assert r.status_code == 422


def test_preview_of_unrenderable_clip_returns_422(client, tmp_path: Path):
    # A clip pointing at a non-video file → render fails → must be 422, not 500.
    bad = tmp_path / "broken.mp4"
    bad.write_bytes(os.urandom(2000))
    sid = client.post("/api/sessions").json()["id"]
    client.post(f"/api/sessions/{sid}/dispatch", json={
        "tool": "add_clip",
        "args": {"track": "v1", "src": str(bad), "in": 0, "out": 2, "start": 0},
    })
    r = client.post(f"/api/sessions/{sid}/preview")
    assert r.status_code == 422, f"expected 422, got {r.status_code}"
