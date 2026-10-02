"""Backend pieces of the 2026-10-02 desktop redesign: `reattach_audio`
("Recover audio", the inverse of detach_audio) and the project-card facts
`GET /api/sessions` now carries (duration, size_bytes)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from video_ai_editor.agent.dispatch import DISPATCH, dispatch
from video_ai_editor.agent.tools import ALL_TOOLS
from video_ai_editor.edl.snapshot import EDLStore
from video_ai_editor import storage


def _make_clip(path: Path) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=30:duration=2",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(path)],
        check=True, text=True, encoding="utf-8", errors="replace",
    )
    return path


@pytest.fixture
def store(tmp_path: Path) -> EDLStore:
    st = EDLStore(tmp_path / "s")
    src = _make_clip(tmp_path / "a.mp4")
    dispatch(st, "add_clip", {"track": "v1", "src": str(src), "in": 0.0, "out": 2.0, "start": 0.0})
    return st


def test_reattach_audio_is_registered_and_advertised():
    assert "reattach_audio" in DISPATCH
    assert any(t["name"] == "reattach_audio" for t in ALL_TOOLS)


def test_recover_audio_puts_the_sound_back_and_undo_separates_again(store: EDLStore):
    v1 = store.edl.tracks[0]
    vid = v1.clips[0]
    r = dispatch(store, "detach_audio", {"clip_id": vid.id})
    aid = r["audio_clip_id"]
    assert store.edl.get_clip(vid.id)[1].audio.mute is True
    assert store.edl.get_clip(aid) is not None

    out = dispatch(store, "reattach_audio", {"clip_id": aid})
    assert out["video_clip_id"] == vid.id
    assert store.edl.get_clip(aid) is None, "the extracted clip is removed"
    assert store.edl.get_clip(vid.id)[1].audio.mute is False, "the picture plays its own sound again"

    dispatch(store, "undo", {})
    assert store.edl.get_clip(aid) is not None
    assert store.edl.get_clip(vid.id)[1].audio.mute is True


def test_recover_audio_refuses_a_clip_that_was_not_extracted(store: EDLStore):
    vid = store.edl.tracks[0].clips[0]
    with pytest.raises(ValueError):
        dispatch(store, "reattach_audio", {"clip_id": vid.id})


def test_session_rows_carry_duration_and_media_size(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(storage, "WORKDIR", tmp_path)
    d = tmp_path / "s_abc123"
    (d / "uploads").mkdir(parents=True)
    (d / "uploads" / "x.bin").write_bytes(b"\0" * 1234)
    (d / "edl.json").write_text(json.dumps({"version": 3, "duration": 12.5, "tracks": []}), encoding="utf-8")
    (d / "meta.json").write_text(json.dumps({"name": "Demo"}), encoding="utf-8")
    rows = storage.list_sessions()
    row = next(r for r in rows if r["id"] == "s_abc123")
    assert row["duration"] == 12.5
    assert row["size_bytes"] == 1234
    assert row["name"] == "Demo"
