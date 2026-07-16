"""Canvas orientation is inferred from the first uploaded source (R6).

Regression coverage for issues 6/7 (docs/superpowers/plans/
2026-07-10-editor-issues-verification-and-fixes.md): every fresh session
starts with the hardcoded 1080x1920 vertical default regardless of what gets
uploaded. A landscape source dropped into that canvas gets pillarboxed with
thick black bars — reported as "aspect ratio inconsistent" / upload "distorts
the video" (the compositor actually preserves aspect ratio via scale+pad, so
it's letterboxed rather than stretched, but the visual result reads just as
wrong). Only the FIRST upload into an empty timeline should auto-match the
canvas; a later upload into an existing project must not silently resize it.
"""
from __future__ import annotations
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from video_ai_editor.main import app


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    from video_ai_editor import storage as _storage, main as _main
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path)
    monkeypatch.setattr(_main, "WORKDIR", tmp_path)
    _main._STORES.clear()
    return TestClient(app)


def _make_video(p: Path, *, w: int, h: int, dur: float = 1.0):
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi",
                    "-i", f"color=c=blue:s={w}x{h}:d={dur}:r=30",
                    "-pix_fmt", "yuv420p", str(p)],
                   check=True, capture_output=True)


def _upload(client, sid, path, name="v.mp4"):
    with path.open("rb") as f:
        return client.post(
            f"/api/sessions/{sid}/upload",
            files={"file": (name, f, "video/mp4")},
            data={"add_to_timeline": "true", "transcribe": "false"},
        )


def test_landscape_upload_into_empty_session_sets_16_9_canvas(client, tmp_path: Path):
    sid = client.post("/api/sessions").json()["id"]
    v = tmp_path / "landscape.mp4"
    _make_video(v, w=1920, h=1080)
    r = _upload(client, sid, v)
    assert r.status_code == 200

    edl = client.get(f"/api/sessions/{sid}/edl").json()
    assert edl["canvas"]["w"] == 1920
    assert edl["canvas"]["h"] == 1080


def test_portrait_upload_into_empty_session_sets_9_16_canvas(client, tmp_path: Path):
    sid = client.post("/api/sessions").json()["id"]
    v = tmp_path / "portrait.mp4"
    _make_video(v, w=1080, h=1920)
    r = _upload(client, sid, v)
    assert r.status_code == 200

    edl = client.get(f"/api/sessions/{sid}/edl").json()
    assert edl["canvas"]["w"] == 1080
    assert edl["canvas"]["h"] == 1920


def test_square_upload_into_empty_session_sets_1_1_canvas(client, tmp_path: Path):
    sid = client.post("/api/sessions").json()["id"]
    v = tmp_path / "square.mp4"
    _make_video(v, w=720, h=720)
    r = _upload(client, sid, v)
    assert r.status_code == 200

    edl = client.get(f"/api/sessions/{sid}/edl").json()
    assert edl["canvas"]["w"] == 1080
    assert edl["canvas"]["h"] == 1080


def test_second_upload_into_a_nonempty_project_does_not_resize_canvas(client, tmp_path: Path):
    """A user adding b-roll (or any second clip) must not have their
    already-chosen canvas silently changed underneath them."""
    sid = client.post("/api/sessions").json()["id"]
    v1 = tmp_path / "v1.mp4"
    _make_video(v1, w=1080, h=1920)  # first upload: portrait -> 9:16 canvas
    r1 = _upload(client, sid, v1, name="v1.mp4")
    assert r1.status_code == 200
    edl1 = client.get(f"/api/sessions/{sid}/edl").json()
    assert edl1["canvas"]["w"] == 1080 and edl1["canvas"]["h"] == 1920

    v2 = tmp_path / "v2.mp4"
    _make_video(v2, w=1920, h=1080)  # second upload: landscape b-roll
    r2 = _upload(client, sid, v2, name="v2.mp4")
    assert r2.status_code == 200
    edl2 = client.get(f"/api/sessions/{sid}/edl").json()
    # Canvas must stay whatever the FIRST upload set it to.
    assert edl2["canvas"]["w"] == 1080 and edl2["canvas"]["h"] == 1920
