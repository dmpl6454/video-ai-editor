"""QA-026: the "↓ MP4" link must know WHICH timeline it was rendered from.

The toolbar marked an export outdated when `ops.length > exportGen`, which
Undo lowers — so exporting and then undoing showed a stale file as current.
The fix compares EDL hashes, which needs two facts from the backend: the hash
an export was rendered from (in its payload — new) and the current timeline's
hash (GET /sessions/{sid} summary.edl_hash, which the store now keeps). This
drives a real export through ffmpeg.
"""
from __future__ import annotations

import importlib
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from video_ai_editor import platformutil as _pu


@pytest.fixture()
def client(monkeypatch, tmp_path: Path):
    from video_ai_editor import storage as _storage
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path / "wd")
    from video_ai_editor import main as _main
    importlib.reload(_main)
    monkeypatch.setattr(_main, "WORKDIR", tmp_path / "wd")
    _main._STORES.clear()
    return TestClient(_main.app)


@pytest.fixture()
def clip(tmp_path: Path) -> Path:
    dst = tmp_path / "c.mp4"
    subprocess.run([_pu.FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=160x90:rate=30:duration=1", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=1", "-shortest", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", str(dst)], check=True)
    return dst


def _dispatch(client, sid, tool, args=None):
    r = client.post(f"/api/sessions/{sid}/dispatch", json={"tool": tool, "args": args or {}})
    assert r.status_code == 200, r.text
    return r.json()


def test_session_reports_the_current_edl_hash_and_undo_changes_it(client, clip):
    sid = client.post("/api/sessions").json()["id"]
    with clip.open("rb") as f:
        client.post(f"/api/sessions/{sid}/upload", files={"file": ("c.mp4", f, "video/mp4")},
                    data={"transcribe": "false"})
    before = client.get(f"/api/sessions/{sid}").json()["summary"]["edl_hash"]
    edited = _dispatch(client, sid, "set_aspect_ratio", {"ratio": "1:1"})["edl_hash"]
    assert client.get(f"/api/sessions/{sid}").json()["summary"]["edl_hash"] == edited != before
    _dispatch(client, sid, "undo")
    assert client.get(f"/api/sessions/{sid}").json()["summary"]["edl_hash"] == before
    _dispatch(client, sid, "redo")
    assert client.get(f"/api/sessions/{sid}").json()["summary"]["edl_hash"] == edited


def test_export_payload_names_the_timeline_it_was_rendered_from(client, clip):
    sid = client.post("/api/sessions").json()["id"]
    with clip.open("rb") as f:
        client.post(f"/api/sessions/{sid}/upload", files={"file": ("c.mp4", f, "video/mp4")},
                    data={"transcribe": "false"})
    current = client.get(f"/api/sessions/{sid}").json()["summary"]["edl_hash"]
    r = client.post(f"/api/sessions/{sid}/export", json={"height": 90})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["edl_hash"] == current
    assert Path(body["path"]).is_file()
    # After an undo-able edit the export no longer matches the timeline.
    _dispatch(client, sid, "set_aspect_ratio", {"ratio": "1:1"})
    assert client.get(f"/api/sessions/{sid}").json()["summary"]["edl_hash"] != body["edl_hash"]
