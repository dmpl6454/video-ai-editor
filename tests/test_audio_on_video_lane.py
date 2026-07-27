"""An audio-only source must never reach a video lane.

Every lane check in the app tested the TRACK TYPE and none asked what the file
actually contains, so an mp3 dropped on v1 was accepted everywhere. The render
then emitted `[i:v]scale=…` for a file with no video stream and ffmpeg answered
"Stream specifier ':v' … matches no streams / Error binding filtergraph
inputs/outputs" — killing the WHOLE render, so every preview and export 422'd
until the clip was removed. This is the mirror of `normalize._has_video`.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import EDL, Canvas, Clip, Track
from video_ai_editor.main import _render_failure_message

FFMPEG = shutil.which("ffmpeg")
pytestmark = pytest.mark.skipif(not FFMPEG, reason="ffmpeg not on PATH")


def _mkvideo(p: Path, dur: float = 2.0) -> str:
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi",
                    "-i", f"color=c=blue:s=320x180:d={dur}:r=30",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={dur}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(p)], check=True, capture_output=True)
    return str(p)


def _mkaudio(p: Path, dur: float = 2.0) -> str:
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi",
                    "-i", f"sine=frequency=220:duration={dur}",
                    "-c:a", "libmp3lame", str(p)], check=True, capture_output=True)
    return str(p)


@pytest.fixture()
def store(tmp_path: Path) -> EDLStore:
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=[
        Track(id="v1", type="video", clips=[]),
        Track(id="v2", type="video", z=1, clips=[]),
        Track(id="music", type="music", z=5, clips=[]),
    ])
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    return EDLStore(tmp_path)


def test_add_clip_rejects_audio_only_on_v1(store, tmp_path):
    mp3 = _mkaudio(tmp_path / "song.mp3")
    with pytest.raises(ValueError, match="no video stream"):
        dispatch(store, "add_clip", {"track": "v1", "src": mp3,
                                     "in": 0.0, "out": 2.0, "start": 0.0})
    assert store.edl.get_track("v1").clips == []


def test_add_clip_rejects_audio_only_on_pip_lane(store, tmp_path):
    mp3 = _mkaudio(tmp_path / "song.mp3")
    with pytest.raises(ValueError, match="no video stream"):
        dispatch(store, "add_clip", {"track": "v2", "src": mp3,
                                     "in": 0.0, "out": 2.0, "start": 0.0})


def test_add_clip_allows_audio_on_music_lane(store, tmp_path):
    mp3 = _mkaudio(tmp_path / "song.mp3")
    dispatch(store, "add_clip", {"track": "music", "src": mp3,
                                 "in": 0.0, "out": 2.0, "start": 0.0})
    assert len(store.edl.get_track("music").clips) == 1


def test_add_clip_allows_real_video_on_v1(store, tmp_path):
    mp4 = _mkvideo(tmp_path / "v.mp4")
    dispatch(store, "add_clip", {"track": "v1", "src": mp4,
                                 "in": 0.0, "out": 2.0, "start": 0.0})
    assert len(store.edl.get_track("v1").clips) == 1


def test_move_clip_rejects_audio_dragged_onto_v1(store, tmp_path):
    """The second ingress: drag the music clip up from the Music lane."""
    mp3 = _mkaudio(tmp_path / "song.mp3")
    dispatch(store, "add_clip", {"track": "music", "src": mp3,
                                 "in": 0.0, "out": 2.0, "start": 0.0})
    cid = store.edl.get_track("music").clips[0].id
    with pytest.raises(ValueError, match="no video stream"):
        dispatch(store, "move_clip", {"clip_id": cid, "new_track": "v1",
                                      "new_start": 0.0})
    # Still on music, untouched.
    assert [c.id for c in store.edl.get_track("music").clips] == [cid]


def test_guard_fails_open_when_source_cannot_be_probed(store):
    """A missing/unreadable path must not become a new way for edits to fail —
    the guard only rejects a definitive "has streams, none of them video"."""
    dispatch(store, "add_clip", {"track": "v1", "src": "/nonexistent/fake.mp4",
                                 "in": 0.0, "out": 2.0, "start": 0.0})
    assert len(store.edl.get_track("v1").clips) == 1


def test_render_failure_message_is_actionable_for_stream_mismatch():
    """Projects saved BEFORE the guard existed still can't render; the message
    has to name the real problem instead of blaming the user's media."""
    tail = ("[fc#0] Stream specifier ':v' in filtergraph description "
            "[0:v]scale=1080:1920[v] matches no streams.\n"
            "Error binding filtergraph inputs/outputs: Invalid argument")
    msg = _render_failure_message(tail)
    assert "audio-only file on the video track" in msg
    assert "Music" in msg
    assert "corrupt" not in msg.lower()


def test_render_failure_message_keeps_overlay_and_generic_cases():
    overlay = _render_failure_message(r"C:\cache\st_0123abcd4567ef89.png: Invalid data")
    assert "overlay" in overlay.lower()
    generic = _render_failure_message("some other ffmpeg complaint")
    assert "corrupt frames" in generic
