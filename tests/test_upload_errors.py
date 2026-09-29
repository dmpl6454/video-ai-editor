"""Import/render error handling: a bad file must NEVER return a bare 500.

Reproduces the "video import failed 500" report: uploading a file ffmpeg can't
read used to bubble a non-RuntimeError out of the ingest pipeline as an
unhandled 500. It must be a clean 422 the UI can show. Likewise a render that
fails on a corrupt clip → 422, not 500.
"""
from __future__ import annotations
import importlib
import os
import struct
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


def _mp4_without_a_moov(size: int = 2000) -> bytes:
    """An MP4 that every ffmpeg identifies as MP4 and none can open: a valid
    `ftyp` box and an `mdat` of zeros, no `moov`. The `ftyp` tag gives the mov
    demuxer the top probe score, so no other demuxer is tried, and the header
    read ends in "moov atom not found" (ffprobe exit 1, ffmpeg 8.1.1).

    Random bytes are NOT that fixture: the container guess then depends on the
    dice and on the ffmpeg version. Measured on ffmpeg 8.1.1, 400 files of
    os.urandom(2000): 397 unreadable, 3 identified as an `lrc` lyrics file
    (one subtitle stream) - which add_clip rightly refuses on a video lane, so
    the clip this test needs on v1 was never added (the ubuntu CI failure,
    ffmpeg 6.1.1: "add_clip returned 400: 'broken.mp4' has no video stream")."""
    ftyp = struct.pack(">I4s4sI4s4s", 24, b"ftyp", b"isom", 0x200, b"isom", b"mp41")
    payload = bytes(size - len(ftyp) - 8)
    return ftyp + struct.pack(">I4s", len(payload) + 8, b"mdat") + payload


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
    #
    # This asserts its own PRECONDITION and reports context on failure, because
    # it previously did neither: it ignored add_clip's status and asserted only
    # the preview code. An empty timeline renders fine (v6: gaps become
    # black+silence), so if add_clip is ever rejected the preview legitimately
    # returns 200 and the failure reads as "the render didn't fail" when the
    # real story is "the clip was never added". A Windows-only 200 here in the
    # round-5 verification run could not be diagnosed from the log for exactly
    # that reason — the message now carries what is needed.
    bad = tmp_path / "broken.mp4"
    bad.write_bytes(_mp4_without_a_moov())
    sid = client.post("/api/sessions").json()["id"]
    add = client.post(f"/api/sessions/{sid}/dispatch", json={
        "tool": "add_clip",
        "args": {"track": "v1", "src": str(bad), "in": 0, "out": 2, "start": 0},
    })
    assert add.status_code == 200, (
        f"precondition failed: add_clip returned {add.status_code}: {add.text[:300]}")
    edl = client.get(f"/api/sessions/{sid}/edl").json()
    v1 = [c for t in edl["tracks"] if t["id"] == "v1" for c in t["clips"]]
    assert len(v1) == 1, f"precondition failed: v1 holds {len(v1)} clips, expected 1"

    r = client.post(f"/api/sessions/{sid}/preview")
    assert r.status_code == 422, (
        f"expected 422, got {r.status_code}.\n"
        f"  body: {r.text[:400]}\n"
        f"  v1 clip: {v1[0]}\n"
        f"  duration: {edl.get('duration')}\n"
        f"  A 200 here means ffmpeg RENDERED a garbage source instead of "
        f"failing — check whether the chunk path swallowed the error and the "
        f"black-filler covered the clip's span.")
