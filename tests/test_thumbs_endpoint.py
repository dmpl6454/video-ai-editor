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


def test_thumb_at_the_very_end_of_a_clip_still_returns_a_frame(client, tmp_path: Path):
    """Found by loading the packaged app and watching its network log.

    The Timeline requests a thumbnail for the TAIL of a clip. A seek at or past
    the last frame decodes nothing, so ffmpeg wrote no output and the endpoint
    answered 422 — a broken thumbnail and a console error for a perfectly valid
    file. Measured on a 4.017s clip: t=3.9 was fine, t=3.967 was a 422.
    """
    sid = client.post("/api/sessions").json()["id"]
    src = tmp_path / sid / "uploads" / "tail.mp4"
    _make_video(src, dur=2.0)

    for t in (1.9, 1.98, 2.0, 2.5):
        r = client.get(f"/api/sessions/{sid}/thumb",
                       params={"src": str(src), "t": t, "h": 54})
        assert r.status_code == 200, f"t={t} -> {r.status_code} {r.text[:200]}"
        assert r.content[:2] == b"\xff\xd8", f"t={t} did not return a JPEG"


def test_thumb_end_retry_does_not_change_the_normal_path(client, tmp_path: Path):
    """The end-of-file retry must be a FAILURE path only: a mid-clip thumb has
    to keep coming from the requested timestamp, not the last frame."""
    sid = client.post("/api/sessions").json()["id"]
    src = tmp_path / sid / "uploads" / "grad.mp4"
    src.parent.mkdir(parents=True, exist_ok=True)
    # Brightness ramps over time, so an early frame and a late frame differ.
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi",
                    "-i", "gradients=s=320x180:d=3:r=30",
                    "-pix_fmt", "yuv420p", str(src)],
                   check=True, capture_output=True)

    early = client.get(f"/api/sessions/{sid}/thumb",
                       params={"src": str(src), "t": 0.2, "h": 54})
    late = client.get(f"/api/sessions/{sid}/thumb",
                      params={"src": str(src), "t": 2.5, "h": 54})
    assert early.status_code == late.status_code == 200
    assert early.content != late.content, (
        "mid-clip thumbnails collapsed to the same frame — the retry is "
        "firing on the normal path")
