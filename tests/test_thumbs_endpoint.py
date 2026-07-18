"""GET /api/sessions/{sid}/thumb — single-frame JPEG thumbnails.

Feeds the timeline filmstrip and media-bin previews. Same trust posture as
/waveform: `src` is untrusted and must resolve inside the session workdir.
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


def _make_video(p: Path, *, dur: float = 2.0):
    p.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi",
                    "-i", f"color=c=blue:s=320x180:d={dur}:r=30",
                    "-pix_fmt", "yuv420p", str(p)],
                   check=True, capture_output=True)


def test_thumb_returns_cached_jpeg(client, tmp_path: Path):
    sid = client.post("/api/sessions").json()["id"]
    src = tmp_path / sid / "uploads" / "clip.mp4"
    _make_video(src)

    r = client.get(f"/api/sessions/{sid}/thumb",
                   params={"src": str(src), "t": 0.5, "h": 54})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content[:2] == b"\xff\xd8"  # JPEG magic

    # Second call is a cache hit: identical bytes, exactly one cached file.
    r2 = client.get(f"/api/sessions/{sid}/thumb",
                    params={"src": str(src), "t": 0.5, "h": 54})
    assert r2.status_code == 200
    assert r2.content == r.content
    thumbs = list((tmp_path / sid / "cache" / "thumbs").glob("*.jpg"))
    assert len(thumbs) == 1


def test_thumb_rejects_src_outside_session(client, tmp_path: Path):
    sid = client.post("/api/sessions").json()["id"]
    outside = tmp_path / "elsewhere.mp4"
    _make_video(outside)
    r = client.get(f"/api/sessions/{sid}/thumb",
                   params={"src": str(outside), "t": 0.0})
    assert r.status_code == 403


def test_thumb_rejects_prefix_sibling_session(client, tmp_path: Path):
    """s_ab must not grant access to s_abcd — a bare startswith() path check
    admits any sibling whose directory name extends the session's."""
    sid = client.post("/api/sessions").json()["id"]
    sibling = tmp_path / f"{sid}x" / "uploads" / "clip.mp4"
    _make_video(sibling)
    r = client.get(f"/api/sessions/{sid}/thumb",
                   params={"src": str(sibling), "t": 0.0})
    assert r.status_code == 403


def test_thumb_rejects_relative_src(client, tmp_path: Path):
    sid = client.post("/api/sessions").json()["id"]
    r = client.get(f"/api/sessions/{sid}/thumb",
                   params={"src": "uploads/clip.mp4", "t": 0.0})
    assert r.status_code == 403


def test_thumb_404_for_missing_source(client, tmp_path: Path):
    sid = client.post("/api/sessions").json()["id"]
    r = client.get(f"/api/sessions/{sid}/thumb",
                   params={"src": str(tmp_path / sid / "uploads" / "nope.mp4")})
    assert r.status_code == 404
